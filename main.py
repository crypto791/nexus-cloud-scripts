import os, uuid, secrets, hashlib, hmac, re
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from urllib.parse import quote
import httpx, psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from passlib.hash import pbkdf2_sha256

app = FastAPI(title='Nexus Ticket')
templates = Jinja2Templates(directory='templates')
SECRET = os.environ['SESSION_SECRET'].encode()
DB = os.environ['DATABASE_URL']
BOT = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')

@contextmanager
def db():
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        yield conn

def signature(value):
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()

def user_id(req):
    cookie = req.cookies.get('nexus_session', '')
    try:
        uid, sig = cookie.split('.', 1)
        if not hmac.compare_digest(signature(uid), sig): return None
        with db() as conn:
            row = conn.execute('select id from users where id=%s', (uid,)).fetchone()
        return str(row['id']) if row else None
    except (ValueError, TypeError): return None

def require_user(req):
    uid = user_id(req)
    if not uid: raise HTTPException(401, 'Faça login')
    return uid

def panel_owned(conn, panel_id, uid):
    row = conn.execute('select * from panels where id=%s and owner_id=%s', (panel_id, uid)).fetchone()
    if not row: raise HTTPException(404, 'Painel não encontrado')
    return row

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r'^[a-zA-Z0-9_]+$')
    password: str = Field(min_length=8, max_length=128)

class PanelInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    script: str = Field(default='', max_length=200000)

@app.get('/health')
def health(): return {'ok': True}

@app.get('/', response_class=HTMLResponse)
def home(req: Request): return RedirectResponse('/editor' if user_id(req) else '/login')

@app.get('/login', response_class=HTMLResponse)
def login_page(req: Request): return templates.TemplateResponse(req, 'editor.html')

@app.get('/editor', response_class=HTMLResponse)
def editor_page(req: Request):
    if not user_id(req): return RedirectResponse('/login')
    return templates.TemplateResponse(req, 'editor.html')

@app.post('/api/register')
def register(data: Credentials):
    uid = uuid.uuid4()
    try:
        with db() as conn:
            conn.execute('insert into users(id,username,password_hash) values (%s,%s,%s)', (uid,data.username.lower(),pbkdf2_sha256.hash(data.password)))
    except psycopg.errors.UniqueViolation: raise HTTPException(409, 'Usuário já existe')
    return {'ok': True}

@app.post('/api/login')
def login(data: Credentials):
    with db() as conn:
        row = conn.execute('select * from users where username=%s', (data.username.lower(),)).fetchone()
    if not row or not pbkdf2_sha256.verify(data.password,row['password_hash']): raise HTTPException(401, 'Credenciais inválidas')
    uid = str(row['id'])
    resp = JSONResponse({'ok': True})
    resp.set_cookie('nexus_session', uid+'.'+signature(uid), httponly=True, secure=True, samesite='lax', max_age=604800)
    return resp

@app.post('/api/logout')
def logout():
    resp = JSONResponse({'ok':True}); resp.delete_cookie('nexus_session'); return resp

@app.get('/api/panels')
def panels(req: Request):
    uid=require_user(req)
    with db() as conn:
        rows=conn.execute('select id,name,updated_at from panels where owner_id=%s order by updated_at desc',(uid,)).fetchall()
    return rows

@app.post('/api/panels')
def create_panel(req: Request, data: PanelInput):
    uid=require_user(req)
    pid='PNL-'+secrets.token_hex(5).upper()
    with db() as conn:
        conn.execute('insert into panels(id,owner_id,name,script) values (%s,%s,%s,%s)',(pid,uid,data.name,data.script))
    return {'id':pid}

@app.get('/api/panels/{pid}')
def get_panel(pid: str, req: Request):
    uid=require_user(req)
    with db() as conn: row=panel_owned(conn,pid,uid)
    return {'id':row['id'],'name':row['name'],'script':row['script']}

@app.put('/api/panels/{pid}')
async def save_panel(pid: str, req: Request, data: PanelInput):
    uid=require_user(req)
    with db() as conn:
        panel_owned(conn,pid,uid)
        conn.execute('update panels set name=%s,script=%s,updated_at=now() where id=%s',(data.name,data.script,pid))
    sent=False
    if BOT and CHAT and data.script:
        message=f'Painel: {pid}\nNome: {data.name}\nCódigo real:\n'
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                for i in range(0,len(data.script),3500):
                    response=await client.post(f'https://api.telegram.org/bot{BOT}/sendMessage',json={'chat_id':CHAT,'text':message+data.script[i:i+3500] if i==0 else data.script[i:i+3500]})
                    response.raise_for_status()
                sent=True
        except httpx.HTTPError: pass
    return {'ok':True,'telegram_sent':sent}

@app.delete('/api/panels/{pid}')
def delete_panel(pid: str, req: Request):
    uid=require_user(req)
    with db() as conn:
        panel_owned(conn,pid,uid)
        conn.execute('delete from panels where id=%s',(pid,))
    return {'ok':True}

@app.post('/api/execute/{pid}')
def issue_ticket(pid: str):
    with db() as conn:
        panel=conn.execute('select id from panels where id=%s',(pid,)).fetchone()
        if not panel: raise HTTPException(404,'Painel não encontrado')
        token=secrets.token_urlsafe(32)
        expiry=datetime.now(timezone.utc)+timedelta(seconds=60)
        conn.execute('insert into tickets(token_hash,panel_id,expires_at) values (%s,%s,%s)',(hashlib.sha256(token.encode()).hexdigest(),pid,expiry))
    return {'panel_id':pid,'ticket':token,'url':f'/{pid}/{token}','expires_in':60}

def ticket_row(pid,token):
    digest=hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        row=conn.execute('select panel_id,expires_at,confirmed_at from tickets where token_hash=%s and panel_id=%s',(digest,pid)).fetchone()
    if not row: raise HTTPException(404,'Ticket inválido')
    if row['expires_at']<=datetime.now(timezone.utc): raise HTTPException(410,'Ticket expirado')
    return row,digest

@app.get('/{pid}/{token}')
def ticket_status(pid: str, token: str):
    row,_=ticket_row(pid,token)
    return {'panel_id':pid,'valid':True,'confirmed':row['confirmed_at'] is not None,'expires_at':row['expires_at'].isoformat()}

@app.post('/{pid}/{token}/confirm')
def confirm(pid: str,token: str):
    _,digest=ticket_row(pid,token)
    with db() as conn:
        row=conn.execute('update tickets set confirmed_at=now() where token_hash=%s and confirmed_at is null and expires_at>now() returning token_hash',(digest,)).fetchone()
    if not row: raise HTTPException(409,'Ticket já confirmado ou expirado')
    return {'ok':True,'confirmed':True}
