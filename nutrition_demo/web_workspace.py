"""Persistent shared-demo coordinator; no network calls at import time."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from hashlib import sha256
from pathlib import Path
import hmac
import json
import os
import re
from psycopg import sql
from .agent import DemoAgent, Settings
from .catalog import Catalog, DATA_DIR
from .models import encode
from .service import NutritionService
from .store import Store, TABLES, jsonb
from . import web_display
from .web_meals import WebMeals
from .web_planning import WebWorkflow

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
        self.workflow = WebWorkflow(self.service, SESSION, self.fixture)
        self.meals = WebMeals(self.service, self.workflow)

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

    def agent(self, *, use_model=True):
        client = None
        if use_model and os.environ.get('WEB_ENABLE_MODEL_CALLS') == 'true':
            from openai import OpenAI
            client = OpenAI(max_retries=0, timeout=45)
        return DemoAgent(self.service, self.workflow, client, Settings.from_env())

    def snapshot(self):
        with self.store.transaction() as c:
            messages = c.execute('SELECT data FROM (SELECT id,data FROM web_messages ORDER BY id DESC LIMIT 100) recent ORDER BY id').fetchall()
        state = self.workflow.load()
        pending = self.workflow.command('/preview') if state.get('pending_preview') else None
        profile = self.service.get_profile().data
        daily = self.service.daily_summary('today').data
        return encode({'synthetic': True, 'model_enabled': os.environ.get('WEB_ENABLE_MODEL_CALLS') == 'true',
                       'messages': [r['data'] for r in messages], 'stage': state['stage'],
                       'inventory': web_display.inventory(self.workflow, self.service.list_inventory().data['items']),
                       'batches': web_display.batches(self.service.batch_summary().data['batches']),
                       'daily': web_display.daily(daily),
                       'goals': {'meal': profile.get('meal_targets'), 'daily': profile.get('daily_targets')},
                       'recipe_options': [
                           self.workflow.recipe_option(key) for key in self.service.catalog.recipes
                       ] if state['stage'] == 'recipe_selection' else [],
                       'selected_recipe': self.workflow.recipe_option(state.get('selected_recipe')),
                       'preview': web_display.preview(self.workflow, pending.data) if pending and pending.ok else None,
                       'proposal': self.workflow.proposal_view(state),
                       'meal_choices': self.meals.choices(state)})

    def local_action(self, message, request_id):
        if self.meals.recognizes(message):
            return partial(self.meals.handle, message, request_id)
        cook = message.split()
        if cook[:1] == ['/cook-plan']:
            return partial(self.workflow.cook_plan, cook[1] if len(cook) == 2 else None)
        adjustment = re.fullmatch(r'/adjust(?: ([1-9][0-9]?))?', message)
        if adjustment:
            boxes = int(adjustment[1] or self.fixture['reference_plan']['boxes'])
            if os.environ.get('WEB_ENABLE_MODEL_CALLS') == 'true' and not self.workflow.has_reference_goals():
                return partial(self.adjust_with_model, boxes)
            return partial(self.workflow.adjust, boxes)
        purchase = re.fullmatch(r'/purchase chicken ([1-9][0-9]{0,5})', message) or re.fullmatch(
            r'i bought ([1-9][0-9]{0,5})\s*(?:g|grams) of (?:raw )?chicken breast[.!]?',
            message, re.IGNORECASE,
        )
        if purchase:
            return partial(self.workflow.purchase_chicken, int(purchase[1]))
        if not message.startswith('/'):
            choice = self.workflow.selection_command(message)
            if choice:
                return partial(self.workflow.command, choice)
        return None

    def adjust_with_model(self, boxes):
        return self.agent().handle(f'Adjust the selected recipe to my saved per-meal goals for {boxes} boxes. '
                                   'Calculate a proposal using the current inventory and show the result.')

    @staticmethod
    def message_label(message):
        if not message.startswith('/'):
            return message
        return {
            '/confirm': 'Approved changes', '/reject': 'Rejected changes',
            '/meal-select': 'Chose a batch', '/accept-recipe': 'Accepted recipe',
            '/select': 'Selected recipe', '/purchase': 'Added a purchase for review',
            '/new-plan': 'Started a new plan', '/inventory-ok': 'Confirmed inventory',
            '/adjust': 'Adjust to my goals', '/cook-plan': 'Used the plan as written',
            '/inventory': 'View ingredients', '/batches': 'View batches', '/daily': 'View nutrition',
        }.get(message.split()[0], message)

    def submit(self, request_id, message):
        digest = sha256(message.encode()).hexdigest()
        with self.store.transaction() as c:
            prior = c.execute('SELECT input_hash,data FROM web_messages WHERE request_id=%s', (request_id,)).fetchone()
        if prior:
            if not hmac.compare_digest(prior['input_hash'], digest):
                raise WorkspaceError(409, 'Request ID already used for another message.')
            return prior['data']
        action = self.local_action(message, request_id)
        if not action and not message.startswith('/') and os.environ.get('WEB_ENABLE_MODEL_CALLS') != 'true':
            raise WorkspaceError(503, 'Model calls are disabled. You can still use the demo actions.')
        # Reserve before invoking the agent: if a worker dies after a meal commits,
        # retrying this HTTP request must never execute that meal a second time.
        label = self.message_label(message)
        reserved = {'request_id': request_id, 'user': label, 'ok': False, 'activity': [],
                    'assistant': 'This request started but has no final receipt. Inspect the shared records before sending another request. Use /retry to recover a completed meal if available.'}
        with self.store.transaction() as c:
            c.execute('INSERT INTO web_messages(request_id,input_hash,data) VALUES(%s,%s,%s)',
                      (request_id, digest, jsonb(reserved)))
        result = action() if action else self.agent(use_model=not message.startswith('/')).handle(message)
        record = {'request_id': request_id, 'user': label, 'assistant': web_display.reply(self.workflow, result, message), 'ok': result.ok,
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

