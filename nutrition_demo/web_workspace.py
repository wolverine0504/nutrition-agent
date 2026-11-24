"""Persistent shared-demo coordinator; no network calls at import time."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path
import hmac
import json
import os
from psycopg import sql
from .agent import DemoAgent, Settings
from .catalog import Catalog, DATA_DIR
from .display import render
from .models import encode
from .service import NutritionService
from .store import Store, TABLES, jsonb
from .workflow import Workflow

SESSION = 'web-shared'
RESET_CONNECTION = ContextVar('web_reset_connection', default=None)


class WebStore(Store):
    @contextmanager
    def atomic_reset(self):
        with super().transaction(lock=True) as connection:
            token = RESET_CONNECTION.set((self, connection))
            try:
                yield connection
            finally:
                RESET_CONNECTION.reset(token)

    @contextmanager
    def transaction(self, *, lock=False):
        reset = RESET_CONNECTION.get()
        if reset is not None and reset[0] is self:
            connection = reset[1]
            # The outer transaction already holds the state lock.
            with connection.transaction():
                yield connection
        else:
            with super().transaction(lock=lock) as connection:
                yield connection

    def _migrations(self):
        # Keep presentation-only tables out of frozen evaluation manifests.
        return {**super()._migrations(), **{
            p.name: (sha256(p.read_bytes()).hexdigest(), p.read_text())
            for p in sorted((Path(__file__).parent / 'web_migrations').glob('*.sql'))
        }}


class WorkspaceError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class WebWorkspace:
    def __init__(self, store):
        if not store.schema.startswith('web_demo_'):
            raise ValueError('The web demo requires a dedicated web_demo_ schema')
        self.store = store
        # Catalog and seed are synthetic and independent of personal data.
        self.fixture = json.loads((DATA_DIR / 'web-scenario.json').read_text())
        self.service = NutritionService(store, Catalog(self.fixture['catalog']))
        self.workflow = Workflow(self.service, SESSION)

    @contextmanager
    def exclusive(self):
        # Separate connection holds the lock across the agent's short transactions.
        # PostgreSQL releases it even if a worker crashes. It works across replicas.
        with self.store.transaction() as c:
            locked = c.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS locked',
                               ('nutrition-web:' + self.store.schema,)).fetchone()['locked']
            if not locked:
                raise WorkspaceError(409, 'Another operation is running. Wait and retry.')
            yield

    def agent(self):
        client = None
        if os.environ.get('WEB_ENABLE_MODEL_CALLS') == 'true':
            from openai import OpenAI
            client = OpenAI(max_retries=0, timeout=45)
        return DemoAgent(self.service, self.workflow, client, Settings.from_env())

    def snapshot(self):
        with self.store.transaction() as c:
            messages = c.execute('SELECT data FROM (SELECT id,data FROM web_messages ORDER BY id DESC LIMIT 100) recent ORDER BY id').fetchall()
        state = self.workflow.load()
        pending = self.workflow.command('/preview') if state.get('pending_preview') else None
        proposal = state.get('proposal')
        return encode({'synthetic': True, 'model_enabled': os.environ.get('WEB_ENABLE_MODEL_CALLS') == 'true',
                       'messages': [r['data'] for r in messages], 'stage': state['stage'],
                       'inventory': render(self.service.list_inventory()),
                       'batches': render(self.service.batch_summary()),
                       'daily': render(self.service.daily_summary('today')),
                       'preview': {'id': pending.data['id'], 'text': render(pending)} if pending and pending.ok else None,
                       'proposal': {'id': proposal['id'], 'passed': proposal['calculation']['targets']['passed']} if proposal else None})

    def submit(self, request_id, message):
        digest = sha256(message.encode()).hexdigest()
        with self.store.transaction() as c:
            prior = c.execute('SELECT input_hash,data FROM web_messages WHERE request_id=%s', (request_id,)).fetchone()
        if prior:
            if not hmac.compare_digest(prior['input_hash'], digest):
                raise WorkspaceError(409, 'Request ID already used for another message.')
            return prior['data']
        if not message.startswith('/') and os.environ.get('WEB_ENABLE_MODEL_CALLS') != 'true':
            raise WorkspaceError(503, 'Model calls are disabled. Offline commands remain available.')
        # Reserve before invoking the agent: if a worker dies after a meal commits,
        # retrying this HTTP request must never execute that meal a second time.
        reserved = {'request_id': request_id, 'user': message, 'ok': False, 'activity': [],
                    'assistant': 'This request started but has no final receipt. Inspect the shared records before sending another request. Use /retry to recover a completed meal if available.'}
        with self.store.transaction() as c:
            c.execute('INSERT INTO web_messages(request_id,input_hash,data) VALUES(%s,%s,%s)',
                      (request_id, digest, jsonb(reserved)))
        result = self.agent().handle(message)
        record = {'request_id': request_id, 'user': message, 'assistant': render(result), 'ok': result.ok,
                  'activity': [{'name': r['name'], 'ok': r['result']['ok']} for r in result.data.get('tool_results', [])]}
        with self.store.transaction() as c:
            c.execute('UPDATE web_messages SET data=%s WHERE request_id=%s',
                      (jsonb(record), request_id))
        return record

    def reset(self):
        # This method can only be constructed against an explicitly dedicated schema.
        tables = sql.SQL(',').join(sql.Identifier(t) for t in (*TABLES, 'web_messages'))
        with self.store.atomic_reset() as c:
            c.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY').format(tables))
            c.execute('UPDATE state_meta SET version=version+1 WHERE id=1')
            self.store.seed(self.fixture)
            # Prepare one useful batch without calling any model.
            p = self.service.preview_preparation('curry', [
                {'ingredient_id': 'fixture_mix', 'quantity': '800', 'unit': 'g', 'state': 'synthetic'}], [
                {'name': 'Synthetic curry', 'boxes': 8, 'allocations': [
                    {'cooking_ingredient_id': 'fixture_mix', 'quantity': '800', 'unit': 'g'}]}],
                self.service._day('today'))
            if not p.ok or not self.service.confirm_preview(p.data['id'], 'web-fixture-prepare').ok:
                raise RuntimeError('Synthetic fixture preparation failed')

