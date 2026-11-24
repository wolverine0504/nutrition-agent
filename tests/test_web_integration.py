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
    assert 'Synthetic curry' in reopened.snapshot()['batches']
    with reopened.exclusive():
        reopened.reset()
    assert reopened.snapshot()['messages'] == []
    assert reopened.service.batch_summary().data['batches'][0]['remaining_boxes'] == 8


def test_separate_connections_cannot_enter_same_workspace(shared):
    reopened = WebWorkspace(shared.store)
    with shared.exclusive():
        with pytest.raises(WorkspaceError) as error:
            with reopened.exclusive():
                pytest.fail('Should be busy')
        assert error.value.status_code == 409
    with reopened.exclusive():
        pass


def test_preview_requires_current_id_and_explicit_decision(shared):
    batch = shared.service.batch_summary().data['batches'][0]['id']
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
    assert shared.service.batch_summary().data['batches'][0]['remaining_boxes'] == 7


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
