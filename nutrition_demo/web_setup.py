"""Initialize an empty, dedicated synthetic web workspace without model calls."""
import os
import psycopg
from psycopg import sql
from .web_workspace import WebWorkspace, WebStore


def main():
    schema = os.environ.get('WEB_DATABASE_SCHEMA', '')
    if not schema.startswith('web_demo_'):
        raise ValueError('Set WEB_DATABASE_SCHEMA to a dedicated web_demo_ schema')
    dsn = os.environ.get('DATABASE_URL', '')
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
    store = WebStore(dsn, schema)
    store.migrate()
    workspace = WebWorkspace(store)
    with workspace.exclusive():
        if any(store.info()['counts'].values()):
            raise ValueError('Workspace is already initialized; use the presenter reset control')
        workspace.reset()
    print('Synthetic web workspace initialized. No model calls made.')


if __name__ == '__main__':
    main()
