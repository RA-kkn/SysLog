import hashlib
import io
import ipaddress
import logging
import re
import uuid
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from PIL import Image
from fastapi import FastAPI, Query, UploadFile, File, Form, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import auth
import config
import device_store
from database import get_client, reset_client
from search import query_logs
from monitoring import report

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('api')
app = FastAPI(title='ISP Log Console')
STATIC_DIR = config.ROOT / 'static'
UPLOAD_DIR = STATIC_DIR / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

@app.middleware('http')
async def security_headers(request, call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        # Bound the complete request before multipart parsing can spool arbitrary
        # amounts to disk. Also covers chunked requests without Content-Length.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 3 * 1024 * 1024:
                return JSONResponse({'detail':'Request limit is 3 MiB'}, status_code=413)
        request._body = bytes(body)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'Cross-origin request rejected'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/')
def index():
    return FileResponse(STATIC_DIR / 'index.html')

class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    try:
        token, csrf = auth.login(body.username, body.password, request.client.host if request.client else 'unknown')
    except HTTPException:
        log.warning('event=login_failure')
        raise
    response.set_cookie('session', token, httponly=True, secure=config.COOKIE_SECURE,
                        samesite='strict', max_age=config.SESSION_SECONDS, path='/')
    return {'csrf':csrf}

@app.get('/api/me')
def me(user=Depends(auth.current)):
    return user

@app.post('/api/logout')
def logout(request: Request, response: Response, user=Depends(auth.current)):
    with device_store._conn() as c:
        c.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(request.cookies.get('session','').encode()).hexdigest(),))
    response.delete_cookie('session', path='/')
    return {'ok':True}

def search_options(kind: str='all', keyword: str=Query('',max_length=512), ip: str='',
                   start: Optional[datetime]=None, end: Optional[datetime]=None,
                   limit: int=200, cursor: Optional[str]=None,
                   port: Optional[int]=Query(None,ge=0,le=65535),
                   severity: Optional[int]=Query(None,ge=0,le=7), hostname: str='', event_type: str=''):
    return dict(kind=kind,keyword=keyword,ip=ip,start=start,end=end,limit=limit,cursor=cursor,
                port=port,severity=severity,hostname=hostname,event_type=event_type)

@app.get('/api/search')
def search_logs(options=Depends(search_options), user=Depends(auth.current)):
    try:
        return query_logs(**options)
    except HTTPException:
        raise
    except Exception:
        log.exception('event=search_failure')
        reset_client()
        raise HTTPException(503, 'Search unavailable. Check ClickHouse and apply the structured schema.')

def csv_value(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text

@app.get('/api/export')
def export(options=Depends(search_options), user=Depends(auth.current)):
    if user['role'] != 'ADMIN' and not user['can_export']:
        raise HTTPException(403, 'Export permission required')
    options.update(limit=1000, cursor=None)
    first = search_logs(options, user)
    def stream():
        batch, written = first, 0
        fields = list(batch['results'][0]) if batch['results'] else ['timestamp','router_ip','message']
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(fields)
        yield out.getvalue()
        while True:
            for row in batch['results']:
                if written >= config.EXPORT_LIMIT:
                    return
                out.seek(0); out.truncate(0)
                writer.writerow([csv_value(row.get(k)) for k in fields])
                written += 1
                yield out.getvalue()
            if not batch['next_cursor'] or written >= config.EXPORT_LIMIT:
                break
            options['cursor'] = batch['next_cursor']
            batch = query_logs(**options)
    return StreamingResponse(stream(), media_type='text/csv', headers={
        'Content-Disposition':'attachment; filename="syslog-export.csv"',
        'X-Export-Row-Limit':str(config.EXPORT_LIMIT)})

@app.get('/api/devices/counts')
def devices_counts(user=Depends(auth.current)):
    return device_store.counts()

@app.get('/api/devices')
def devices_list(status: str='all', q: str='', user=Depends(auth.current)):
    if status not in ('all','pending','approved','blocked'):
        raise HTTPException(422,'Invalid status')
    devices = device_store.list_devices(status)
    from monitoring import authorization_applied
    applied = authorization_applied()
    return {'devices':[dict(d, applied_on_server=applied) for d in devices
                       if q.lower() in (d['ip']+' '+(d['name'] or '')).lower()]}

def valid_ip(ip):
    try:
        return str(ipaddress.IPv4Address(ip))
    except ValueError:
        raise HTTPException(422, 'Enter a valid IPv4 router address')

@app.post('/api/devices/add')
def device_add(ip: str=Form(...), user=Depends(auth.admin)):
    ip = valid_ip(ip)
    device_store.set_status(ip, 'approved')
    log.info('event=device_approval actor=%s ip=%s',user['username'],ip)
    return {'ip':ip,'status':'approved'}

@app.post('/api/devices/{ip}/name')
def device_name(ip: str, name: str=Form(...,max_length=128), user=Depends(auth.admin)):
    device_store.set_name(valid_ip(ip),name)
    log.info('event=device_rename actor=%s ip=%s',user['username'],ip)
    return {'ok':True}

@app.post('/api/devices/{ip}/{action}')
def device_status(ip: str, action: str, user=Depends(auth.admin)):
    status = {'approve':'approved','block':'blocked','pending':'pending'}.get(action)
    if not status:
        raise HTTPException(404,'Unknown device action')
    ip = valid_ip(ip)
    device_store.set_status(ip,status)
    log.info('event=device_status actor=%s ip=%s status=%s',user['username'],ip,status)
    return {'ip':ip,'status':status}

BRANDING = dict(product_name='Syslog Server', logo_url='/static/presets/logo-default.svg',
                favicon_url='/static/presets/logo-default.svg', background_url='/static/presets/bg-default.svg',
                accent_color='#3b6fd6', display_timezone=config.DISPLAY_TIMEZONE)

@app.get('/api/settings/branding')
def branding():
    return {**{k:device_store.get_setting(k,v) for k,v in BRANDING.items()},
            'presets':[f'/static/presets/{p.name}' for p in (STATIC_DIR/'presets').glob('*.svg')]}

@app.post('/api/settings/branding')
def set_branding(values: dict[str,str], user=Depends(auth.admin)):
    for key,value in values.items():
        if key not in BRANDING or len(value)>256:
            raise HTTPException(422,'Invalid setting')
        if key.endswith('_url'):
            path = (config.ROOT/value.lstrip('/')).resolve()
            if not value.startswith(('/static/presets/','/static/uploads/')) or not path.is_relative_to(STATIC_DIR) or not path.is_file():
                raise HTTPException(422,'Select a local preset or uploaded image')
        if key == 'accent_color' and not re.fullmatch(r'#[0-9a-fA-F]{6}',value):
            raise HTTPException(422,'Invalid accent color')
        if key == 'display_timezone':
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise HTTPException(422,'Invalid timezone')
    for key,value in values.items():
        device_store.set_setting(key,value)
    log.info('event=branding_update actor=%s',user['username'])
    return {'ok':True}

@app.post('/api/settings/branding/upload')
def upload_branding(kind: str=Form(...), file: UploadFile=File(...), user=Depends(auth.admin)):
    if kind not in ('logo','background','favicon'):
        raise HTTPException(422,'Invalid image kind')
    content = file.file.read(2*1024*1024+1)
    if len(content)>2*1024*1024:
        raise HTTPException(413,'Image limit is 2 MiB')
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>16_000_000:
                raise ValueError()
            image.load()
            name = f'{kind}-{uuid.uuid4().hex}.png'
            image.convert('RGBA').save(UPLOAD_DIR/name,format='PNG')
    except Exception:
        raise HTTPException(422,'Upload a valid PNG, JPEG or WebP image (maximum 16 megapixels)')
    url = f'/static/uploads/{name}'
    device_store.set_setting(f'{kind}_url',url)
    log.info('event=branding_upload actor=%s kind=%s',user['username'],kind)
    return {'url':url}

@app.get('/api/users')
def users(user=Depends(auth.admin)):
    with device_store._conn() as c:
        rows = c.execute('SELECT username,full_name,role,enabled,can_export,created FROM users ORDER BY username').fetchall()
    return {'users':[dict(zip(('username','full_name','role','enabled','can_export','created'),r)) for r in rows]}

class UserEdit(BaseModel):
    username: str = Field(max_length=64)
    full_name: str = Field('',max_length=128)
    password: str = Field('',max_length=256)
    role: str='VIEWER'
    enabled: bool=True
    can_export: bool=False
    delete: bool=False

@app.post('/api/users')
def edit_user(body: UserEdit, user=Depends(auth.admin)):
    try:
        auth.save_user(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    log.info('event=user_update actor=%s target=%s',user['username'],body.username)
    return {'ok':True}

@app.get('/api/system')
def system(user=Depends(auth.admin)):
    return report()

@app.get('/api/settings/system')
def settings(user=Depends(auth.admin)):
    return dict(listener_host=config.LISTEN_HOST,listener_port=config.LISTEN_PORT,
                batch_rows=config.BATCH_MAX_ROWS,batch_seconds=config.BATCH_MAX_SECONDS,
                queue_size=config.QUEUE_SIZE,spool_limit_per_worker=config.SPOOL_MAX_BYTES,
                retention_days=config.RETENTION_DAYS,export_limit=config.EXPORT_LIMIT,
                clickhouse_host=config.CLICKHOUSE_HOST,clickhouse_port=config.CLICKHOUSE_PORT,
                database=config.CLICKHOUSE_DB, secure_cookies=config.COOKIE_SECURE,
                note='Listener settings use environment variables and require restart. TTL changes require reviewed SQL; changing RETENTION_DAYS alone does not alter existing tables.')

@app.get('/health')
def health():
    return {'status':'ok'}
