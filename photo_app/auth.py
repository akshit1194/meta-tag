import hashlib
import hmac
import secrets
import time
from functools import wraps

from flask import abort, current_app, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

# Equal-cost password check for nonexistent users; no default account/password.
DUMMY_HASH = generate_password_hash(secrets.token_hex(24))


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.user:
            if request.path.startswith('/api/'):
                return jsonify(error='AUTHENTICATION_REQUIRED'), 401
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user['role'] != 'admin':
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def init_app(app):
    @app.before_request
    def authenticate_and_csrf():
        g.user = None
        if session.get('user_id'):
            g.user = get_db().execute('SELECT id,username,role FROM users WHERE id=?', (session['user_id'],)).fetchone()
        session.setdefault('csrf_token', secrets.token_urlsafe(32))
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            if not hmac.compare_digest(str(supplied), session['csrf_token']):
                abort(400, description='Invalid or missing CSRF token. Reload the page and try again.')

    app.jinja_env.globals['csrf_token'] = lambda: session['csrf_token']

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip()[:80]
            password = request.form.get('password', '')[:1024]
            key = hmac.new(
                str(current_app.secret_key).encode(),
                (str(request.remote_addr) + '\0' + username.casefold()).encode(), hashlib.sha256,
            ).hexdigest()
            db = get_db()
            now = int(time.time())
            db.execute('DELETE FROM login_attempts WHERE window_start < ?', (now - 900,))
            attempt = db.execute('SELECT attempts FROM login_attempts WHERE key=?', (key,)).fetchone()
            if attempt and attempt['attempts'] >= 5:
                db.commit()
                return render_template('login.html', error='Too many attempts. Try again in 15 minutes.'), 429
            user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if check_password_hash(user['password_hash'] if user else DUMMY_HASH, password) and user:
                db.execute('DELETE FROM login_attempts WHERE key=?', (key,))
                db.commit()
                session.clear()
                session.update(user_id=user['id'], csrf_token=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect(url_for('photos.index'))
            db.execute(
                'INSERT INTO login_attempts(key,attempts,window_start) VALUES (?,1,?) '
                'ON CONFLICT(key) DO UPDATE SET attempts=attempts+1', (key, now),
            )
            db.commit()
            error = 'Username or password is incorrect.'
        return render_template('login.html', error=error)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))
