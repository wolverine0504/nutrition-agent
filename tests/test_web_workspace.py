"""No network, database, or paid model needed for coordinator contract checks."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from nutrition_demo.models import Result
from nutrition_demo.web_workspace import WebWorkspace, WorkspaceError


class FakeStore:
    schema = 'web_demo_test'
    def __init__(self):
        self.rows = {}
        self.locked = True
    @contextmanager
    def transaction(self, **kwargs):
        yield self
    def execute(self, statement, args=()):
        self.row = None
        if 'pg_try_advisory_xact_lock' in statement:
            self.row = {'locked': self.locked}
        elif statement.startswith('SELECT input_hash'):
            self.row = self.rows.get(args[0])
        elif statement.startswith('INSERT INTO web_messages'):
            self.rows[args[0]] = {'input_hash': args[1], 'data': args[2].obj}
        elif statement.startswith('UPDATE web_messages'):
            self.rows[args[1]]['data'] = args[0].obj
        else:
            raise AssertionError(statement)
        return self
    def fetchone(self):
        return self.row


@pytest.fixture
def workspace(monkeypatch):
    monkeypatch.delenv('WEB_ENABLE_MODEL_CALLS', raising=False)
    ws = WebWorkspace.__new__(WebWorkspace)
    ws.store = FakeStore()
    ws.workflow = Mock()
    ws.workflow.selection_command.return_value = None
    ws.meals = Mock()
    ws.meals.recognizes.return_value = False
    ws.agent = Mock(return_value=SimpleNamespace(handle=Mock(return_value=Result(True, {'message': 'Done'}))))
    return ws


def test_offline_natural_language_never_constructs_client(workspace):
    with pytest.raises(WorkspaceError) as error:
        workspace.submit('r1', 'I ate lunch')
    assert error.value.status_code == 503
    workspace.agent.assert_not_called()
    assert not workspace.store.rows


def test_commands_persist_receipt_and_duplicate_is_not_reexecuted(workspace):
    result = workspace.submit('r1', '/inventory')
    assert result['assistant'] == 'Done'
    assert workspace.submit('r1', '/inventory') == result
    workspace.agent.return_value.handle.assert_called_once_with('/inventory')


def test_idempotency_key_cannot_change_message(workspace):
    workspace.submit('r1', '/inventory')
    with pytest.raises(WorkspaceError) as error:
        workspace.submit('r1', '/batches')
    assert error.value.status_code == 409
    assert workspace.agent.call_count == 1


def test_crash_receipt_prevents_reexecution(workspace):
    workspace.agent.return_value.handle.side_effect = RuntimeError('worker crashed after commit')
    with pytest.raises(RuntimeError):
        workspace.submit('r1', '/inventory')
    result = workspace.submit('r1', '/inventory')
    assert not result['ok']
    assert 'Inspect the shared records' in result['assistant']
    assert workspace.agent.call_count == 1


def test_busy_database_lock_refuses_second_operation(workspace):
    workspace.store.locked = False
    with pytest.raises(WorkspaceError) as error:
        with workspace.exclusive():
            pytest.fail('Must not enter while busy')
    assert error.value.status_code == 409


def test_dedicated_schema_required():
    with pytest.raises(ValueError, match='dedicated'):
        WebWorkspace(SimpleNamespace(schema='public'))


def test_only_explicit_true_enables_model_calls(workspace, monkeypatch):
    monkeypatch.setenv('WEB_ENABLE_MODEL_CALLS', '1')
    with pytest.raises(WorkspaceError):
        workspace.submit('r1', 'hello')
    workspace.agent.assert_not_called()


def test_api_key_alone_does_not_construct_model_client(monkeypatch):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY', 'not-a-real-key')
    monkeypatch.delenv('WEB_ENABLE_MODEL_CALLS', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_REASONING_EFFORT', raising=False)
    constructor = Mock(side_effect=AssertionError('A model client must not be constructed'))
    monkeypatch.setattr(openai, 'OpenAI', constructor)
    ws = WebWorkspace.__new__(WebWorkspace)
    ws.service, ws.workflow = Mock(), Mock()
    assert ws.agent().client is None
    constructor.assert_not_called()


def test_public_receipt_excludes_raw_tool_arguments_and_trace(workspace):
    workspace.agent.return_value.handle.return_value = Result(True, {
        'message': 'Saved.', 'history': ['private model context'], 'run_id': 'internal',
        'tool_results': [{'name': 'list_inventory', 'arguments': {'private': 'arguments'},
                          'result': {'ok': True, 'data': {}, 'error': None}}]})
    receipt = workspace.submit('r1', '/inventory')
    assert receipt['activity'] == [{'name': 'list_inventory', 'ok': True}]
    assert 'private' not in str(receipt)
    assert 'run_id' not in receipt


def test_inventory_tool_reply_uses_food_names(workspace):
    workspace.workflow.ingredient_name.return_value = 'Curry roux'
    workspace.agent.return_value.handle.return_value = Result(True, {
        'message': 'Results from the shared application service follow.',
        'tool_results': [{
            'name': 'list_inventory',
            'result': {'ok': True, 'data': {'items': [{
                'ingredient_id': 'curry_block', 'quantity': '92', 'unit': 'g',
                'state': 'packaged', 'source': {'status': 'synthetic'},
            }]}, 'error': None},
        }],
    })
    receipt = workspace.submit('r1', '/inventory')
    assert receipt['assistant'] == 'Curry roux: 92 g'


def test_web_migrations_do_not_modify_frozen_core_manifest():
    from nutrition_demo.store import Store
    from nutrition_demo.web_workspace import WebStore
    core = Store('dbname=unused')._migrations()
    web = WebStore('dbname=unused')._migrations()
    assert set(web) == set(core) | {'web_001_messages.sql'}
    assert all(web[name] == value for name, value in core.items())
