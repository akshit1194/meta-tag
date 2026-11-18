import os
import secrets
import sqlite3
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, Request, current_app
from werkzeug.security import generate_password_hash

from .db import close_db, get_db, migrate


class PrivateUploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        # Werkzeug normally spills multipart files to the OS temp directory.
        # Keep that first, pre-route copy within the private data directory too.
        return tempfile.SpooledTemporaryFile(max_size=512 * 1024, mode='w+b',
                                            dir=current_app.config['DATA_ROOT'] / 'tmp')


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.request_class = PrivateUploadRequest
    root = Path(os.environ.get('PHOTO_DATA_DIR', app.instance_path)).resolve()
    app.config.from_mapping(
        DATA_ROOT=root,
        DATABASE=str(root / 'photos.sqlite3'),
        SECRET_KEY=os.environ.get('SECRET_KEY'),
        MAX_UPLOAD_BYTES=20 * 1024 * 1024,
        MAX_CONTENT_LENGTH=20 * 1024 * 1024 + 64 * 1024,
        # Werkzeug also applies this limit to a 64 KiB multipart read plus
        # retained boundary bytes. A 64 KiB cap rejects ordinary photo parts.
        # Give its buffer headroom; MAX_UPLOAD_BYTES still caps each photo.
        MAX_FORM_MEMORY_SIZE=512 * 1024,
        MAX_FORM_PARTS=10,
        MAX_IMAGE_PIXELS=40_000_000,
        WORKER_TIMEOUT=12,
        WORKER_MEMORY_MB=768,
        EXIFTOOL_PATH=os.environ.get('EXIFTOOL_PATH', ''),
        RETAIN_ORIGINALS=os.environ.get('RETAIN_ORIGINALS', 'true').lower() == 'true',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'false').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if test_config:
        app.config.update(test_config)
    root = Path(app.config['DATA_ROOT']).resolve()
    app.config['DATA_ROOT'] = root
    # All source files, temporary uploads, and derivatives stay outside /static.
    for folder in (root, root / 'originals', root / 'display', root / 'tmp'):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not app.config['SECRET_KEY']:
        key_path = root / 'secret.key'
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_hex(32))
        app.config['SECRET_KEY'] = key_path.read_text().strip()
    app.extensions['photo_slots'] = threading.BoundedSemaphore(2)
    app.teardown_appcontext(close_db)

    from . import auth, routes
    auth.init_app(app)
    app.register_blueprint(routes.bp)

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; "
            "script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        if not response.headers.get('Cache-Control', '').startswith('public'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.cli.command('init-db')
    def init_db():
        """Apply database migrations (idempotent)."""
        migrate()
        click.echo('Database migrations applied.')

    @app.cli.command('create-user')
    @click.option('--username', prompt=True)
    @click.option('--admin', is_flag=True)
    @click.password_option(confirmation_prompt=True)
    def create_user(username, admin, password):
        """Create an upload account; use --admin for metadata access."""
        from .metadata import server_now
        username = username.strip()
        if not 1 <= len(username) <= 80 or len(password) < 12:
            raise click.ClickException('Use a username of 1–80 characters and a password of at least 12 characters.')
        db = get_db()
        try:
            db.execute(
                'INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)',
                (username, generate_password_hash(password), 'admin' if admin else 'user', server_now()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            raise click.ClickException('That username already exists.') from None
        click.echo('Account created.')

    return app
