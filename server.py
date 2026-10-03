"""Local M1 prototype. Standard library HTTP + SQLite; deploy behind TLS for M2."""
import argparse
import base64
import hashlib
import io
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
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError
from provider import GeminiProvider, GenerationError
from database import connect
import invitations
from invitations import Problem

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static' if (ROOT / 'static').is_dir() else ROOT
MAX_BODY = 24 * 1024 * 1024
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
            invitations.initialize(db)
            if database_url:
                db.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS participant TEXT")
            elif "participant" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN participant TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS sessions_participant ON sessions(participant)")
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
                "remaining": max(0, min(self.session_limit-used, self.total_limit-total)),
                "invite_required": self.invites_required, "invited": invited,
                "retention_hours": 24}

    def create(self, sid, body):
        if self.invites_required:
            with self.db() as db:
                if not self.invited(db, self.participant(db, sid)):
                    raise Problem(403, "Введите код приглашения, чтобы начать примерку.")
        if body.get("consent") is not True:
            raise Problem(400, "Подтвердите отправку фотографий для примерки.")
        rid = body.get("request_id")
        if not isinstance(rid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", rid):
            raise Problem(400, "Обновите страницу и попробуйте снова.")
        # Normalize before hashing to make idempotency independent of image metadata.
        with self.lock:
            person, garment = decode_image(body.get("person")), decode_image(body.get("garment"))
        link = shop_url(body.get("product_url", ""))
        fingerprint = hashlib.sha256(person + garment + link.encode()).hexdigest()
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
            if used >= self.session_limit or total >= self.total_limit:
                raise Problem(429, "Лимит тестовых примерок исчерпан. Сообщите владельцу приложения.")
            jid, now = secrets.token_hex(16), time.time()
            db.execute("INSERT INTO jobs (id,session,request_id,fingerprint,status,created,person,garment,product_url) VALUES (?,?,?,?,?,?,?,?,?)",
                       (jid, sid, rid, fingerprint, "queued", now, person, garment, link))
            db.execute("INSERT INTO attempts VALUES (?,?)", (sid, now))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return self.public(row)

    def public(self, row):
        return {key: row[key] for key in ("id", "request_id", "status", "created", "elapsed", "product_url", "error", "feedback")}

    def get(self, sid, jid):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE session=? AND id=?", (sid, jid)).fetchone()
        if not row:
            raise Problem(404, "Эта примерка недоступна или уже удалена.")
        return row

    def cleanup(self):
        with self.lock, self.db() as db:
            db.execute("DELETE FROM jobs WHERE created < ?", (time.time()-TTL,))

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
            except Exception:
                continue
            if not row:
                continue
            start = time.monotonic()
            try:
                result, usage = self.provider(row["person"], row["garment"])
                result = normalize(result)
                values = ("success", result, None, json.dumps(usage), time.monotonic()-start, row["id"])
            except GenerationError as error:
                values = ("error", None, str(error), json.dumps(error.usage), time.monotonic()-start, row["id"])
            except Exception:
                values = ("error", None, "Не удалось обработать результат. Попробуйте другое фото.", None, time.monotonic()-start, row["id"])
            # Retry only storing the result, never the paid generation itself.
            while not self.stop.is_set():
                try:
                    with self.lock, self.db() as db:
                        db.execute("UPDATE jobs SET status=?,result=?,error=?,usage=?,elapsed=? WHERE id=?", values)
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
                if self.headers.get("Origin") != self.server.frontend_origin or self.headers.get("X-Fitme-Request") != "1":
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
            self.server.store.cleanup()
            if self.command == "GET" and path == "/api/session":
                with self.server.store.db() as db:
                    rows = db.execute("SELECT * FROM jobs WHERE session=? ORDER BY created DESC", (sid,)).fetchall()
                return self.respond(200, {**self.server.store.config(sid), "jobs": [self.server.store.public(r) for r in rows]})
            if self.command == "DELETE" and path == "/api/photos":
                with self.server.store.lock, self.server.store.db() as db:
                    db.execute("DELETE FROM jobs WHERE session=?", (sid,))
                return self.respond(200, {"deleted": True})
            if self.command == "POST":
                body = self.read_json(MAX_BODY)
                if path == "/api/invite":
                    return self.respond(200, self.server.store.unlock(sid, body.get("code")))
                if path == "/api/jobs":
                    return self.respond(202, self.server.store.create(sid, body))
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
