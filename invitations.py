"""Owner-only invitation management. Raw access tokens are never stored."""
import hashlib
import secrets
import time


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS invitations (
      id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, label TEXT, created REAL,
      expires REAL, revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS admin_sessions (
      token_hash TEXT PRIMARY KEY, key_hash TEXT, expires REAL);
    CREATE TABLE IF NOT EXISTS referral_links (
      token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created REAL);
    CREATE TABLE IF NOT EXISTS referral_claims (
      friend_id TEXT PRIMARY KEY, referrer_id TEXT NOT NULL, claimed REAL,
      rewarded REAL);
    CREATE INDEX IF NOT EXISTS referral_owner ON referral_claims(referrer_id);
    ''')


def create_referral(store, sid):
    with store.lock, store.db() as db:
        owner = store.participant(db, sid)
        if not owner or not store.invited(db, owner):
            raise Problem(403, 'Сначала примите приглашение, чтобы получить свою ссылку.')
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO referral_links VALUES (?,?,?)', (digest(token), owner, time.time()))
    return {'token': token}


def claim_referral(store, sid, token):
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        raise Problem(403, 'Реферальная ссылка недействительна.')
    with store.lock, store.db() as db:
        row = db.execute('SELECT owner_id FROM referral_links WHERE token_hash=?', (digest(token),)).fetchone()
        if not row:
            raise Problem(403, 'Реферальная ссылка недействительна.')
        referrer = row['owner_id']
        friend = store.participant(db, sid)
        if not friend:
            friend = digest(secrets.token_urlsafe(32))
            db.execute('UPDATE sessions SET participant=? WHERE id=?', (friend, sid))
        if friend == referrer:
            raise Problem(400, 'Нельзя применить собственную ссылку.')
        existing = db.execute('SELECT referrer_id FROM referral_claims WHERE friend_id=?', (friend,)).fetchone()
        if existing and existing['referrer_id'] != referrer:
            raise Problem(409, 'Для этого пользователя уже сохранено другое приглашение.')
        if not existing:
            if not store.invited(db, referrer):
                raise Problem(403, 'Приглашение отправителя больше не действует.')
            if store.used(db, sid):
                raise Problem(409, 'Реферальная ссылка действует до первой генерации.')
            db.execute('INSERT INTO referral_claims (friend_id,referrer_id,claimed) VALUES (?,?,?)',
                       (friend, referrer, time.time()))
    return {'claimed': True}


def reward_first_generation(db, friend):
    if friend:
        db.execute('UPDATE referral_claims SET rewarded=? WHERE friend_id=? AND rewarded IS NULL', (time.time(), friend))


def active(db, participant):
    return bool(participant and db.execute(
        'SELECT 1 FROM invitations WHERE token_hash=? AND revoked=0 AND expires>?',
        (participant, time.time())).fetchone())


def login(store, key):
    if not isinstance(key, str) or len(key) > 128 or not store.admin_hash or not secrets.compare_digest(digest(key), store.admin_hash):
        raise Problem(403, 'Неверный ключ владельца.')
    token = secrets.token_urlsafe(32)
    with store.db() as db:
        db.execute('DELETE FROM admin_sessions WHERE expires<?', (time.time(),))
        db.execute('INSERT INTO admin_sessions VALUES (?,?,?)',
                   (digest(token), store.admin_hash, time.time()+8*3600))
    return token


def authorize(store, token):
    with store.db() as db:
        row = db.execute('SELECT key_hash,expires FROM admin_sessions WHERE token_hash=?', (digest(token),)).fetchone()
    if not store.admin_hash or not row or row['expires'] <= time.time() or not secrets.compare_digest(row['key_hash'], store.admin_hash):
        raise Problem(401, 'Войдите с ключом владельца.')


def listing(store):
    with store.db() as db:
        rows = db.execute('''SELECT i.id,i.label,i.created,i.expires,i.revoked,
          (SELECT count(*) FROM attempts a JOIN sessions s ON a.session=s.id WHERE s.participant=i.token_hash) AS used
          FROM invitations i ORDER BY i.created DESC''').fetchall()
        used = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
        referrals = db.execute('''SELECT c.referrer_id, COUNT(*) AS invited,
          SUM(CASE WHEN c.rewarded IS NOT NULL THEN 1 ELSE 0 END) AS activated
          FROM referral_claims c GROUP BY c.referrer_id ORDER BY MAX(c.claimed) DESC''').fetchall()
        bonus = sum(int(row['activated'] or 0) * 5 for row in referrals)
    return {'invitations': [dict(row) for row in rows], 'remaining_total': max(0, store.total_limit+bonus-used),
            'total_limit': store.total_limit+bonus, 'per_invite': store.session_limit,
            'referrals': [{**dict(row), 'reward_generations': int(row['activated'] or 0)*5} for row in referrals]}


def create(store, label):
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
        raise Problem(400, 'Укажите имя или заметку — до 60 символов.')
    token, iid, now = secrets.token_urlsafe(32), secrets.token_hex(16), time.time()
    with store.lock, store.db() as db:
        if db.execute('SELECT count(*) FROM invitations').fetchone()[0] >= 200:
            raise Problem(409, 'Достигнут лимит списка приглашений.')
        if db.execute('SELECT count(*) FROM attempts').fetchone()[0] >= store.total_limit + store.referral_bonus(db):
            raise Problem(409, 'Общий лимит примерок исчерпан. Новая ссылка его не увеличит.')
        db.execute('INSERT INTO invitations (id,token_hash,label,created,expires) VALUES (?,?,?,?,?)',
                   (iid, digest(token), label.strip(), now, now+7*86400))
    return {'id': iid, 'token': token, 'expires': now+7*86400}


def revoke(store, iid):
    with store.lock, store.db() as db:
        if not db.execute('SELECT 1 FROM invitations WHERE id=?', (iid,)).fetchone():
            raise Problem(404, 'Приглашение не найдено.')
        db.execute('UPDATE invitations SET revoked=1 WHERE id=?', (iid,))
    return {'revoked': True}
