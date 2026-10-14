import os,uuid,secrets,hashlib,hmac,re
from pathlib import Path
from datetime import datetime,timedelta,timezone
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,RedirectResponse,JSONResponse,PlainTextResponse
from pydantic import BaseModel,Field
from passlib.hash import pbkdf2_sha256

app=FastAPI(title='Nexus Cloud Scripts')
ROOT=Path(__file__).resolve().parent
DB=os.environ['DATABASE_URL']; SECRET=os.environ['SESSION_SECRET'].encode()
@contextmanager
def db():
    with psycopg.connect(DB,row_factory=dict_row) as conn: yield conn

def sig(s): return hmac.new(SECRET,s.encode(),hashlib.sha256).hexdigest()
def uid(req):
    try:
        raw=req.cookies.get('nexus_session',''); ident,signature=raw.split('.',1)
        if not hmac.compare_digest(sig(ident),signature): return None
        with db() as conn: row=conn.execute('select id from users where id=%s',(ident,)).fetchone()
        return str(row['id']) if row else None
    except (ValueError,TypeError): return None

def auth(req):
    result=uid(req)
    if not result: raise HTTPException(401,'Faça login')
    return result

def owned(conn,pid,user):
    row=conn.execute('select * from panels where id=%s and owner_id=%s',(pid,user)).fetchone()
    if not row: raise HTTPException(404,'Script não encontrado ou sem permissão')
    return row

def summary(row):
    return {k:row[k] for k in ('id','name','description','visibility','updated_at','created_at','views','executions')}
class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=32,pattern=r'^[a-zA-Z0-9_]+$')
    password:str=Field(min_length=8,max_length=128)
class ScriptInput(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    description:str=Field(default='',max_length=1000)
    script:str=Field(default='',max_length=200000)
    visibility:str=Field(default='private',pattern='^(public|private)$')
class TicketInput(BaseModel):
    seconds:int=Field(default=60,ge=30,le=3600)
@app.get('/health')
def health(): return {'ok':True,'service':'Nexus Cloud Scripts'}
@app.get('/')
def home(req:Request): return RedirectResponse('/editor' if uid(req) else '/login')
@app.get('/login')
def login_page(): return FileResponse(ROOT/'editor.html')
@app.get('/editor')
def editor(req:Request): return FileResponse(ROOT/'editor.html') if uid(req) else RedirectResponse('/login')
@app.post('/api/register')
def register(data:Credentials):
    try:
        with db() as conn: conn.execute('insert into users(id,username,password_hash) values (%s,%s,%s)',(uuid.uuid4(),data.username.lower(),pbkdf2_sha256.hash(data.password)))
    except psycopg.errors.UniqueViolation: raise HTTPException(409,'Usuário já existe')
    return {'ok':True}
@app.post('/api/login')
def login(data:Credentials):
    with db() as conn: row=conn.execute('select * from users where username=%s',(data.username.lower(),)).fetchone()
    if not row or not pbkdf2_sha256.verify(data.password,row['password_hash']): raise HTTPException(401,'Usuário ou senha inválidos')
    ident=str(row['id']); response=JSONResponse({'ok':True})
    response.set_cookie('nexus_session',ident+'.'+sig(ident),httponly=True,secure=True,samesite='lax',max_age=604800)
    return response
@app.post('/api/logout')
def logout():
    response=JSONResponse({'ok':True});response.delete_cookie('nexus_session');return response
@app.get('/api/me')
def me(req:Request):
    user=auth(req)
    with db() as conn: row=conn.execute('select username from users where id=%s',(user,)).fetchone()
    return {'username':row['username']}
@app.get('/api/panels')
def panels(req:Request):
    user=auth(req)
    with db() as conn: rows=conn.execute('select * from panels where owner_id=%s order by updated_at desc',(user,)).fetchall()
    return [summary(r) for r in rows]
@app.post('/api/panels')
def create(req:Request,data:ScriptInput):
    user=auth(req);pid='NX-'+secrets.token_hex(4).upper()
    with db() as conn: conn.execute('insert into panels(id,owner_id,name,description,script,visibility) values (%s,%s,%s,%s,%s,%s)',(pid,user,data.name,data.description,data.script,data.visibility))
    return {'id':pid}
@app.get('/api/panels/{pid}')
def get_panel(pid:str,req:Request):
    user=auth(req)
    with db() as conn: row=owned(conn,pid,user)
    return {**summary(row),'script':row['script']}
@app.put('/api/panels/{pid}')
def update(pid:str,req:Request,data:ScriptInput):
    user=auth(req)
    with db() as conn:
        owned(conn,pid,user)
        conn.execute('update panels set name=%s,description=%s,script=%s,visibility=%s,updated_at=now() where id=%s',(data.name,data.description,data.script,data.visibility,pid))
        conn.execute('delete from tickets where panel_id=%s',(pid,))
    return {'ok':True,'tickets_revoked':True}
@app.delete('/api/panels/{pid}')
def delete(pid:str,req:Request):
    user=auth(req)
    with db() as conn: owned(conn,pid,user);conn.execute('delete from panels where id=%s',(pid,))
    return {'ok':True}
@app.get('/api/community')
def community(q:str=''):
    with db() as conn:
        rows=conn.execute("select p.*,u.username as author from panels p join users u on u.id=p.owner_id where visibility='public' and (p.name ilike %s or p.description ilike %s) order by p.updated_at desc limit 100",('%'+q[:80]+'%','%'+q[:80]+'%')).fetchall()
    return [{**summary(r),'author':r['author']} for r in rows]
@app.get('/raw/{pid}')
def raw(pid:str):
    with db() as conn:
        row=conn.execute("update panels set views=views+1 where id=%s and visibility='public' returning script",(pid,)).fetchone()
    if not row: raise HTTPException(404,'RAW não disponível')
    return PlainTextResponse(row['script'],media_type='text/plain; charset=utf-8',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
@app.post('/api/panels/{pid}/tickets')
def issue(pid:str,req:Request,data:TicketInput):
    user=auth(req)
    with db() as conn:
        owned(conn,pid,user)
        token=secrets.token_urlsafe(32);expiry=datetime.now(timezone.utc)+timedelta(seconds=data.seconds)
        conn.execute('insert into tickets(token_hash,panel_id,expires_at) values (%s,%s,%s)',(hashlib.sha256(token.encode()).hexdigest(),pid,expiry))
    return {'panel_id':pid,'ticket':token,'expires_at':expiry.isoformat(),'expires_in':data.seconds}
@app.get('/api/panels/{pid}/tickets')
def tickets(pid:str,req:Request):
    user=auth(req)
    with db() as conn:
        owned(conn,pid,user)
        rows=conn.execute('select left(token_hash,12) as id,created_at,expires_at,confirmed_at,used_at from tickets where panel_id=%s order by created_at desc limit 50',(pid,)).fetchall()
    return rows
@app.delete('/api/panels/{pid}/tickets')
def revoke(pid:str,req:Request):
    user=auth(req)
    with db() as conn: owned(conn,pid,user);conn.execute('delete from tickets where panel_id=%s',(pid,))
    return {'ok':True}
def ticket_valid(conn,pid,token):
    digest=hashlib.sha256(token.encode()).hexdigest()
    row=conn.execute('select * from tickets where panel_id=%s and token_hash=%s',(pid,digest)).fetchone()
    if not row: raise HTTPException(404,'Ticket inválido')
    if row['expires_at']<=datetime.now(timezone.utc): raise HTTPException(410,'Ticket expirado')
    return row,digest
@app.get('/api/ticket/{pid}/{token}')
def ticket_status(pid:str,token:str):
    with db() as conn: row,_=ticket_valid(conn,pid,token)
    return {'valid':True,'confirmed':bool(row['confirmed_at']),'used':bool(row['used_at']),'expires_at':row['expires_at']}
@app.post('/api/ticket/{pid}/{token}/redeem')
def redeem(pid:str,token:str):
    with db() as conn:
        row,digest=ticket_valid(conn,pid,token)
        if row['used_at']: raise HTTPException(409,'Ticket já utilizado')
        claimed=conn.execute('update tickets set used_at=now(),confirmed_at=now() where token_hash=%s and used_at is null and expires_at>now() returning panel_id',(digest,)).fetchone()
        if not claimed: raise HTTPException(409,'Ticket utilizado ou expirado')
        panel=conn.execute('update panels set executions=executions+1 where id=%s returning name,script',(pid,)).fetchone()
        if not panel: raise HTTPException(404,'Script removido')
    return {'name':panel['name'],'script':panel['script']}
