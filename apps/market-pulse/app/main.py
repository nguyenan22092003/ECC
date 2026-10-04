"""Single-user local dashboard and authenticated API."""
from contextlib import asynccontextmanager
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import secrets as random_secrets
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.db import Store
from app.engine import Engine

BASE = Path(__file__).resolve().parents[1]
WEB = BASE / 'web'
MAX_BODY = 32768
SESSION_SECONDS = 60 * 60 * 12
MUTATING = {'POST', 'PUT', 'PATCH', 'DELETE'}


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Credentials(Payload):
    password: str = Field(min_length=1, max_length=256)


class WatchBody(Payload):
    asset_id: str = Field(min_length=3, max_length=80)


class ScanBody(Payload):
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    hours: int = Field(default=24, ge=1, le=168)
    force: bool = False
    notify: bool = False


class SourceBody(Payload):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    market: str = 'ALL'
    enabled: bool = True


class SourcePatch(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    market: str | None = None
    enabled: bool | None = None


class ScheduleBody(Payload):
    name: str = Field(min_length=1, max_length=100)
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    hours: int = Field(default=24, ge=1, le=168)
    force: bool = False
    notify: bool = False
    cron: str = Field(default='*/30 * * * *', min_length=9, max_length=100)
    timezone: str = Field(default='Asia/Ho_Chi_Minh', max_length=80)
    kind: str = 'scan'
    enabled: bool = True


class SettingsBody(Payload):
    ai_enabled: bool | None = None
    ai_provider: str | None = None
    ai_model: str | None = Field(default=None, max_length=200)
    daily_budget_usd: float | None = Field(default=None, ge=0, le=10000)
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=10000)
    input_price_per_million: float | None = Field(default=None, ge=0, le=10000)
    output_price_per_million: float | None = Field(default=None, ge=0, le=10000)
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=200)
    notify_high_only: bool | None = None
    max_notifications_per_day: int | None = Field(default=None, ge=1, le=1000)
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str | None = Field(default=None, max_length=80)
    ai_api_key: str | None = Field(default=None, max_length=500)
    telegram_bot_token: str | None = Field(default=None, max_length=500)
    clear_ai_key: bool = False
    clear_telegram_token: bool = False


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or random_secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310_000)
    return base64.urlsafe_b64encode(salt + digest).decode()


def _check_password(password: str, encoded: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode())
        return hmac.compare_digest(raw[16:], hashlib.pbkdf2_hmac('sha256', password.encode(), raw[:16], 310_000))
    except (ValueError, TypeError):
        return False


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def _safe_settings(settings: dict, secrets_state: dict) -> dict:
    return {**settings, 'ai_key_configured': bool(secrets_state.get('ai_api_key')),
            'telegram_token_configured': bool(secrets_state.get('telegram_bot_token'))}


def create_app(data_dir: str | Path | None = None, password: str | None = None,
               worker: bool = True) -> FastAPI:
    directory = Path(data_dir or os.getenv('MARKET_PULSE_DATA_DIR') or BASE / 'data').expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    auth_file, secrets_file, key_file = directory / 'auth.json', directory / 'secrets.bin', directory / 'secrets.key'
    bootstrap_message = None
    env_password = password or os.getenv('MARKET_PULSE_PASSWORD')
    if auth_file.exists():
        auth = json.loads(auth_file.read_text(encoding='utf-8'))
    else:
        bootstrap = env_password or random_secrets.token_urlsafe(20)
        auth = {'password_hash': _password_hash(bootstrap), 'signing_key': random_secrets.token_urlsafe(32)}
        _write_json(auth_file, auth)
        if not env_password:
            bootstrap_message = f'Market Pulse first-run password: {bootstrap}'
    if key_file.exists():
        fernet = Fernet(key_file.read_bytes())
    else:
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
        fernet = Fernet(key_file.read_bytes())
    secret_values = {}
    if secrets_file.exists():
        try:
            secret_values = json.loads(fernet.decrypt(secrets_file.read_bytes()))
        except (InvalidToken, ValueError, json.JSONDecodeError):
            raise RuntimeError('Encrypted local credentials cannot be read; preserve data and restore the matching key file.')
    store = Store(directory / 'market-pulse.sqlite3')
    engine = Engine(store, secret_values)

    @asynccontextmanager
    async def lifespan(app):
        if worker:
            await engine.start()
        try:
            yield
        finally:
            if worker:
                await engine.stop()
            store.close()

    app = FastAPI(title='Market Pulse', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.state.auth, app.state.fernet, app.state.secret_file = auth, fernet, secrets_file
    app.state.bootstrap_message = bootstrap_message
    app.state.login_attempts = {}
    allowed = {'127.0.0.1', 'localhost', 'testserver'}
    allowed.update(x.strip().lower() for x in os.getenv('MARKET_PULSE_ALLOWED_HOSTS', '').split(',') if x.strip())

    @app.middleware('http')
    async def secure_request(request: Request, call_next):
        content_length = request.headers.get('content-length', '0') or '0'
        if not content_length.isdecimal() or len(content_length) > 10 or int(content_length) > MAX_BODY:
            return JSONResponse({'success': False, 'error': 'Yêu cầu quá lớn.'}, status_code=413)
        host = request.headers.get('host', '').lower()
        host_name = (urlsplit('//' + host).hostname or '').lower()
        if host_name not in allowed:
            return JSONResponse({'success': False, 'error': 'Host không được phép.'}, status_code=400)
        if request.method in MUTATING and request.url.path.startswith('/api/'):
            origin = request.headers.get('origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc.lower() != host or parsed.scheme not in {'http', 'https'} or parsed.path not in {'', '/'}:
                    return JSONResponse({'success': False, 'error': 'Nguồn yêu cầu không hợp lệ.'}, status_code=403)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        return response

    def csrf_for(session: str) -> str:
        return hmac.new(auth['signing_key'].encode(), ('csrf:' + session).encode(), hashlib.sha256).hexdigest()

    def authenticated(request: Request) -> bool:
        token = request.cookies.get('market_pulse_session', '')
        try:
            session, timestamp, signature = token.split('.')
            expiry = int(timestamp)
            if expiry < int(time.time()) or expiry > int(time.time()) + SESSION_SECONDS + 60:
                return False
            expected = hmac.new(auth['signing_key'].encode(), f'{session}.{timestamp}'.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def session_id(request: Request) -> str:
        try:
            return request.cookies.get('market_pulse_session', '').split('.')[0]
        except (ValueError, IndexError):
            return ''

    async def require_login(request: Request, csrf=True):
        if not authenticated(request):
            raise HTTPException(401, 'Vui lòng đăng nhập.')
        if csrf and request.method in MUTATING:
            expected = csrf_for(session_id(request))
            provided = request.headers.get('x-csrf-token', '')
            if not hmac.compare_digest(expected, provided):
                raise HTTPException(403, 'Phiên bảo mật hết hạn; tải lại trang và thử lại.')

    from fastapi import HTTPException, Depends

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({'success': False, 'error': exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'success': False, 'error': 'Dữ liệu nhập chưa hợp lệ. Kiểm tra các trường và thử lại.'}, status_code=422)

    @app.get('/')
    async def home():
        if bootstrap_message:
            print(bootstrap_message, flush=True)
            app.state.bootstrap_message = None
        return FileResponse(WEB / 'index.html')

    app.mount('/static', StaticFiles(directory=WEB), name='static')

    @app.get('/api/session')
    async def get_session(request: Request):
        ok = authenticated(request)
        return {'success': True, 'data': {'authenticated': ok, 'csrf': csrf_for(session_id(request)) if ok else ''}}

    @app.post('/api/login')
    async def login(body: Credentials, request: Request, response: Response):
        client = request.client.host if request.client else 'unknown'
        attempts, start = app.state.login_attempts.get(client, (0, time.monotonic()))
        if time.monotonic() - start > 300:
            attempts, start = 0, time.monotonic()
        if attempts >= 10:
            raise HTTPException(429, 'Thử đăng nhập quá nhiều lần. Đợi 5 phút rồi thử lại.')
        if not _check_password(body.password, auth['password_hash']):
            app.state.login_attempts[client] = (attempts + 1, start)
            raise HTTPException(401, 'Mật khẩu chưa đúng.')
        app.state.login_attempts.pop(client, None)
        session, expiry = random_secrets.token_urlsafe(24), int(time.time()) + SESSION_SECONDS
        signature = hmac.new(auth['signing_key'].encode(), f'{session}.{expiry}'.encode(), hashlib.sha256).hexdigest()
        response.set_cookie('market_pulse_session', f'{session}.{expiry}.{signature}', max_age=SESSION_SECONDS,
            httponly=True, secure=request.url.scheme == 'https', samesite='strict', path='/')
        return {'success': True, 'data': {'csrf': csrf_for(session)}}

    @app.post('/api/logout', dependencies=[Depends(require_login)])
    async def logout(response: Response):
        response.delete_cookie('market_pulse_session', path='/', httponly=True, samesite='strict')
        return {'success': True, 'data': {'logged_out': True}}

    @app.get('/api/overview', dependencies=[Depends(require_login)])
    async def overview():
        return {'success': True, 'data': {**store.overview(), 'ai_configured': bool(engine.secrets.get('ai_api_key') and store.settings()['ai_model']),
            'telegram_configured': bool(engine.secrets.get('telegram_bot_token') and store.settings()['telegram_chat_id'])}}

    @app.get('/api/assets', dependencies=[Depends(require_login)])
    async def assets():
        return {'success': True, 'data': store.assets()}

    @app.get('/api/watchlist', dependencies=[Depends(require_login)])
    async def watchlist():
        return {'success': True, 'data': store.watchlist()}

    @app.post('/api/watchlist', dependencies=[Depends(require_login)])
    async def add_watch(body: WatchBody):
        try:
            return {'success': True, 'data': store.add_watch(body.asset_id)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete('/api/watchlist/{asset_id}', dependencies=[Depends(require_login)])
    async def remove_watch(asset_id: str):
        if not store.get('assets', asset_id):
            raise HTTPException(404, 'Không tìm thấy mã này.')
        store.remove_watch(asset_id)
        return {'success': True, 'data': {'removed': True}}

    @app.get('/api/sources', dependencies=[Depends(require_login)])
    async def sources():
        return {'success': True, 'data': store.sources()}

    @app.post('/api/sources', dependencies=[Depends(require_login)])
    async def create_source(body: SourceBody):
        try:
            return {'success': True, 'data': store.add_source(body.model_dump())}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch('/api/sources/{source_id}', dependencies=[Depends(require_login)])
    async def patch_source(source_id: str, body: SourcePatch):
        try:
            return {'success': True, 'data': store.update_source(source_id, body.model_dump(exclude_none=True))}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete('/api/sources/{source_id}', dependencies=[Depends(require_login)])
    async def delete_source(source_id: str):
        if not store.get('sources', source_id):
            raise HTTPException(404, 'Không tìm thấy nguồn tin.')
        store.delete_source(source_id)
        return {'success': True, 'data': {'removed': True}}

    @app.get('/api/articles', dependencies=[Depends(require_login)])
    async def articles(q: str = Query(default='', max_length=250), asset_id: str = Query(default='', max_length=80)):
        return {'success': True, 'data': store.articles(query=q, asset_id=asset_id)}

    @app.get('/api/jobs', dependencies=[Depends(require_login)])
    async def jobs():
        return {'success': True, 'data': store.jobs()}

    @app.post('/api/scans', status_code=202, dependencies=[Depends(require_login)])
    async def scan(body: ScanBody):
        try:
            return {'success': True, 'data': engine.enqueue_scan(body.model_dump())}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/schedules', dependencies=[Depends(require_login)])
    async def schedules():
        return {'success': True, 'data': store.schedules()}

    @app.post('/api/schedules', dependencies=[Depends(require_login)])
    async def create_schedule(body: ScheduleBody):
        try:
            return {'success': True, 'data': store.save_schedule(body.model_dump())}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put('/api/schedules/{schedule_id}', dependencies=[Depends(require_login)])
    async def update_schedule(schedule_id: str, body: ScheduleBody):
        try:
            return {'success': True, 'data': store.save_schedule(body.model_dump(), schedule_id)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete('/api/schedules/{schedule_id}', dependencies=[Depends(require_login)])
    async def delete_schedule(schedule_id: str):
        if not store.get('schedules', schedule_id):
            raise HTTPException(404, 'Không tìm thấy lịch.')
        store.delete_schedule(schedule_id)
        return {'success': True, 'data': {'removed': True}}

    @app.post('/api/schedules/{schedule_id}/run', status_code=202, dependencies=[Depends(require_login)])
    async def run_schedule(schedule_id: str):
        try:
            return {'success': True, 'data': await engine.run_schedule(schedule_id)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/settings', dependencies=[Depends(require_login)])
    async def get_settings():
        return {'success': True, 'data': _safe_settings(store.settings(), engine.secrets)}

    @app.put('/api/settings', dependencies=[Depends(require_login)])
    async def update_settings(body: SettingsBody):
        incoming = body.model_dump(exclude_unset=True)
        ai_key, bot_token = incoming.pop('ai_api_key', None), incoming.pop('telegram_bot_token', None)
        clear_ai, clear_telegram = incoming.pop('clear_ai_key', False), incoming.pop('clear_telegram_token', False)
        try:
            settings = dict(store.settings(), **incoming)
            candidate = dict(engine.secrets)
            if clear_ai:
                candidate.pop('ai_api_key', None)
            elif ai_key:
                candidate['ai_api_key'] = ai_key
            if clear_telegram:
                candidate.pop('telegram_bot_token', None)
            elif bot_token:
                if ':' not in bot_token or len(bot_token) > 200:
                    raise ValueError('Bot token không đúng định dạng.')
                candidate['telegram_bot_token'] = bot_token
            if settings['ai_enabled'] and (not settings['ai_model'] or not candidate.get('ai_api_key') or
                    not settings['input_price_per_million'] or not settings['output_price_per_million']):
                raise ValueError('Để bật AI, cần model, API key và giá token dương.')
            if settings['telegram_enabled'] and (not settings['telegram_chat_id'] or not candidate.get('telegram_bot_token')):
                raise ValueError('Để bật Telegram, cần bot token và Chat ID.')
            settings = store.save_settings(incoming)
            secrets_file.write_bytes(fernet.encrypt(json.dumps(candidate).encode()))
            secrets_file.chmod(0o600)
            engine.secrets = candidate
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'success': True, 'data': _safe_settings(settings, engine.secrets)}

    @app.get('/api/notifications', dependencies=[Depends(require_login)])
    async def notifications():
        return {'success': True, 'data': store.notifications()}

    @app.post('/api/notifications/test', status_code=202, dependencies=[Depends(require_login)])
    async def test_notification():
        try:
            return {'success': True, 'data': await engine.test_telegram()}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/notifications/digest', status_code=202, dependencies=[Depends(require_login)])
    async def digest():
        try:
            return {'success': True, 'data': await engine.notify_digest()}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app


def run() -> None:
    import uvicorn
    print('Market Pulse dashboard: http://127.0.0.1:8000', flush=True)
    uvicorn.run('app.main:app', host='127.0.0.1', port=int(os.getenv('PORT', '8000')), reload=False)


app = create_app()
