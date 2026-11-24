"""Run only against the existing isolated db_test/nutrition_test fixture."""
from uuid import uuid4
import psycopg
from psycopg import sql
import pytest
from nutrition_demo.web_workspace import WebWorkspace, WorkspaceError, WebStore


@pytest.fixture
def shared(store):
    schema = 'web_demo_' + uuid4().hex
    with psycopg.connect(store.dsn) as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    ws_store = WebStore(store.dsn, schema)
    try:
        ws_store.migrate()
        ws = WebWorkspace(ws_store)
        with ws.exclusive():
            ws.reset()
        yield ws
    finally:
        with psycopg.connect(store.dsn) as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_shared_state_survives_new_worker_and_reset(shared, monkeypatch):
    monkeypatch.delenv('WEB_ENABLE_MODEL_CALLS', raising=False)
    with shared.exclusive():
        response = shared.submit(str(uuid4()), '/inventory')
    reopened = WebWorkspace(shared.store)
    assert reopened.snapshot()['messages'] == [response]
    assert reopened.snapshot()['batches'] == 'No batches prepared yet.'
    with reopened.exclusive():
        reopened.reset()
    assert reopened.snapshot()['messages'] == []
    assert reopened.service.batch_summary().data['batches'] == []


def test_separate_connections_cannot_enter_same_workspace(shared):
    reopened = WebWorkspace(shared.store)
    with shared.exclusive():
        with pytest.raises(WorkspaceError) as error:
            with reopened.exclusive():
                pytest.fail('Should be busy')
        assert error.value.status_code == 409
    with reopened.exclusive():
        pass


def save_batch(workspace, boxes):
    recipe = workspace.service.catalog.recipe('curry')
    items = workspace.workflow.scaled(recipe['ingredients'], boxes, recipe['default_requested_boxes'])
    preview = workspace.service.preview_preparation('curry', items, [{
        'name': recipe['name'], 'boxes': boxes,
        'allocations': [{'cooking_ingredient_id': i['ingredient_id'], 'unit': i['unit'], 'quantity': i['quantity']}
                        for i in items],
    }], prepared_on='today')
    assert preview.ok, preview.error
    saved = workspace.service.confirm_preview(preview.data['id'], str(uuid4()))
    assert saved.ok, saved.error
    return saved.data['batch_ids'][0]


def test_preview_requires_current_id_and_explicit_decision(shared):
    batch = save_batch(shared, 4)
    meal = shared.service.log_meal(batch, 'today', 'lunch', request_id='web-test-meal')
    agent = shared.agent()
    preview = agent._dispatch('preview_correction', {'event_id': meal.data['event_id'], 'changes': {'date': 'yesterday'}}, 'web-test-preview')
    assert preview.ok
    pending = shared.workflow.load()['pending_preview']
    before = shared.service.daily_summary('today').data
    with shared.exclusive():
        rejected = shared.submit(str(uuid4()), '/confirm wrong-preview')
    assert not rejected['ok']
    assert shared.service.daily_summary('today').data == before
    with shared.exclusive():
        rejected = shared.submit(str(uuid4()), '/reject ' + pending)
        assert rejected['ok']
        assert shared.snapshot()['preview'] is None
        assert shared.service.daily_summary('today').data == before
        preview = agent._dispatch('preview_correction', {
            'event_id': meal.data['event_id'], 'changes': {'date': 'yesterday'},
        }, 'web-test-second-preview')
        assert preview.ok
        accepted = shared.submit(str(uuid4()), '/confirm ' + preview.data['id'])
    assert accepted['ok']
    assert shared.service.daily_summary('today').data['events'] == 0
    assert shared.service.daily_summary('yesterday').data['events'] == 1
    assert shared.service.batch_summary().data['batches'][0]['remaining_boxes'] == 3


def test_failed_reset_rolls_back_chat_and_records(shared, monkeypatch):
    with shared.exclusive():
        shared.submit(str(uuid4()), '/batches')
        before = shared.snapshot()

        def fail_seed(_):
            raise RuntimeError('fixture seed failed')

        monkeypatch.setattr(shared.store, 'seed', fail_seed)
        with pytest.raises(RuntimeError, match='fixture seed failed'):
            shared.reset()
        assert shared.snapshot() == before


def send(workspace, text, request_id=None):
    with workspace.exclusive():
        return workspace.submit(request_id or str(uuid4()), text)


def stock(workspace, ingredient_id):
    return next(r['quantity'] for r in workspace.service.list_inventory().data['items']
                if r['ingredient_id'] == ingredient_id)


def confirm_current(workspace, decision='/confirm'):
    return send(workspace, f'{decision} {workspace.snapshot()["preview"]["id"]}')


def test_full_curry_demo_is_available_without_a_model(shared, monkeypatch):
    import openai
    monkeypatch.setenv('WEB_ENABLE_MODEL_CALLS', 'false')
    monkeypatch.setattr(openai, 'OpenAI', lambda **_: pytest.fail('Constructed a live client'))

    start = shared.snapshot()
    assert not start['model_enabled']
    assert start['goals']['meal']['kcal'] == '1000'
    assert start['goals']['daily']['kcal'] == '1800'
    assert 'No meals logged today' in start['daily']
    assert 'No batches prepared yet' in start['batches']
    assert 'Chicken breast' in start['inventory']
    assert 'fixture_mix' not in start['inventory']
    assert send(shared, '/inventory-ok')['ok']
    assert shared.snapshot()['recipe_options'][0]['name'].casefold() == 'curry chicken'
    assert send(shared, 'curry')['ok']
    assert send(shared, '/adjust')['ok']

    proposal = shared.snapshot()['proposal']
    assert proposal['passed'] and not proposal['can_accept']
    assert float(proposal['after']['kcal']) == pytest.approx(956.11, abs=.01)
    chicken = next(i for i in proposal['ingredients'] if 'chicken' in i['name'].casefold())
    assert (float(chicken['needed']), float(chicken['available']), float(chicken['missing'])) == (1160, 1000, 160)
    accept = '/accept-recipe ' + proposal['id']
    assert not send(shared, accept)['ok']

    assert send(shared, 'I bought 200 g of raw chicken breast.')['ok']
    assert stock(shared, 'chicken') == 1000
    assert confirm_current(shared, '/reject')['ok']
    assert not shared.snapshot()['proposal']['can_accept']
    assert send(shared, '/purchase chicken 200')['ok']
    assert confirm_current(shared)['ok']
    assert stock(shared, 'chicken') == 1200
    assert shared.snapshot()['proposal']['id'] == proposal['id']
    assert shared.snapshot()['proposal']['can_accept']

    # A second worker can reduce inventory after the first browser renders the Accept button.
    other = WebWorkspace(shared.store)
    update = other.service.preview_inventory_change(
        [{'ingredient_id': 'chicken', 'quantity': '1000', 'unit': 'g', 'state': 'raw'}],
        'reconcile',
    )
    assert other.service.confirm_preview(update.data['id'], 'other-browser-inventory').ok
    assert not send(shared, accept)['ok']
    assert send(shared, '/purchase chicken 200')['ok']
    assert confirm_current(shared)['ok']
    assert send(shared, accept)['ok']

    assert not send(shared, '/cook-plan')['ok']
    assert not send(shared, '/cook-plan old-proposal')['ok']
    assert shared.snapshot()['preview'] is None
    assert send(shared, '/cook-plan ' + proposal['id'])['ok']
    snapshot = shared.snapshot()
    assert snapshot['preview']['title'] == 'Save 4 boxes of curry chicken?'
    assert snapshot['preview']['id'] not in snapshot['preview']['text']
    assert stock(shared, 'chicken') == 1200
    receipt = confirm_current(shared)
    assert receipt['ok'] and 'Saved 4 boxes of curry chicken' in receipt['assistant']
    assert shared.snapshot()['proposal'] is None
    assert stock(shared, 'chicken') == 40
    assert 'Remaining 4/4' in shared.snapshot()['batches']

    request_id = str(uuid4())
    text = 'I ate one box from the 4-box curry chicken batch I just saved for lunch.'
    result = send(shared, text, request_id)
    assert result['ok']
    assert 'Logged' in result['assistant'] and 'Accept this recipe' not in result['assistant']
    assert send(shared, text, request_id) == result
    assert 'Remaining 3/4' in shared.snapshot()['batches']
    assert shared.service.daily_summary('today').data['events'] == 1


def test_two_box_meal_reference_and_clickable_choices_survive_worker_change(shared):
    first = save_batch(shared, 2)
    latest = save_batch(shared, 2)
    text = 'I ate one box from the 2-box curry batch for lunch.'
    assert send(shared, text)['ok']
    assert shared.service.daily_summary('today').data['events'] == 0
    other = WebWorkspace(shared.store)
    choice = other.snapshot()['meal_choices']
    assert {row['id'] for row in choice['options']} == {first, latest}
    assert not send(other, f'/meal-select wrong-token {first}')['ok']
    assert send(other, f'/meal-select {choice["id"]} {first}')['ok']
    assert not send(other, f'/meal-select {choice["id"]} {latest}')['ok']
    assert shared.service.find_events({'batch_id': first, 'meal': 'lunch'}).data['candidates']
    result = send(shared, 'I ate one box from the 2-box curry batch I just saved.')
    assert result['ok'] and 'Logged' in result['assistant']
    assert shared.service.find_events({'batch_id': latest}).data['candidates']
    assert shared.service.daily_summary('today').data['events'] == 2
