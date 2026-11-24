"""Shared synthetic presentation workspace. Live model access is opt-in only."""
import hmac
import os
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .web_workspace import WebWorkspace, WorkspaceError, WebStore

STATIC = Path(__file__).parent / 'static'


class Message(BaseModel):
    request_id: UUID
    message: str = Field(min_length=1, max_length=4000)


class Decision(BaseModel):
    request_id: UUID
    preview_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    approve: bool


def create_app(workspace=None):
    app = FastAPI(title='Nutrition Agent Demo', docs_url=None, redoc_url=None, openapi_url=None)

    def get_workspace():
        nonlocal workspace
        if workspace is None:
            schema = os.environ.get('WEB_DATABASE_SCHEMA', '')
            if not schema.startswith('web_demo_'):
                raise HTTPException(503, 'Configure a dedicated web_demo_ database schema first.')
            store = WebStore(os.environ.get('DATABASE_URL', ''), schema)
            store.check_schema()
            workspace = WebWorkspace(store)
        return workspace

    def authorize(code, name):
        interaction = os.environ.get('WEB_INTERACTION_CODE', '')
        presenter = os.environ.get('WEB_PRESENTER_CODE', '')
        if interaction and interaction == presenter:
            raise HTTPException(503, 'Configure distinct interaction and presenter codes.')
        expected = os.environ.get(name, '')
        if len(expected) < 12 or not code or not hmac.compare_digest(code.encode(), expected.encode()):
            raise HTTPException(403, 'A valid access code is required.')

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(WorkspaceError)
    async def workspace_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={'detail': 'Operation interrupted. Inspect shared records before retrying; no automatic retry was made.'})

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/state')
    def state():
        ws = get_workspace()
        with ws.exclusive():
            return ws.snapshot()

    @app.post('/api/messages')
    def message(body: Message, x_interaction_code: str | None = Header(default=None)):
        authorize(x_interaction_code, 'WEB_INTERACTION_CODE')
        text = body.message.strip()
        if not text:
            raise HTTPException(422, 'Enter a message.')
        ws = get_workspace()
        with ws.exclusive():
            return ws.submit(str(body.request_id), text)

    @app.post('/api/decisions')
    def decision(body: Decision, x_interaction_code: str | None = Header(default=None)):
        authorize(x_interaction_code, 'WEB_INTERACTION_CODE')
        ws = get_workspace()
        with ws.exclusive():
            return ws.submit(str(body.request_id), ('/confirm ' if body.approve else '/reject ') + body.preview_id)

    @app.post('/api/reset')
    def reset(x_presenter_code: str | None = Header(default=None)):
        authorize(x_presenter_code, 'WEB_PRESENTER_CODE')
        ws = get_workspace()
        with ws.exclusive():
            ws.reset()
            return ws.snapshot()

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
