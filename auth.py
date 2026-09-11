"""Expiring server-side sessions. Persist token hashes only."""
import hashlib
import hmac
import secrets
import time
import sqlite3
from fastapi import HTTPException, Request
import config
import device_store

def init():
    with device_store._conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          username TEXT PRIMARY KEY, full_name TEXT NOT NULL DEFAULT '',
          password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('ADMIN','OPERATOR','VIEWER')),
          enabled INTEGER NOT NULL DEFAULT 1, can_export INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_limits (key TEXT PRIMARY KEY, attempts INTEGER, until REAL);
        ''')

def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12 to 256 characters')
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${value}'

def verify(password, encoded):
    try:
        _, salt, expected = encoded.split('$')
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

DUMMY_HASH = password_hash(secrets.token_urlsafe(24))
def login(username, password, remote):
    now = time.time()
    with device_store._conn() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM login_limits WHERE until < ?', (now,))
        attempt = c.execute('SELECT attempts FROM login_limits WHERE key=?', (remote,)).fetchone()
        if attempt and attempt[0] >= 10:
            raise HTTPException(429, 'Too many attempts. Try again in 15 minutes.')
        c.execute('INSERT INTO login_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1', (remote, now + 900))
        row = c.execute('SELECT password_hash,enabled FROM users WHERE username=?', (username,)).fetchone()
    if not verify(password, row[0] if row else DUMMY_HASH) or not row or not row[1]:
        raise HTTPException(401, 'Invalid username or password')
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with device_store._conn() as c:
        c.execute('DELETE FROM sessions WHERE expires < ?', (now,))
        c.execute('DELETE FROM login_limits WHERE key=?', (remote,))
        c.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), username, csrf, now + config.SESSION_SECONDS))
    return token, csrf

def current(request: Request):
    token = request.cookies.get('session', '')
    with device_store._conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute('''SELECT u.username,u.full_name,u.role,u.can_export,s.csrf FROM sessions s
          JOIN users u ON u.username=s.username WHERE s.token_hash=? AND s.expires>? AND u.enabled=1''',
          (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not row:
        raise HTTPException(401, 'Session expired; please sign in')
    user = dict(row)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), user['csrf']):
            raise HTTPException(403, 'Invalid CSRF token')
    return user

def admin(request: Request):
    user = current(request)
    if user['role'] != 'ADMIN':
        raise HTTPException(403, 'Administrator access required')
    return user

def save_user(username, full_name, password, role, enabled, can_export, delete=False):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
        raise ValueError('Invalid username')
    if role not in ('ADMIN','OPERATOR','VIEWER'):
        raise ValueError('Invalid role')
    encoded = password_hash(password) if password else None
    with device_store._conn() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT role,enabled FROM users WHERE username=?', (username,)).fetchone()
        if old and old[0] == 'ADMIN' and old[1] and (delete or not enabled or role != 'ADMIN'):
            if c.execute("SELECT count(*) FROM users WHERE role='ADMIN' AND enabled=1").fetchone()[0] <= 1:
                raise ValueError('Cannot remove the last active administrator')
        if delete:
            c.execute('DELETE FROM users WHERE username=?', (username,))
        elif old:
            c.execute('UPDATE users SET full_name=?,role=?,enabled=?,can_export=?,password_hash=COALESCE(?,password_hash) WHERE username=?',
                      (full_name[:128], role, int(enabled), int(can_export), encoded, username))
        else:
            if not encoded:
                raise ValueError('Password is required for new users')
            c.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?)', (username, full_name[:128], encoded, role, int(enabled), int(can_export), time.time()))
        c.execute('DELETE FROM sessions WHERE username=?', (username,))

init()
