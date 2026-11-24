"""Offline checks for resolving a meal without requiring a batch ID."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nutrition_demo.models import Result
from nutrition_demo.web_meals import WebMeals


def batch(batch_id, count, *, name='curry chicken', prepared=24, saved=12, remaining=None):
    return {
        'id': batch_id, 'name': name, 'recipe_id': 'curry',
        'prepared_on': date(2026, 11, prepared),
        'saved_at': datetime(2026, 11, prepared, saved, tzinfo=timezone.utc),
        'total_boxes': count, 'remaining_boxes': count if remaining is None else remaining,
        'default_addons': [],
    }


class Store:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, statement):
        assert 'FROM batches' in statement
        return SimpleNamespace(fetchall=lambda: [row.copy() for row in self.rows if row['remaining_boxes'] > 0])


@pytest.fixture
def resolver():
    state = {}
    workflow = SimpleNamespace(load=lambda: state, save=lambda new: state.update(new))
    log = Mock(return_value=Result(True, {'remaining_boxes': 1}))
    service = SimpleNamespace(store=Store([]), _day=lambda word: {
        'today': '2026-11-24', 'yesterday': '2026-11-23',
    }.get(word, word), log_meal=log)
    meals = WebMeals(service, workflow)
    return meals, state, service


def test_two_box_reference_selects_by_original_count_and_logs_one(resolver):
    meals, _, service = resolver
    service.store.rows = [batch('eight', 8, remaining=2), batch('two', 2, name='Curry')]
    text = 'I ate one box from the 2-box curry batch I just saved.'
    assert meals.recognizes(text)
    result = meals.handle(text, 'request-1')
    assert result.ok
    assert 'Curry' in result.data['message']
    assert '1 box remaining' in result.data['message']
    service.log_meal.assert_called_once_with(
        batch_id='two', eaten_on='2026-11-24', meal=None,
        request_id='web-meal:request-1', addon_override=[],
    )


def test_recent_reference_selects_most_recent_saved_matching_batch(resolver):
    meals, _, service = resolver
    service.store.rows = [batch('older', 4, saved=11), batch('latest', 4)]
    result = meals.handle('I ate one box from the 4-box curry chicken batch I just saved for lunch.', 'read-1')
    assert result.ok and meals.choices() is None
    assert service.log_meal.call_args.kwargs['batch_id'] == 'latest'
    assert service.log_meal.call_args.kwargs['meal'] == 'lunch'


def test_ambiguous_batch_requires_choice_and_keeps_original_meal_date(resolver):
    meals, _, service = resolver
    service.store.rows = [batch('first', 2, prepared=23), batch('second', 4, prepared=23)]
    result = meals.handle('I ate one box of curry chicken yesterday for dinner.', 'read-2')
    assert result.ok
    choice = meals.choices()
    assert {row['id'] for row in choice['options']} == {'first', 'second'}
    assert choice['options'][0]['prepared_on'] == '2026-11-23'
    assert service.log_meal.call_count == 0
    invalid = meals.handle(f"/meal-select {choice['id']} wrong-batch", 'bad-click')
    assert not invalid.ok
    assert meals.choices() == choice
    assert service.log_meal.call_count == 0
    picked = meals.handle(f"/meal-select {choice['id']} second", 'valid-click')
    assert picked.ok and meals.choices() is None
    service.log_meal.assert_called_once_with(
        batch_id='second', eaten_on='2026-11-23', meal='dinner',
        request_id='web-meal:valid-click', addon_override=[],
    )
    assert not meals.handle(f"/meal-select {choice['id']} second", 'stale-click').ok
    assert service.log_meal.call_count == 1


def test_same_cooking_time_is_still_ambiguous_and_unavailable_choice_is_refused(resolver):
    meals, _, service = resolver
    service.store.rows = [batch('first', 4), batch('second', 4)]
    assert meals.handle('I ate one box from the 4-box curry chicken batch I just saved.', 'ambiguous').ok
    choice = meals.choices()
    assert len(choice['options']) == 2
    service.store.rows[0]['remaining_boxes'] = 0
    result = meals.handle(f"/meal-select {choice['id']} first", 'no-longer-available')
    assert not result.ok
    assert service.log_meal.call_count == 0
    assert [b['id'] for b in meals.choices()['options']] == ['second']


@pytest.mark.parametrize('message', [
    'What if I ate one box of curry chicken?',
    'I did not eat one box of curry chicken.',
    'I ate two boxes of curry chicken.',
    'I ate one box of curry chicken and drank milk.',
    'I ate one box of curry chicken with yogurt.',
])
def test_hypothetical_multi_box_or_unresolved_addons_never_log_offline(resolver, message):
    meals, _, service = resolver
    service.store.rows = [batch('one', 4)]
    assert not meals.handle(message, 'unsupported').ok
    service.log_meal.assert_not_called()


def test_unknown_recipe_does_not_guess_and_named_batch_can_be_logged(resolver):
    meals, _, service = resolver
    service.store.rows = [batch('one', 4, name='Weeknight curry')]
    assert not meals.handle('I ate one box of tacos.', 'unknown').ok
    service.log_meal.assert_not_called()
    assert meals.handle('I ATE ONE BOX OF WEEKNIGHT CURRY, FOR LUNCH.', 'known').ok
    assert service.log_meal.call_args.kwargs['batch_id'] == 'one'


def test_addons_require_an_explicit_statement_before_recording(resolver):
    meals, _, service = resolver
    service.store.rows = [batch('one', 4)]
    service.store.rows[0]['default_addons'] = [{'ingredient_id': 'yogurt'}]
    assert not meals.handle('I ate one box of curry chicken.', 'unspecified-addons').ok
    service.log_meal.assert_not_called()
    assert meals.handle("I ate one box of curry chicken. I didn't eat any add-ons.", 'no-addons').ok
    assert service.log_meal.call_args.kwargs['addon_override'] == []
