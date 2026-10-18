"""Local M1 prototype. Standard library HTTP + SQLite; deploy behind TLS for M2."""
import argparse
import base64
import hashlib
import io
import math
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import warnings
from collections import deque
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from PIL import Image, ImageOps, UnidentifiedImageError
from provider import GeminiProvider, GenerationError
from database import connect
import invitations
from invitations import Problem

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static' if (ROOT / 'static').is_dir() else ROOT
MAX_BODY = 24 * 1024 * 1024
MAX_GARMENTS = 6
CATEGORIES = ('Вещь', 'Футболка', 'Рубашка', 'Свитер', 'Худи', 'Куртка', 'Пальто',
              'Джинсы', 'Брюки', 'Шорты', 'Юбка', 'Платье', 'Костюм', 'Кроссовки',
              'Обувь', 'Кепка', 'Головной убор', 'Сумка', 'Очки', 'Аксессуар')
Image.MAX_IMAGE_PIXELS = 25_000_000
TTL = 24 * 3600


def normalize(blob):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(blob)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError()
                source.load()
                if min(source.size) < 128:
                    raise ValueError()
                # Resize before allocating RGBA and the white background so a
                # phone photo fits comfortably in a small free hosting instance.
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((1800, 1800))
                im = oriented.convert("RGBA")
                canvas = Image.new("RGB", im.size, "white")
                canvas.paste(im, mask=im.getchannel("A"))
                canvas.thumbnail((1800, 1800))
                out = io.BytesIO()
                canvas.save(out, "JPEG", quality=90)
                return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Problem(400, "Нужно фото JPG, PNG или WebP размером от 128 пикселей, до 25 мегапикселей.") from None


def decode_image(value):
    if not isinstance(value, str) or len(value) > 12 * 1024 * 1024:
        raise Problem(400, "Изображение слишком большое.")
    match = re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise Problem(400, "Загрузите фото JPG, PNG или WebP.")
    try:
        return normalize(base64.b64decode(match[1], validate=True))
    except ValueError:
        raise Problem(400, "Файл изображения повреждён.") from None


def shop_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise Problem(400, "Проверьте ссылку на магазин.")
    value = value.strip()
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or any(ord(c) < 33 for c in value):
            raise ValueError()
        url.port
    except ValueError:
        raise Problem(400, "Ссылка должна начинаться с https:// или http://.") from None
    return value


class Store:
    def __init__(self, path, provider=None, session_limit=5, total_limit=25, invite_hashes=None, database_url=None, admin_hash=None):
        self.path = str(path)
        self.database_url = database_url
        self.invite_hashes = set(invite_hashes or [])
        self.admin_hash = admin_hash
        self.provider = provider
        self.session_limit, self.total_limit = session_limit, total_limit
        self.lock = threading.RLock()
        self.stop = threading.Event()
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created REAL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, session TEXT, request_id TEXT, fingerprint TEXT,
              status TEXT, created REAL, elapsed REAL, person BLOB, garment BLOB,
              result BLOB, product_url TEXT, error TEXT, usage TEXT, feedback TEXT,
              UNIQUE(session, request_id));
            CREATE TABLE IF NOT EXISTS attempts (session TEXT, created REAL);
            """)
            db.executescript("""
            CREATE TABLE IF NOT EXISTS invited_activity (
              user_id TEXT PRIMARY KEY, active_seconds REAL DEFAULT 0, last_seen REAL);
            CREATE TABLE IF NOT EXISTS engagement_visits (
              id TEXT PRIMARY KEY, user_id TEXT, session TEXT, created REAL,
              last_seen REAL, sequence INTEGER);
            CREATE INDEX IF NOT EXISTS engagement_user ON engagement_visits(user_id);
            """)
            invitations.initialize(db)
            if database_url:
                db.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS participant TEXT")
            elif "participant" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN participant TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS sessions_participant ON sessions(participant)")
            # The owner archive survives the 24-hour session history and user deletion.
            db.executescript("""
            CREATE TABLE IF NOT EXISTS generation_archive (
              id TEXT PRIMARY KEY, user_id TEXT, session TEXT, status TEXT,
              created REAL, elapsed REAL, wait_elapsed REAL, person BLOB, garment BLOB,
              result BLOB, product_url TEXT, error TEXT, usage TEXT, feedback TEXT,
              model TEXT, prompt TEXT);
            CREATE INDEX IF NOT EXISTS archive_user_created ON generation_archive(user_id, created);
            """)
            for table in ('jobs', 'generation_archive'):
                if database_url:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS garments TEXT')
                elif 'garments' not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN garments TEXT')
            db.executescript("""
            CREATE TABLE IF NOT EXISTS clothing_classifications (
              id TEXT PRIMARY KEY, user_id TEXT, fingerprint TEXT, created REAL,
              category TEXT, usage TEXT);
            CREATE INDEX IF NOT EXISTS classifications_user ON clothing_classifications(user_id, created);
            """)
            db.execute("""INSERT INTO generation_archive
              (id,user_id,session,status,created,elapsed,person,garment,result,product_url,error,usage,feedback,garments)
              SELECT j.id,COALESCE(s.participant,s.id),j.session,j.status,j.created,j.elapsed,
                     j.person,j.garment,j.result,j.product_url,j.error,j.usage,j.feedback,j.garments
              FROM jobs j JOIN sessions s ON s.id=j.session
              WHERE NOT EXISTS (SELECT 1 FROM generation_archive a WHERE a.id=j.id)""")
            db.execute("UPDATE generation_archive SET status='error', error=? WHERE status IN ('queued','running')",
                       ("Сервер был перезапущен. Примерка не повторялась автоматически; предыдущий запрос мог быть оплачен.",))
            db.execute("UPDATE jobs SET status='error', error=? WHERE status IN ('queued','running')",
                       ("Сервер был перезапущен. Примерка не повторялась автоматически; предыдущий запрос мог быть оплачен.",))
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    @contextmanager
    def db(self):
        with connect(self.path, self.database_url) as db:
            yield db

    def session(self, token):
        sid = hashlib.sha256(token.encode()).hexdigest() if token else ""
        with self.db() as db:
            if sid and db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                return sid, None
            token = secrets.token_urlsafe(32)
            sid = hashlib.sha256(token.encode()).hexdigest()
            db.execute("INSERT INTO sessions (id,created) VALUES (?,?)", (sid, time.time()))
        return sid, token

    def participant(self, db, sid):
        row = db.execute("SELECT participant FROM sessions WHERE id=?", (sid,)).fetchone()
        return row[0] if row else None

    def used(self, db, sid):
        participant = self.participant(db, sid)
        if participant:
            return db.execute("SELECT count(*) FROM attempts a JOIN sessions s ON a.session=s.id WHERE s.participant=?", (participant,)).fetchone()[0]
        return db.execute("SELECT count(*) FROM attempts WHERE session=?", (sid,)).fetchone()[0]

    @property
    def invites_required(self):
        return bool(self.invite_hashes or self.admin_hash)

    def invited(self, db, participant):
        return participant in self.invite_hashes or invitations.active(db, participant)

    def unlock(self, sid, code):
        if not self.invites_required:
            raise Problem(400, "Приглашение здесь не требуется.")
        if not isinstance(code, str) or not 16 <= len(code.strip()) <= 128:
            raise Problem(403, "Не удалось найти приглашение. Проверьте код.")
        hashed = hashlib.sha256(code.strip().encode()).hexdigest()
        with self.lock, self.db() as db:
            if not self.invited(db, hashed):
                raise Problem(403, "Приглашение недействительно, отозвано или срок ссылки истёк.")
            current = self.participant(db, sid)
            if current and current != hashed:
                raise Problem(409, "В этом браузере уже используется другое приглашение.")
            db.execute("UPDATE sessions SET participant=? WHERE id=?", (hashed, sid))
        return self.config(sid)

    def config(self, sid):
        with self.db() as db:
            used = self.used(db, sid)
            total = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
            invited = self.invited(db, self.participant(db, sid))
        return {"generation_enabled": bool(self.provider),
                "classification_enabled": callable(getattr(self.provider, 'classify', None)),
                "max_garments": MAX_GARMENTS,
                "remaining": max(0, min(self.session_limit-used, self.total_limit-total)),
                "invite_required": self.invites_required, "invited": invited,
                "retention_hours": 24}

    def create(self, sid, body):
        if self.invites_required:
            with self.db() as db:
                if not self.invited(db, self.participant(db, sid)):
                    raise Problem(403, "Введите код приглашения, чтобы начать примерку.")
        rid = body.get("request_id")
        if not isinstance(rid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", rid):
            raise Problem(400, "Обновите страницу и попробуйте снова.")
        # Normalize before hashing to make idempotency independent of image metadata.
        with self.lock:
            person = decode_image(body.get('person'))
            items = body.get('garments')
            if items is None:
                items = [{'image': body.get('garment'), 'category': 'Вещь'}]
            if not isinstance(items, list) or not 1 <= len(items) <= MAX_GARMENTS:
                raise Problem(400, 'Добавьте от 1 до 6 вещей для образа.')
            packed = []
            images = []
            for item in items:
                if not isinstance(item, dict) or item.get('category', 'Вещь') not in CATEGORIES:
                    raise Problem(400, 'Выберите тип вещи из списка.')
                image = decode_image(item.get('image'))
                images.append(image)
                packed.append({'image': base64.b64encode(image).decode(), 'category': item.get('category', 'Вещь')})
            garment = images[0]
            garments = json.dumps(packed, ensure_ascii=False, separators=(',', ':'))
        link = shop_url(body.get("product_url", ""))
        fingerprint = hashlib.sha256(person + b''.join(images) + link.encode() +
                                     (json.dumps([p['category'] for p in packed]).encode() if 'garments' in body else b'')).hexdigest()
        with self.lock, self.db() as db:
            if self.invites_required and not self.invited(db, self.participant(db, sid)):
                raise Problem(403, "Приглашение больше не действует.")
            previous = db.execute("SELECT * FROM jobs WHERE session=? AND request_id=?", (sid, rid)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise Problem(409, "Этот запрос уже отправлен с другими фотографиями.")
                return self.public(previous)
            if not self.provider:
                raise Problem(503, "Генерация пока выключена. Загрузка и обрезка работают; платный запрос не отправлен.")
            if db.execute("SELECT 1 FROM jobs WHERE session=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise Problem(409, "Одна примерка уже создаётся. Дождитесь результата.")
            total = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
            used = self.used(db, sid)
            participant = self.participant(db, sid)
            if used >= self.session_limit or total >= self.total_limit:
                raise Problem(429, "Лимит тестовых примерок исчерпан. Сообщите владельцу приложения.")
            jid, now = secrets.token_hex(16), time.time()
            db.execute("INSERT INTO jobs (id,session,request_id,fingerprint,status,created,person,garment,product_url,garments) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (jid, sid, rid, fingerprint, "queued", now, person, garment, link, garments))
            user_id = self.participant(db, sid) or sid
            db.execute("""INSERT INTO generation_archive
              (id,user_id,session,status,created,person,garment,product_url,model,prompt,garments)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (jid, user_id, sid, 'queued', now, person, garment, link,
               getattr(self.provider, 'model', None), getattr(self.provider, 'prompt', None), garments))
            db.execute("INSERT INTO attempts VALUES (?,?)", (sid, now))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return self.public(row)

    def public(self, row):
        result = {key: row[key] for key in ("id", "request_id", "status", "created", "elapsed", "product_url", "error", "feedback")}
        result['garments'] = [{'category': item['category'], 'index': i}
                              for i, item in enumerate(self.items(row))]
        return result

    def items(self, row):
        return json.loads(row['garments']) if row['garments'] else [
            {'image': base64.b64encode(row['garment']).decode(), 'category': 'Вещь'}]

    def classify(self, sid, body):
        classifier = getattr(self.provider, 'classify', None)
        if not callable(classifier):
            raise Problem(503, 'Выберите тип вещи вручную.')
        with self.lock, self.db() as db:
            uid = self.participant(db, sid) or sid
            if self.invites_required and not self.invited(db, uid):
                raise Problem(403, 'Сначала введите приглашение.')
            image = decode_image(body.get('image'))
            fingerprint = hashlib.sha256(image).hexdigest()
            cutoff = time.time() - TTL
            cached = db.execute('SELECT category FROM clothing_classifications WHERE user_id=? AND fingerprint=? AND created>? ORDER BY created DESC LIMIT 1', (uid, fingerprint, cutoff)).fetchone()
            if cached:
                return {'category': cached[0] or 'Вещь'}
            used = db.execute('SELECT count(*) FROM clothing_classifications WHERE user_id=? AND created>?', (uid, cutoff)).fetchone()[0]
            total = db.execute('SELECT count(*) FROM clothing_classifications WHERE created>?', (cutoff,)).fetchone()[0]
            if used >= 30 or total >= 150:
                raise Problem(429, 'Распознавание пока недоступно. Выберите тип вручную.')
            cid = secrets.token_hex(16)
            db.execute('INSERT INTO clothing_classifications (id,user_id,fingerprint,created) VALUES (?,?,?,?)', (cid, uid, fingerprint, time.time()))
        # Reserve before calling the provider; uncertain failures are never retried.
        try:
            category, usage = classifier(image, CATEGORIES)
        except GenerationError as error:
            category, usage = 'Вещь', error.usage
        except Exception:
            category, usage = 'Вещь', None
        if category not in CATEGORIES:
            category = 'Вещь'
        with self.db() as db:
            db.execute('UPDATE clothing_classifications SET category=?,usage=? WHERE id=?', (category, json.dumps(usage), cid))
        return {'category': category}

    def get(self, sid, jid):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE session=? AND id=?", (sid, jid)).fetchone()
        if not row:
            raise Problem(404, "Эта примерка недоступна или уже удалена.")
        return row

    def activity(self, sid, body):
        visit = body.get('visit_id')
        sequence, seconds = body.get('sequence'), body.get('seconds')
        if not isinstance(visit, str) or not re.fullmatch(r'[a-f0-9]{32}', visit):
            raise Problem(400, 'Неверный визит.')
        if type(sequence) is not int or not 1 <= sequence <= 1_000_000:
            raise Problem(400, 'Неверный номер события.')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 45:
            raise Problem(400, 'Неверное время активности.')
        with self.lock, self.db() as db:
            uid = self.participant(db, sid)
            if not uid or uid == self.admin_hash or not self.invited(db, uid):
                return {'tracked': False}
            now = time.time()
            previous = db.execute('SELECT * FROM engagement_visits WHERE id=?', (visit,)).fetchone()
            if previous and (previous['user_id'] != uid or previous['session'] != sid):
                raise Problem(409, 'Этот визит недоступен.')
            if previous and sequence <= previous['sequence']:
                return {'tracked': True}
            # Cap claimed time by server time, and merge overlapping tabs per invite.
            seconds = min(seconds, max(0, now-previous['last_seen']+1)) if previous else min(seconds, 30)
            activity = db.execute('SELECT * FROM invited_activity WHERE user_id=?', (uid,)).fetchone()
            credit = min(seconds, max(0, now-activity['last_seen'])) if activity else seconds
            if previous:
                db.execute('UPDATE engagement_visits SET last_seen=?,sequence=? WHERE id=?', (now, sequence, visit))
            else:
                db.execute('INSERT INTO engagement_visits VALUES (?,?,?,?,?,?)', (visit, uid, sid, now, now, sequence))
            if activity:
                db.execute('UPDATE invited_activity SET active_seconds=active_seconds+?,last_seen=? WHERE user_id=?', (credit, now, uid))
            else:
                db.execute('INSERT INTO invited_activity VALUES (?,?,?)', (uid, credit, now))
        return {'tracked': True}

    def dashboard(self):
        with self.db() as db:
            rows = db.execute("""WITH users AS (
              SELECT DISTINCT COALESCE(s.participant,s.id) AS user_id FROM sessions s
              WHERE s.participant IS NOT NULL OR EXISTS (SELECT 1 FROM attempts a WHERE a.session=s.id)
              UNION SELECT user_id FROM generation_archive
            ), counts AS (
              SELECT COALESCE(s.participant,s.id) AS user_id,COUNT(*) AS generations,MAX(a.created) AS last_generation
              FROM attempts a JOIN sessions s ON s.id=a.session GROUP BY COALESCE(s.participant,s.id)
            ), metrics AS (
              SELECT user_id,
              SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successful,
              SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
              SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END) AS pending,
              AVG(CASE WHEN status='success' THEN wait_elapsed END) AS average_seconds
              FROM generation_archive GROUP BY user_id
            ), visits AS (
              SELECT user_id,COUNT(*) AS visits FROM engagement_visits GROUP BY user_id
            ) SELECT u.user_id,i.label,COALESCE(c.generations,0) AS generations,
              c.last_generation,COALESCE(m.successful,0) AS successful,
              COALESCE(m.errors,0) AS errors,COALESCE(m.pending,0) AS pending,m.average_seconds,
              t.active_seconds,t.last_seen,COALESCE(v.visits,0) AS visits,
              CASE WHEN i.id IS NOT NULL THEN 1 ELSE 0 END AS invitation_known
              FROM users u LEFT JOIN counts c ON c.user_id=u.user_id
              LEFT JOIN metrics m ON m.user_id=u.user_id
              LEFT JOIN invitations i ON i.token_hash=u.user_id
              LEFT JOIN invited_activity t ON t.user_id=u.user_id
              LEFT JOIN visits v ON v.user_id=u.user_id
              ORDER BY CASE WHEN c.last_generation IS NULL THEN 1 ELSE 0 END,c.last_generation DESC""").fetchall()
            quality = db.execute("""SELECT
              SUM(CASE WHEN feedback IS NOT NULL THEN 1 ELSE 0 END) AS rated,
              SUM(CASE WHEN feedback='helpful' THEN 1 ELSE 0 END) AS helpful
              FROM generation_archive WHERE status='success'""").fetchone()
        users = [dict(r) for r in rows]
        for user in users:
            user['is_invited'] = user['user_id'] != self.admin_hash and bool(user.pop('invitation_known') or user['user_id'] in self.invite_hashes)

            user['label'] = user['label'] or 'Участник ' + user['user_id'][:8]
        totals = {'users': len(users), **{key: sum(u[key] for u in users) for key in ('generations','successful','errors','pending')}}
        invited = [u for u in users if u['is_invited']]
        measured = [u for u in invited if u['visits'] > 0]
        completed = totals['successful']+totals['errors']
        return {'users': users, 'totals': totals, 'engagement': {
            'invited_users': len(invited), 'measured_users': len(measured),
            'average_active_seconds': sum(u['active_seconds'] or 0 for u in measured)/len(measured) if measured else None,
            'visits': sum(u['visits'] for u in measured),
            'activated_users': sum(u['generations'] > 0 for u in invited),
            'repeat_users': sum(u['generations'] > 1 for u in invited),
            'success_rate': totals['successful']/completed if completed else None,
            'rated': quality['rated'] or 0, 'helpful': quality['helpful'] or 0
        }}

    def archive(self, user_id=None, offset=0, status=None):
        clauses, params = [], []
        if user_id:
            clauses.append('a.user_id=?'); params.append(user_id)
        if status:
            clauses.append('a.status=?'); params.append(status)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.db() as db:
            total = db.execute('SELECT COUNT(*) FROM generation_archive a'+where, params).fetchone()[0]
            rows = db.execute("""SELECT a.id,a.user_id,i.label,a.status,a.created,a.elapsed,
              a.wait_elapsed,a.product_url,a.error,a.feedback,a.model,a.prompt,a.garments,
              CASE WHEN a.person IS NOT NULL THEN 1 ELSE 0 END AS has_person,
              CASE WHEN a.garment IS NOT NULL THEN 1 ELSE 0 END AS has_garment,
              CASE WHEN a.result IS NOT NULL THEN 1 ELSE 0 END AS has_result
              FROM generation_archive a LEFT JOIN invitations i ON i.token_hash=a.user_id"""
              + where + ' ORDER BY a.created DESC,a.id DESC LIMIT 20 OFFSET ?', (*params, offset)).fetchall()
        jobs = [dict(r) for r in rows]
        for job in jobs:
            job['label'] = job['label'] or 'Участник ' + job['user_id'][:8]
            packed = json.loads(job.pop('garments') or '[]')
            job['garments'] = [{'category': item['category'], 'index': i} for i, item in enumerate(packed)]
        return {'jobs': jobs, 'total': total, 'offset': offset, 'limit': 20}

    def cleanup(self):
        with self.lock, self.db() as db:
            cutoff = time.time()-TTL
            db.execute("""UPDATE generation_archive SET status='error',error=?
              WHERE status='queued' AND id IN (SELECT id FROM jobs WHERE status='queued' AND created<?)""",
              ('Примерка не запущена: срок ожидания истёк.', cutoff))
            db.execute("DELETE FROM jobs WHERE created < ?", (cutoff,))

    def work(self):
        while not self.stop.wait(2 if self.database_url else .2):
            try:
                self.cleanup()
            except Exception:
                # An unavailable remote database must not kill the queue worker.
                continue
            if not self.provider:
                continue
            try:
                with self.lock, self.db() as db:
                    row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                    if row:
                        db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
                        db.execute("UPDATE generation_archive SET status='running' WHERE id=?", (row["id"],))
            except Exception:
                continue
            if not row:
                continue
            start = time.monotonic()
            try:
                images = [base64.b64decode(item['image']) for item in self.items(row)]
                result, usage = self.provider(row["person"], images if len(images) > 1 else images[0])
                result = normalize(result)
                values = ("success", result, None, json.dumps(usage), time.monotonic()-start, row["id"])
            except GenerationError as error:
                values = ("error", None, str(error), json.dumps(error.usage), time.monotonic()-start, row["id"])
            except Exception:
                values = ("error", None, "Не удалось обработать результат. Попробуйте другое фото.", None, time.monotonic()-start, row["id"])
            wait_elapsed = max(0, time.time()-row['created'])
            # Retry only storing the result, never the paid generation itself.
            while not self.stop.is_set():
                try:
                    with self.lock, self.db() as db:
                        db.execute("UPDATE jobs SET status=?,result=?,error=?,usage=?,elapsed=? WHERE id=?", values)
                        db.execute("UPDATE generation_archive SET status=?,result=?,error=?,usage=?,elapsed=?,wait_elapsed=? WHERE id=?",
                                   (*values[:5], wait_elapsed, row['id']))
                    break
                except Exception:
                    self.stop.wait(5)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, origin, frontend_origin=None):
        self.store, self.origin = store, origin.rstrip("/")
        self._frontend_origin = frontend_origin.rstrip("/") if frontend_origin else None
        self.slots = threading.BoundedSemaphore(8)
        self.rate_lock = threading.Lock()
        self.rate_windows = {}
        self.trust_proxy = False
        super().__init__(address, Handler)

    @property
    def frontend_origin(self):
        return self._frontend_origin or self.origin

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def rate_limit(self, peer, mutation):
        now = time.monotonic()
        with self.rate_lock:
            key = (peer, mutation)
            # Bound memory even when many addresses visit the app.
            if len(self.rate_windows) > 4096:
                self.rate_windows = {k:v for k,v in self.rate_windows.items() if v and v[-1] > now-60}
                if len(self.rate_windows) > 4096:
                    raise Problem(429, "Слишком много запросов. Попробуйте через минуту.")
            window = self.rate_windows.setdefault(key, deque())
            while window and window[0] < now-60:
                window.popleft()
            if len(window) >= (12 if mutation else 180):
                raise Problem(429, "Слишком много запросов. Попробуйте через минуту.")
            window.append(now)


class Handler(BaseHTTPRequestHandler):
    server_version = "Fitme"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass  # Never log image payloads, cookies, keys or shop URLs.

    def respond(self, status, data, mime="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.headers.get("Origin") == self.server.frontend_origin:
            self.send_header("Access-Control-Allow-Origin", self.server.frontend_origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Fitme-Request")
        if mime == 'image/jpeg' and urlsplit(self.path).query == 'download=1':
            self.send_header('Content-Disposition', 'attachment; filename="fitme-try-on.jpg"')
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if getattr(self, "new_token", None):
            secure = "; Secure" if self.server.origin.startswith("https:") else ""
            self.send_header("Set-Cookie", f"fitme_session={self.new_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800{secure}")
        if getattr(self, "admin_cookie", None) is not None:
            secure = "; Secure" if self.server.origin.startswith("https:") else ""
            age = 28800 if self.admin_cookie else 0
            self.send_header("Set-Cookie", f"fitme_admin={self.admin_cookie}; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age={age}{secure}")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self, limit):
        if self.headers.get("Content-Type") != "application/json":
            raise Problem(415, "Неверный формат запроса.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Problem(400, "Неверный размер запроса.")
        if not 0 < length <= limit:
            raise Problem(413, "Запрос слишком большой.")
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
            return body
        except (ValueError, OSError):
            raise Problem(400, "Не удалось прочитать запрос.")

    def admin_dispatch(self, path, cookie):
        store = self.server.store
        # Owner tools are same-origin only, including when a separate frontend is configured.
        if self.command != 'GET' and self.headers.get('Origin') != self.server.origin:
            raise Problem(403, 'Недопустимый адрес панели.')
        token = cookie.get('fitme_admin')
        token = token.value if token else ''
        if path == '/api/admin/login' and self.command == 'POST':
            self.admin_cookie = invitations.login(store, self.read_json(1024).get('key'))
            return self.respond(200, {'ok': True})
        invitations.authorize(store, token)
        if path == '/api/admin/logout' and self.command == 'POST':
            with store.db() as db:
                db.execute('DELETE FROM admin_sessions WHERE token_hash=?', (invitations.digest(token),))
            self.admin_cookie = ''
            return self.respond(200, {'ok': True})
        if self.command == 'GET' and path == '/api/admin/dashboard':
            return self.respond(200, store.dashboard())
        if self.command == 'GET' and path == '/api/admin/generations':
            query = parse_qs(urlsplit(self.path).query)
            user_id = query.get('user', [None])[0]
            status = query.get('status', [None])[0]
            try:
                offset = int(query.get('offset', ['0'])[0])
                if offset < 0 or offset > 1_000_000:
                    raise ValueError()
            except ValueError:
                raise Problem(400, 'Неверная страница архива.') from None
            if user_id and not re.fullmatch(r'[a-f0-9]{64}', user_id):
                raise Problem(400, 'Неверный пользователь.')
            if status and status not in ('success', 'error', 'queued', 'running'):
                raise Problem(400, 'Неверный статус.')
            return self.respond(200, store.archive(user_id, offset, status))
        item_match = re.fullmatch(r'/api/admin/generations/([a-f0-9]{32})/garments/([0-5])', path)
        if self.command == 'GET' and item_match:
            jid, index = item_match.groups()
            with store.db() as db:
                row = db.execute('SELECT garment,garments FROM generation_archive WHERE id=?', (jid,)).fetchone()
            if row:
                items = store.items(row)
                if int(index) < len(items):
                    return self.respond(200, base64.b64decode(items[int(index)]['image']), 'image/jpeg')
            raise Problem(404, 'Вещь недоступна.')
        photo = re.fullmatch(r'/api/admin/generations/([a-f0-9]{32})/(person|garment|result)', path)
        if self.command == 'GET' and photo:
            jid, part = photo.groups()
            with store.db() as db:
                row = db.execute(f'SELECT {part} FROM generation_archive WHERE id=?', (jid,)).fetchone()
            if not row or not row[part]:
                raise Problem(404, 'Фото недоступно.')
            return self.respond(200, bytes(row[part]), 'image/jpeg')
        if path == '/api/admin/invitations':
            if self.command == 'GET':
                return self.respond(200, invitations.listing(store))
            if self.command == 'POST':
                return self.respond(201, invitations.create(store, self.read_json(1024).get('label')))
        match = re.fullmatch(r'/api/admin/invitations/([a-f0-9]{32})/revoke', path)
        if match and self.command == 'POST':
            return self.respond(200, invitations.revoke(store, match[1]))
        raise Problem(404, 'Страница не найдена.')

    def dispatch(self):
        self.new_token = None
        self.admin_cookie = None
        try:
            path = urlsplit(self.path).path
            if self.command == "GET" and path == "/healthz":
                return self.respond(200, {"ok": True})
            expected_host = urlsplit(self.server.origin).netloc
            if self.headers.get("Host") != expected_host:
                raise Problem(403, "Недопустимый адрес приложения.")
            if self.command == 'OPTIONS':
                if self.headers.get('Origin') != self.server.frontend_origin:
                    raise Problem(403, 'Недопустимый адрес приложения.')
                return self.respond(204, b'')
            peer = self.headers.get("X-Real-IP", self.client_address[0]) if self.server.trust_proxy else self.client_address[0]
            self.server.rate_limit(peer, self.command != "GET")
            if self.command != "GET":
                if self.headers.get("Origin") != (self.server.origin if path.startswith("/api/admin/") else self.server.frontend_origin) or self.headers.get("X-Fitme-Request") != "1":
                    raise Problem(403, "Запрос отклонён. Откройте приложение заново.")
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                pass
            if self.command == "GET" and path in ("/", "/app.js", "/style.css", "/favicon.svg", "/admin", "/admin.js", "/admin.css"):
                filename = {"/": "index.html", "/admin": "admin.html"}.get(path, path[1:])
                mime = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}[filename.split(".")[-1]]
                return self.respond(200, (STATIC/filename).read_bytes(), mime)
            if path.startswith('/api/admin/'):
                return self.admin_dispatch(path, cookie)
            token = cookie.get("fitme_session")
            sid, self.new_token = self.server.store.session(token.value if token else "")
            if self.command == 'POST' and path == '/api/activity':
                return self.respond(200, self.server.store.activity(sid, self.read_json(1024)))
            self.server.store.cleanup()
            if self.command == "GET" and path == "/api/session":
                with self.server.store.db() as db:
                    rows = db.execute("SELECT * FROM jobs WHERE session=? ORDER BY created DESC", (sid,)).fetchall()
                return self.respond(200, {**self.server.store.config(sid), "jobs": [self.server.store.public(r) for r in rows]})
            if self.command == "DELETE" and path == "/api/photos":
                with self.server.store.lock, self.server.store.db() as db:
                    db.execute("""UPDATE generation_archive SET status='error',error=?
                      WHERE status='queued' AND id IN (SELECT id FROM jobs WHERE session=? AND status='queued')""",
                      ('Примерка отменена до запуска пользователем.', sid))
                    db.execute("DELETE FROM jobs WHERE session=?", (sid,))
                return self.respond(200, {"deleted": True})
            if self.command == "POST":
                body = self.read_json(MAX_BODY)
                if path == "/api/invite":
                    return self.respond(200, self.server.store.unlock(sid, body.get("code")))
                if path == "/api/jobs":
                    return self.respond(202, self.server.store.create(sid, body))
                if path == '/api/classify':
                    return self.respond(200, self.server.store.classify(sid, body))
            item_match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/garments/([0-5])', path)
            if self.command == 'GET' and item_match:
                jid, index = item_match.groups()
                row = self.server.store.get(sid, jid)
                items = self.server.store.items(row)
                if int(index) < len(items):
                    return self.respond(200, base64.b64decode(items[int(index)]['image']), 'image/jpeg')
                raise Problem(404, 'Вещь недоступна.')
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/(person|garment|result|feedback))?", path)
            if match:
                jid, part = match.groups()
                row = self.server.store.get(sid, jid)
                if self.command == "GET":
                    if not part:
                        return self.respond(200, self.server.store.public(row))
                    if part in ("person", "garment", "result") and row[part]:
                        return self.respond(200, row[part], "image/jpeg")
                if self.command == "POST" and part == "feedback" and row["status"] == "success":
                    value = body.get("rating")
                    if value not in ("helpful", "person", "garment", "unhelpful"):
                        raise Problem(400, "Выберите оценку.")
                    with self.server.store.db() as db:
                        db.execute("UPDATE jobs SET feedback=? WHERE id=? AND session=?", (value, jid, sid))
                        db.execute("UPDATE generation_archive SET feedback=? WHERE id=? AND session=?", (value, jid, sid))
                    return self.respond(200, {"saved": True})
            raise Problem(404, "Страница не найдена.")
        except Problem as error:
            self.respond(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(500, {"error": "Не удалось выполнить запрос. Попробуйте позже."})

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch
    do_OPTIONS = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--origin", help="Exact browser origin. Required for a LAN host or TLS proxy.")
    parser.add_argument("--enable-generation", action="store_true")
    parser.add_argument("--require-invites", action="store_true")
    parser.add_argument("--trust-proxy", action="store_true", help="Use only on a private network behind a proxy that overwrites X-Real-IP")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    args = parser.parse_args()
    config = dict(os.environ)
    for env_path in args.env_file:
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                config.setdefault(k.strip(), v.strip().strip("\"'"))
    provider = None
    if args.enable_generation or config.get("FITME_ENABLE_GENERATION") == "1":
        key = config.get("GEMINI_API_KEY")
        if not key:
            parser.error("GEMINI_API_KEY is required to enable generation")
        provider = GeminiProvider(key, config.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"))
    if args.host not in ("localhost", "127.0.0.1") and not args.origin:
        parser.error("--origin is required for LAN access")
    invite_hashes = [value.strip() for value in config.get("FITME_INVITE_HASHES", "").split(",") if value.strip()]
    if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in invite_hashes):
        parser.error("FITME_INVITE_HASHES must contain comma-separated SHA256 hashes")
    admin_hash = config.get('FITME_ADMIN_HASH')
    if admin_hash and not re.fullmatch(r'[a-f0-9]{64}', admin_hash):
        parser.error('FITME_ADMIN_HASH must be a SHA256 hash')
    if args.require_invites and not (invite_hashes or admin_hash):
        parser.error("At least one invitation hash is required for the hosted beta")
    data = args.data_dir
    data.mkdir(exist_ok=True, mode=0o700)
    os.chmod(data, 0o700)
    db_path = data/"fitme.sqlite3"
    database_url = config.get('DATABASE_URL')
    if config.get('RENDER') and not database_url:
        parser.error('DATABASE_URL is required on Render: ephemeral storage cannot safely preserve paid quotas')
    store = Store(db_path, provider, invite_hashes=invite_hashes, database_url=database_url, admin_hash=admin_hash)
    if not database_url:
        os.chmod(db_path, 0o600)
    origin = args.origin or f"http://{args.host}:{args.port}"
    server = AppServer((args.host, args.port), store, origin, config.get('FITME_FRONTEND_ORIGIN'))
    server.trust_proxy = args.trust_proxy
    print(f"Fitme: {origin} | generation {'enabled' if provider else 'disabled'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
