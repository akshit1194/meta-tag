import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def close_db(_error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def migrate():
    db = get_db()
    db.execute('PRAGMA journal_mode = WAL')
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        sql = Path(__file__).resolve().parent.parent / 'migrations' / '001_initial.sql'
        db.executescript(sql.read_text(encoding='utf-8'))
