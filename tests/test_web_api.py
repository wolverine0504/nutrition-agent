"""HTTP contract tests; use an injected workspace, never a model or personal DB."""
from contextlib import nullcontext
from unittest.mock import Mock
from uuid import uuid4
import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from nutrition_demo.web import create_app


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv('WEB_INTERACTION_CODE', 'audience-test-code')
    monkeypatch.setenv('WEB_PRESENTER_CODE', 'presenter-test-code')
    workspace = Mock()
    workspace.exclusive.side_effect = lambda: nullcontext()
    workspace.snapshot.return_value = {'synthetic': True, 'messages': []}
    workspace.submit.return_value = {'ok': True}
    return TestClient(create_app(workspace)), workspace


def test_public_view_and_assets_do_not_require_code(http):
    client, workspace = http
    assert client.get('/api/state').json()['synthetic']
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/docs').status_code == 404
    assert client.get('/api/state').headers['cache-control'] == 'no-store'
    assert 'frame-ancestors' in client.get('/').headers['content-security-policy']
    workspace.submit.assert_not_called()


@pytest.mark.parametrize('code', [None, 'wrong', '觀眾代碼'])
def test_messages_require_valid_code(http, code):
    client, workspace = http
    headers = {'X-Interaction-Code': code} if code and code.isascii() else {}
    response = client.post('/api/messages', json={'request_id': str(uuid4()), 'message': '/batches'}, headers=headers)
    assert response.status_code == 403
    workspace.submit.assert_not_called()


def test_message_and_decision_reuse_shared_workspace(http):
    client, workspace = http
    headers = {'X-Interaction-Code': 'audience-test-code'}
    key = str(uuid4())
    assert client.post('/api/messages', json={'request_id': key, 'message': ' /batches '}, headers=headers).status_code == 200
    workspace.submit.assert_called_with(key, '/batches')
    for approve, command in [(True, '/confirm '), (False, '/reject ')]:
        client.post('/api/decisions', json={'request_id': key, 'preview_id': 'current-preview', 'approve': approve}, headers=headers)
        workspace.submit.assert_called_with(key, command + 'current-preview')


def test_audience_cannot_reset(http):
    client, workspace = http
    assert client.post('/api/reset', headers={'X-Presenter-Code': 'audience-test-code'}).status_code == 403
    workspace.reset.assert_not_called()
    assert client.post('/api/reset', headers={'X-Presenter-Code': 'presenter-test-code'}).status_code == 200
    workspace.reset.assert_called_once()


def test_empty_message_or_bad_receipt_cannot_execute(http):
    client, workspace = http
    headers = {'X-Interaction-Code': 'audience-test-code'}
    for message in ['', '   ', 'x' * 4001]:
        assert client.post('/api/messages', json={'request_id': str(uuid4()), 'message': message}, headers=headers).status_code == 422
    assert client.post('/api/messages', json={'request_id': 'bad-id', 'message': '/batches'}, headers=headers).status_code == 422
    workspace.submit.assert_not_called()


def test_unexpected_errors_do_not_expose_internals(http):
    client, workspace = http
    workspace.snapshot.side_effect = RuntimeError('password=secret database internals')
    client = TestClient(create_app(workspace), raise_server_exceptions=False)
    response = client.get('/api/state')
    assert response.status_code == 503
    assert 'secret' not in response.text


def test_equal_codes_cannot_authorize_writes(http, monkeypatch):
    client, workspace = http
    monkeypatch.setenv('WEB_PRESENTER_CODE', 'audience-test-code')
    response = client.post('/api/reset', headers={'X-Presenter-Code': 'audience-test-code'})
    assert response.status_code == 503
    workspace.reset.assert_not_called()
