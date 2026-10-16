import os,re,json,secrets,hashlib,hmac,unicodedata,urllib.request,uuid,logging
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from urllib.parse import quote
from html import escape
from fastapi import FastAPI,HTTPException,Header,Query,Request
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse,PlainTextResponse
from pydantic import BaseModel,Field
import psycopg
from psycopg.rows import dict_row
DATABASE_URL=os.getenv('DATABASE_URL')
if not DATABASE_URL: raise RuntimeError('Configure DATABASE_URL no Render')
APP_VERSION='4.6.1'; PASSWORD_ITERATIONS=600000; MAX_PROJECT_BYTES=250000; MAX_SCRIPT_LENGTH=200000; MAX_ELEMENTS=500; MAX_TABS=30; MAX_DEPTH=12; COMMUNITY_LIMIT=100
DISCORD_INVITE='https://discord.gg/rbXuR6pP6Z'
WEBHOOKS={'scripts':os.getenv('NEXUS_SCRIPTS_WEBHOOK',''),'reports':os.getenv('NEXUS_REPORTS_WEBHOOK',''),'verify':os.getenv('NEXUS_VERIFY_WEBHOOK',''),'account':os.getenv('NEXUS_ACCOUNT_VERIFY_WEBHOOK','') or os.getenv('NEXUS_ACCOUNTS_WEBHOOK','')}
ADMIN_SECRET=os.getenv('NEXUS_ADMIN_SECRET',''); ADMIN_SECOND_SECRET=os.getenv('NEXUS_SECOND_ADMIN_SECRET','')
if ADMIN_SECRET and ADMIN_SECOND_SECRET and hmac.compare_digest(ADMIN_SECRET,ADMIN_SECOND_SECRET): raise RuntimeError('Admin secrets must differ')
log=logging.getLogger('nexus'); app=FastAPI(title='Nexus Cloud API',version=APP_VERSION,docs_url=None,redoc_url=None,openapi_url=None)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=False,allow_methods=['GET','POST','PATCH','DELETE','OPTIONS'],allow_headers=['*'])
@contextmanager
def db():
    with psycopg.connect(DATABASE_URL,row_factory=dict_row) as c: yield c
def norm(s): return unicodedata.normalize('NFKC',s).casefold().strip()
def hash_password(p,salt,iterations=PASSWORD_ITERATIONS): return hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),iterations).hex()
def hash_token(t): return hashlib.sha256(t.encode()).hexdigest()
def project_hash(p):
    try: return hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    except (TypeError,ValueError): raise HTTPException(400,'Invalid project JSON')
def generate_project_code():
    a='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; return 'NXS-'+''.join(secrets.choice(a) for _ in range(4))+'-'+''.join(secrets.choice(a) for _ in range(4))
def normalize_project_code(code):
    code=code.strip().upper()
    if not re.fullmatch(r'NXS-[A-Z0-9]{4}-[A-Z0-9]{4}',code): raise HTTPException(400,'Invalid project code')
    return code

def initialize_database():
    with db() as c:
        for sql in [
            '''CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY,username TEXT NOT NULL,normalized_name TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,salt TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
            '''CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
            '''CREATE TABLE IF NOT EXISTS projects(code TEXT PRIMARY KEY,owner_id BIGINT NOT NULL REFERENCES users(id),name TEXT NOT NULL,content JSONB NOT NULL,version INTEGER NOT NULL DEFAULT 1,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
            '''CREATE TABLE IF NOT EXISTS favorites(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(user_id,project_code))''',
            '''CREATE TABLE IF NOT EXISTS reviews(id BIGSERIAL PRIMARY KEY,project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,rating SMALLINT NOT NULL CHECK(rating BETWEEN 0 AND 5),comment TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(project_code,user_id))''',
            '''CREATE TABLE IF NOT EXISTS suggestions(id BIGSERIAL PRIMARY KEY,project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,message TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','implemented')),creator_reply TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
            '''CREATE TABLE IF NOT EXISTS action_limits(user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,action TEXT NOT NULL,last_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(user_id,action))''',
            '''CREATE TABLE IF NOT EXISTS moderation_requests(id BIGSERIAL PRIMARY KEY,kind TEXT NOT NULL,requester_id BIGINT REFERENCES users(id) ON DELETE SET NULL,project_code TEXT,target_username TEXT,desired_action TEXT,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',snapshot_hash TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),resolved_at TIMESTAMPTZ)''',
            '''CREATE TABLE IF NOT EXISTS moderation_audit(id BIGSERIAL PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,reason TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
            '''CREATE TABLE IF NOT EXISTS warnings(id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,message TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''']:
            c.execute(sql)
        for col in ('views','uses'): c.execute(f'ALTER TABLE projects ADD COLUMN IF NOT EXISTS {col} BIGINT NOT NULL DEFAULT 0')
        for col in ('content_hash','verified_hash','verified_name'): c.execute(f'ALTER TABLE projects ADD COLUMN IF NOT EXISTS {col} TEXT')
        for col in ('verified','banned'): c.execute(f'ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT FALSE')
        c.execute('ALTER TABLE favorites ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ'); c.execute('UPDATE favorites SET created_at=NOW() WHERE created_at IS NULL'); c.execute('ALTER TABLE favorites ALTER COLUMN created_at SET DEFAULT NOW()'); c.execute('ALTER TABLE favorites ALTER COLUMN created_at SET NOT NULL'); c.execute('ALTER TABLE reviews ADD COLUMN IF NOT EXISTS flagged BOOLEAN NOT NULL DEFAULT FALSE')
        for sql in ['CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id)','CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC)','CREATE INDEX IF NOT EXISTS idx_projects_hash ON projects(content_hash)','CREATE INDEX IF NOT EXISTS idx_favorites_project ON favorites(project_code)','CREATE INDEX IF NOT EXISTS idx_favorites_user_created ON favorites(user_id,created_at DESC)','CREATE INDEX IF NOT EXISTS idx_reviews_project ON reviews(project_code)','CREATE INDEX IF NOT EXISTS idx_suggestions_project ON suggestions(project_code)','CREATE INDEX IF NOT EXISTS idx_suggestions_sender ON suggestions(sender_id)','CREATE INDEX IF NOT EXISTS idx_moderation_requests_status ON moderation_requests(status,created_at DESC)']: c.execute(sql)
        for p in c.execute('SELECT code,content FROM projects WHERE content_hash IS NULL').fetchall(): c.execute('UPDATE projects SET content_hash=%s WHERE code=%s',(project_hash(p['content']),p['code']))
        c.execute('UPDATE projects SET verified_name=name WHERE verified_hash IS NOT NULL AND verified_name IS NULL')
        c.execute("""CREATE TABLE IF NOT EXISTS discord_outbox (
            id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, message TEXT NOT NULL,
            filename TEXT, content TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            delivered_at TIMESTAMPTZ, last_error TEXT)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_discord_outbox_pending
            ON discord_outbox(next_attempt_at,id) WHERE delivered_at IS NULL""")
        # Migration: old V4.6.0 tickets did not have a purpose or requester.
        c.execute('''CREATE TABLE IF NOT EXISTS execution_tickets(ticket_hash TEXT PRIMARY KEY,project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,expires_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('ALTER TABLE execution_tickets ADD COLUMN IF NOT EXISTS requester_id BIGINT REFERENCES users(id) ON DELETE CASCADE')
        c.execute('ALTER TABLE execution_tickets ADD COLUMN IF NOT EXISTS purpose TEXT')
        c.execute('DELETE FROM execution_tickets WHERE requester_id IS NULL OR purpose IS NULL OR expires_at<=NOW()')
        c.execute("ALTER TABLE execution_tickets ALTER COLUMN requester_id SET NOT NULL")
        c.execute('ALTER TABLE execution_tickets ALTER COLUMN purpose SET NOT NULL')
        c.execute('CREATE INDEX IF NOT EXISTS idx_execution_expiry ON execution_tickets(expires_at)')
@app.on_event('startup')
def startup(): initialize_database()
def create_session(uid):
    token=secrets.token_urlsafe(48)
    with db() as c: c.execute('INSERT INTO sessions(token_hash,user_id) VALUES(%s,%s)',(hash_token(token),uid))
    return token
def authenticate(authorization,allow_banned=False):
    if not authorization: raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Bearer'})
    scheme,sep,token=authorization.partition(' ')
    if not sep or scheme.lower()!='bearer' or not token.strip(): raise HTTPException(401,'Invalid authorization header')
    with db() as c: user=c.execute('SELECT u.id,u.username,u.verified,u.banned FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s',(hash_token(token.strip()),)).fetchone()
    if not user: raise HTTPException(401,'Invalid session')
    if user['banned'] and not allow_banned: raise HTTPException(403,'Account suspended; contact moderation')
    return user
def admin_identity(key):
    if not isinstance(key,str) or not key: return None
    return 'OWNER' if ADMIN_SECRET and hmac.compare_digest(key,ADMIN_SECRET) else 'CO_OWNER' if ADMIN_SECOND_SECRET and hmac.compare_digest(key,ADMIN_SECOND_SECRET) else None
def admin_auth(key):
    actor=admin_identity(key)
    if not actor: raise HTTPException(403,'Administrator authorization required')
    return actor
def audit(c,action,target,reason,actor='admin'): c.execute('INSERT INTO moderation_audit(actor,action,target,reason) VALUES(%s,%s,%s,%s)',(actor,action,str(target),reason))
class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=20)
    password:str=Field(min_length=8,max_length=128)
class ProjectInput(BaseModel):
    name:str=Field(min_length=1,max_length=60)
    project:dict
class ReviewInput(BaseModel):
    rating:int=Field(ge=0,le=5)
    comment:str=Field(default='',max_length=1500)
class SuggestionInput(BaseModel): message:str=Field(min_length=5,max_length=1500)
class SuggestionUpdate(BaseModel):
    status:str
    reply:str|None=Field(default=None,max_length=1500)
class ReplyInput(BaseModel): reply:str=Field(min_length=1,max_length=1500)
class RequestInput(BaseModel): reason:str=Field(min_length=10,max_length=1500)
class ReportInput(RequestInput): details:str=Field(default='',max_length=3000)
class DecisionInput(RequestInput): approve:bool
class AdminAccountAction(RequestInput):
    username:str=Field(min_length=3,max_length=20)
    action:str=Field(pattern='^(warn|ban|unban|verify|unverify)$')
class AppealInput(RequestInput):
    username:str=Field(min_length=3,max_length=20)
    password:str=Field(min_length=8,max_length=128)
class InspectInput(BaseModel):
    name:str=Field(default='Inspection',max_length=60)
    project:dict|None=None
    scriptCode:str=Field(default='',max_length=MAX_SCRIPT_LENGTH)
class AdminLoginInput(BaseModel): secret:str=Field(min_length=1,max_length=512)
class AdminDeleteInput(RequestInput): confirm_code:str=Field(min_length=1,max_length=30)
ALLOWED_ELEMENT_TYPES={'TextLabel','TextButton','TextBox','Frame','ScrollingFrame','ImageLabel','ImageButton','List','Label','Button'}
ALLOWED_ACTIONS={'SetText','SetVisible','SetColor','Toggle','OpenTab','Print','SetFloatingImage'}
def validate_project(project):
    if not isinstance(project,dict): raise HTTPException(400,'Project must be a JSON object')
    tabs=project.get('tabs',[]); roots=project.get('roots',[]); script=project.get('scriptCode','')
    if not isinstance(tabs,list) or len(tabs)>MAX_TABS: raise HTTPException(400,'Invalid tabs')
    if not isinstance(roots,list) or len(roots)>MAX_TABS: raise HTTPException(400,'Invalid roots')
    if not isinstance(script,str) or len(script)>MAX_SCRIPT_LENGTH: raise HTTPException(400,'Invalid scriptCode')
    count=0
    def check(element,depth=0):
        nonlocal count
        count+=1
        if depth>MAX_DEPTH or count>MAX_ELEMENTS: raise HTTPException(400,'Project nesting/elements limit exceeded')
        if not isinstance(element,dict): raise HTTPException(400,'Invalid element')
        typ=element.get('type',element.get('Type'))
        if typ not in ALLOWED_ELEMENT_TYPES: raise HTTPException(400,f'Unsupported element: {typ}')
        actions=element.get('actions',element.get('Actions',[]))
        if not isinstance(actions,list): raise HTTPException(400,'Invalid actions')
        for action in actions:
            if not isinstance(action,dict) or action.get('type',action.get('Type')) not in ALLOWED_ACTIONS: raise HTTPException(400,'Unsupported action')
        children=element.get('children',element.get('Children',element.get('items',element.get('Items',[]))))
        if not isinstance(children,list): raise HTTPException(400,'Invalid children')
        for child in children: check(child,depth+1)
    for tab in tabs:
        if not isinstance(tab,dict) or not isinstance(tab.get('name',tab.get('Name')),str) or not tab.get('name',tab.get('Name')).strip(): raise HTTPException(400,'Invalid tab')
        elements=tab.get('elements',tab.get('Elements',[]))
        if not isinstance(elements,list): raise HTTPException(400,'Invalid tab elements')
        for element in elements: check(element)
    for root in roots:
        if not isinstance(root,dict) or root.get('type',root.get('Type')) not in {'ScreenGui','BillboardGui','SurfaceGui'}: raise HTTPException(400,'Invalid GUI root')
        children=root.get('children',root.get('Children',root.get('items',root.get('Items',[]))))
        if not isinstance(children,list): raise HTTPException(400,'Invalid root children')
        for element in children: check(element)
    try: size=len(json.dumps(project,ensure_ascii=False,allow_nan=False).encode())
    except (ValueError,TypeError): raise HTTPException(400,'Invalid project JSON')
    if size>MAX_PROJECT_BYTES: raise HTTPException(400,'Project exceeds 250 KB')
    if not tabs and not roots and not script.strip(): raise HTTPException(400,'Empty project')
BLOCKED_WORDS={'porra','caralho','merda','foda','fdp','puta','putaria','buceta','cacete'}
def clean_for_filter(value):
    value=unicodedata.normalize('NFKD',value.casefold()); value=''.join(ch for ch in value if not unicodedata.combining(ch))
    return value.translate(str.maketrans({'0':'o','1':'i','3':'e','4':'a','5':'s','@':'a','$':'s'}))
def moderate(value,field,allow_empty=False):
    value=value.strip()
    if not value and not allow_empty: raise HTTPException(400,f'{field} cannot be empty')
    normalized=clean_for_filter(value)
    if any(word in BLOCKED_WORDS for word in re.findall(r'[a-z]+',normalized)): raise HTTPException(400,f'{field} contains prohibited language')
    if re.search(r'(.)\1{11,}',normalized): raise HTTPException(400,f'{field} contains spam')
    return value
def throttle(c,uid,action,seconds):
    row=c.execute("INSERT INTO action_limits(user_id,action,last_at) VALUES(%s,%s,NOW()) ON CONFLICT(user_id,action) DO UPDATE SET last_at=NOW() WHERE action_limits.last_at <= NOW()-(%s*INTERVAL '1 second') RETURNING last_at",(uid,action,seconds)).fetchone()
    if not row: raise HTTPException(429,f'Too many requests. Wait {seconds} seconds.')
def require_project(c,code,lock=False):
    row=c.execute('SELECT code,owner_id,name FROM projects WHERE code=%s'+(' FOR UPDATE' if lock else ''),(code,)).fetchone()
    if not row: raise HTTPException(404,'Project not found')
    return row
def rating_stats(c,code):
    row=c.execute('SELECT COUNT(*) AS total,AVG(rating)::float AS average FROM reviews WHERE project_code=%s AND flagged=FALSE',(code,)).fetchone()
    return {'average_rating':round(row['average'],2) if row['average'] is not None else None,'rating_percentage':round(row['average']*20,1) if row['average'] is not None else None,'total_ratings':row['total']}
def project_public_row(project):
    r=dict(project); r['projectCode']=r['code']; r['creator']=r.get('username',''); r['project_verified']=bool(r.get('verified_hash') and r['verified_hash']==r.get('content_hash') and r.get('verified_name')==r.get('name')); r['creator_verified']=bool(r.get('creator_verified',False))
    for k in ('content_hash','verified_hash','verified_name'): r.pop(k,None)
    return r
DISCORD_QUEUE_LOCK=4610461
DISCORD_MAX_ATTEMPTS=20

def webhook(kind,message,filename=None,content=None):
    if kind not in WEBHOOKS or not WEBHOOKS[kind]:
        log.warning('Discord webhook %s not configured',kind)
        return False
    try:
        with db() as c:
            row=c.execute("""INSERT INTO discord_outbox(kind,message,filename,content)
                VALUES(%s,%s,%s,%s) RETURNING id""",
                (kind,message[:1900],filename,content)).fetchone()
        log.info('Discord notification queued id=%s kind=%s',row['id'],kind)
        return True
    except Exception:
        log.exception('Discord queue insert failed kind=%s',kind)
        return False

def discord_request(kind,message,filename=None,content=None):
    url=WEBHOOKS.get(kind)
    if not url: return False,None,'not configured',False
    payload={'content':message[:1900],'allowed_mentions':{'parse':[]}}
    if filename is None:
        body=json.dumps(payload).encode('utf-8')
        headers={'Content-Type':'application/json'}
    else:
        boundary='----Nexus'+uuid.uuid4().hex
        filename=re.sub(r'[^A-Za-z0-9_.-]','_',filename)[:100]
        body=(('--'+boundary+'\\r\\nContent-Disposition: form-data; name="payload_json"\\r\\nContent-Type: application/json\\r\\n\\r\\n'+json.dumps(payload)+'\\r\\n--'+boundary+'\\r\\nContent-Disposition: form-data; name="files[0]"; filename="'+filename+'"\\r\\nContent-Type: text/plain\\r\\n\\r\\n').encode('utf-8')+(content or '').encode('utf-8')+('\\r\\n--'+boundary+'--\\r\\n').encode('utf-8'))
        headers={'Content-Type':'multipart/form-data; boundary='+boundary}
    headers['User-Agent']='NexusCloud/4.6.1'
    try:
        req=urllib.request.Request(url,data=body,headers=headers,method='POST')
        with urllib.request.urlopen(req,timeout=15) as response: response.read()
        return True,None,'delivered',False
    except urllib.error.HTTPError as exc:
        raw=exc.read(8192).decode('utf-8',errors='replace')
        if exc.code==429:
            retry_after=None
            global_limit=exc.headers.get('X-RateLimit-Global','').lower()=='true'
            try:
                data=json.loads(raw)
                if isinstance(data,dict):
                    if data.get('retry_after') is not None: retry_after=float(data['retry_after'])
                    global_limit=global_limit or bool(data.get('global',False))
            except (ValueError,TypeError): pass
            try:
                value=exc.headers.get('Retry-After')
                if value: retry_after=max(retry_after or 0,float(value))
            except (ValueError,TypeError): pass
            retry_after=max(1.0,retry_after if retry_after is not None else 60.0)
            log.warning('Discord 429 kind=%s retry_after=%.2fs global=%s content_type=%s server=%s body_preview=%s',kind,retry_after,global_limit,exc.headers.get('Content-Type',''),exc.headers.get('Server',''),raw[:350])
            return False,retry_after,'HTTP 429',global_limit
        log.warning('Discord delivery failed kind=%s HTTP=%s',kind,exc.code)
        if exc.code in (400,401,403,404): return False,None,'HTTP '+str(exc.code),False
        return False,30.0 if exc.code>=500 else None,'HTTP '+str(exc.code),False
    except Exception as exc:
        log.warning('Discord connection failed kind=%s error=%s',kind,type(exc).__name__)
        return False,30.0,type(exc).__name__,False

def process_discord_queue():
    with psycopg.connect(DATABASE_URL,row_factory=dict_row,autocommit=True) as lock_connection:
        locked=lock_connection.execute('SELECT pg_try_advisory_lock(%s) AS locked',(DISCORD_QUEUE_LOCK,)).fetchone()['locked']
        if not locked: return
        try:
            with db() as c:
                job=c.execute("""SELECT * FROM discord_outbox
                    WHERE delivered_at IS NULL AND next_attempt_at<=NOW()
                    AND attempts<%s ORDER BY id LIMIT 1""",(DISCORD_MAX_ATTEMPTS,)).fetchone()
            if not job: return
            success,delay,error,global_limit=discord_request(job['kind'],job['message'],job['filename'],job['content'])
            if success:
                with db() as c:
                    c.execute('UPDATE discord_outbox SET delivered_at=NOW(),last_error=NULL WHERE id=%s',(job['id'],))
                log.info('Discord notification delivered id=%s kind=%s',job['id'],job['kind'])
                return
            attempts=job['attempts']+1
            if delay is None:
                attempts=DISCORD_MAX_ATTEMPTS
                delay=0
            elif error=='HTTP 429':
                delay=min(3600,delay+random.uniform(.5,1.5))
            else:
                delay=min(3600,max(delay,min(2**min(attempts,8),300)))
            with db() as c:
                c.execute("""UPDATE discord_outbox SET attempts=%s,
                    next_attempt_at=NOW()+(%s*INTERVAL '1 second'),
                    last_error=%s WHERE id=%s""",(attempts,delay,error,job['id']))
                if global_limit:
                    c.execute("""UPDATE discord_outbox SET next_attempt_at=GREATEST(
                        next_attempt_at,NOW()+(%s*INTERVAL '1 second'))
                        WHERE delivered_at IS NULL""",(delay,))
            log.warning('Discord notification postponed id=%s kind=%s attempt=%s delay=%.2fs error=%s',job['id'],job['kind'],attempts,delay,error)
        finally:
            lock_connection.execute('SELECT pg_advisory_unlock(%s)',(DISCORD_QUEUE_LOCK,))

def discord_queue_worker():
    while True:
        try: process_discord_queue()
        except Exception: log.exception('Discord queue worker error')
        time.sleep(2)

@app.on_event('startup')
def start_discord_queue():
    threading.Thread(target=discord_queue_worker,name='nexus-discord-queue',daemon=True).start()

def notify_project(action,code,username,name,version,project): webhook('scripts',f'{action.upper()} | {code} | @{username} | {name} | version {version}',code+'-v'+str(version)+'.lua',project.get('scriptCode',''))
@app.get('/')
def home(): return {'service':'Nexus Cloud','status':'online','version':APP_VERSION,'discord':DISCORD_INVITE,'project_access':'authentication_required','web_login':'/login','web_editor':'/editor'}
@app.get('/health')
def health():
    with db() as c: c.execute('SELECT 1').fetchone()
    return {'status':'healthy','database':'connected','version':APP_VERSION}
@app.post('/profiles')
def register(data:Credentials):
    username=data.username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{3,20}',username): raise HTTPException(400,'Username must contain 3-20 letters, numbers or underscores')
    salt=secrets.token_hex(16)
    try:
        with db() as c: user=c.execute('INSERT INTO users(username,normalized_name,password_hash,salt) VALUES(%s,%s,%s,%s) RETURNING id,username',(username,norm(username),hash_password(data.password,salt),salt)).fetchone()
    except psycopg.errors.UniqueViolation: raise HTTPException(409,'Username unavailable')
    return {'user':{**user,'verified':False},'token':create_session(user['id'])}
@app.post('/login')
def login(data:Credentials):
    with db() as c: user=c.execute('SELECT id,username,password_hash,salt,verified,banned FROM users WHERE normalized_name=%s',(norm(data.username),)).fetchone()
    if not user or not hmac.compare_digest(hash_password(data.password,user['salt']),user['password_hash']): raise HTTPException(401,'Invalid credentials')
    if user['banned']: raise HTTPException(403,'Account suspended; contact moderation')
    return {'user':{'id':user['id'],'username':user['username'],'verified':bool(user['verified'])},'token':create_session(user['id'])}
@app.get('/me')
def me(authorization:str|None=Header(default=None)): return authenticate(authorization)
@app.post('/logout')
def logout(authorization:str|None=Header(default=None)):
    authenticate(authorization); token=authorization.partition(' ')[2].strip()
    with db() as c: c.execute('DELETE FROM sessions WHERE token_hash=%s',(hash_token(token),))
    return {'success':True,'message':'Session revoked'}
@app.post('/projects/inspect')
def inspect_project(data:InspectInput,authorization:str|None=Header(default=None)):
    authenticate(authorization); source=data.scriptCode; structure=None
    if data.project is not None:
        validate_project(data.project); structure={'valid':True}; source=data.project.get('scriptCode','')
        if not isinstance(source,str): raise HTTPException(400,'Invalid scriptCode')
    patterns=((r'\bloadstring\s*\(','Dynamic code loading detected'),(r'\bgetfenv\s*\(','Environment access detected'),(r'\bsetfenv\s*\(','Environment modification detected'),(r'\bHttpGet\s*\(','Remote source loading detected'),(r'\brequest\s*\(','HTTP request usage detected'))
    return {'success':True,'syntax_verified':False,'runtime_verified':False,'inspection':'heuristic_only','structure':structure,'warnings':[msg for pat,msg in patterns if re.search(pat,source,re.IGNORECASE)],'message':'Server does not compile or execute Luau; validate syntax in a trusted Luau runtime.'}
@app.post('/projects')
def publish_project(data:ProjectInput,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); name=moderate(data.name,'Project name'); validate_project(data.project); digest=project_hash(data.project); serialized=json.dumps(data.project,ensure_ascii=False,allow_nan=False); code=None
    with db() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)',(int(digest[:15],16),)); duplicate=c.execute('SELECT code FROM projects WHERE content_hash=%s LIMIT 1',(digest,)).fetchone()
        if duplicate: raise HTTPException(409,f'Duplicate project: {duplicate["code"]}')
        throttle(c,user['id'],'publish',30)
        for _ in range(10):
            candidate=generate_project_code()
            if c.execute('SELECT 1 FROM projects WHERE code=%s',(candidate,)).fetchone(): continue
            c.execute('INSERT INTO projects(code,owner_id,name,content,content_hash) VALUES(%s,%s,%s,%s::jsonb,%s)',(candidate,user['id'],name,serialized,digest)); code=candidate; break
        if not code: raise HTTPException(503,'Could not generate a unique project code')
    notify_project('publish',code,user['username'],name,1,data.project)
    return {'success':True,'code':code,'projectCode':code,'version':1}
@app.get('/public/projects')
@app.get('/community/projects')
def public_projects(search:str=Query(default='',max_length=100),q:str=Query(default='',max_length=100)):
    term=(search or q).strip(); wild='%'+term+'%'
    with db() as c: rows=c.execute('''SELECT p.code,p.name,u.username,u.verified AS creator_verified,p.verified_hash,p.verified_name,p.content_hash,p.version,p.views,p.uses,p.created_at,p.updated_at,(SELECT COUNT(*) FROM favorites f WHERE f.project_code=p.code) AS favorites,(SELECT COUNT(*) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS total_ratings,(SELECT ROUND(AVG(r.rating)::numeric,2) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS average_rating,(SELECT ROUND(AVG(r.rating)::numeric*20,1) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS rating_percentage FROM projects p JOIN users u ON u.id=p.owner_id WHERE (%s='' OR p.name ILIKE %s OR u.username ILIKE %s OR p.code ILIKE %s) ORDER BY p.updated_at DESC,p.code ASC LIMIT %s''',(term,wild,wild,wild,COMMUNITY_LIMIT)).fetchall()
    return {'success':True,'projects':[project_public_row(p) for p in rows],'total':len(rows)}
def project_details(code,user):
    code=normalize_project_code(code)
    with db() as c:
        p=c.execute('''UPDATE projects SET views=views+1 WHERE code=%s RETURNING code,owner_id,name,content,content_hash,verified_hash,verified_name,version,views,uses,created_at,updated_at''',(code,)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
        creator=c.execute('SELECT username,verified FROM users WHERE id=%s',(p['owner_id'],)).fetchone(); favorites=c.execute('SELECT COUNT(*) AS total FROM favorites WHERE project_code=%s',(code,)).fetchone()['total']; stats=rating_stats(c,code)
    username=creator['username'] if creator else 'Unknown'; owner=bool(user and p['owner_id']==user['id'])
    result={'code':p['code'],'projectCode':p['code'],'name':p['name'],'creator':username,'username':username,'creator_verified':bool(creator and creator['verified']),'project_verified':bool(p['verified_hash'] and p['verified_hash']==p['content_hash'] and p['verified_name']==p['name']),'version':p['version'],'views':p['views'],'uses':p['uses'],'favorites':favorites,'created_at':p['created_at'],'updated_at':p['updated_at'],'is_owner':owner,**stats}
    # Never leak source from the ordinary project endpoint.
    if owner: result['project']=p['content']
    return result
@app.get('/projects/{code}')
def get_project(code:str,authorization:str|None=Header(default=None)): return project_details(code,authenticate(authorization) if authorization else None)
@app.get('/my/projects')
def my_projects(authorization:str|None=Header(default=None)):
    user=authenticate(authorization)
    with db() as c: rows=c.execute('''SELECT p.code,p.name,p.verified_hash,p.verified_name,p.content_hash,p.version,p.views,p.uses,p.created_at,p.updated_at,(SELECT COUNT(*) FROM favorites f WHERE f.project_code=p.code) AS favorites,(SELECT COUNT(*) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS total_ratings,(SELECT ROUND(AVG(r.rating)::numeric*20,1) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS rating_percentage FROM projects p WHERE p.owner_id=%s ORDER BY p.updated_at DESC''',(user['id'],)).fetchall()
    projects=[]
    for p in rows:
        item=dict(p); item['projectCode']=item['code']; item['project_verified']=bool(item['verified_hash'] and item['verified_hash']==item['content_hash'] and item['verified_name']==item['name']); item['creator_verified']=bool(user['verified'])
        for k in ('content_hash','verified_hash','verified_name'): item.pop(k,None)
        projects.append(item)
    return {'projects':projects,'total':len(projects)}
@app.patch('/projects/{code}')
def update_project(code:str,data:ProjectInput,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code); name=moderate(data.name,'Project name'); validate_project(data.project); digest=project_hash(data.project); serialized=json.dumps(data.project,ensure_ascii=False,allow_nan=False)
    with db() as c:
        p=require_project(c,code,True)
        if p['owner_id']!=user['id']: raise HTTPException(403,'Not project owner')
        c.execute('SELECT pg_advisory_xact_lock(%s)',(int(digest[:15],16),)); duplicate=c.execute('SELECT code FROM projects WHERE content_hash=%s AND code<>%s LIMIT 1',(digest,code)).fetchone()
        if duplicate: raise HTTPException(409,f'Duplicate project: {duplicate["code"]}')
        result=c.execute('''UPDATE projects SET name=%s,content=%s::jsonb,content_hash=%s,verified_hash=CASE WHEN content_hash=%s AND name=%s THEN verified_hash ELSE NULL END,verified_name=CASE WHEN content_hash=%s AND name=%s THEN verified_name ELSE NULL END,version=version+1,updated_at=NOW() WHERE code=%s RETURNING code,version,updated_at''',(name,serialized,digest,digest,name,digest,name,code)).fetchone()
    notify_project('update',code,user['username'],name,result['version'],data.project); return {'success':True,**result}
@app.delete('/projects/{code}')
def delete_project(code:str,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code)
    with db() as c: row=c.execute('DELETE FROM projects WHERE code=%s AND owner_id=%s RETURNING code',(code,user['id'])).fetchone()
    if not row: raise HTTPException(404,'Project not found or not owned')
    return {'success':True,'deleted':row['code']}
@app.post('/projects/{code}/favorite')
def toggle_favorite(code:str,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code)
    with db() as c:
        p=require_project(c,code,True)
        if p['owner_id']==user['id']: raise HTTPException(403,'Cannot favorite your own project')
        removed=c.execute('DELETE FROM favorites WHERE user_id=%s AND project_code=%s RETURNING user_id',(user['id'],code)).fetchone()
        if not removed: c.execute('INSERT INTO favorites(user_id,project_code) VALUES(%s,%s)',(user['id'],code))
        total=c.execute('SELECT COUNT(*) AS total FROM favorites WHERE project_code=%s',(code,)).fetchone()['total']
    return {'success':True,'code':code,'favorited':not bool(removed),'favorites':total}
@app.get('/my/favorites')
def my_favorites(authorization:str|None=Header(default=None)):
    user=authenticate(authorization)
    with db() as c: rows=c.execute('''SELECT p.code,p.name,u.username,u.verified AS creator_verified,p.verified_hash,p.verified_name,p.content_hash,p.version,p.views,p.uses,p.updated_at,f.created_at AS favorited_at,(SELECT COUNT(*) FROM favorites fx WHERE fx.project_code=p.code) AS favorites,(SELECT COUNT(*) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS total_ratings,(SELECT ROUND(AVG(r.rating)::numeric*20,1) FROM reviews r WHERE r.project_code=p.code AND r.flagged=FALSE) AS rating_percentage FROM favorites f JOIN projects p ON p.code=f.project_code JOIN users u ON u.id=p.owner_id WHERE f.user_id=%s ORDER BY f.created_at DESC,p.code ASC''',(user['id'],)).fetchall()
    projects=[project_public_row(p) for p in rows]; return {'success':True,'projects':projects,'favorites':projects,'total':len(projects)}
@app.get('/script',response_class=PlainTextResponse)
def get_script(): return 'loadstring(game:HttpGet("https://pastefy.app/vEQn3cR3/raw"))()'
@app.post('/projects/{code}/use')
def record_project_use(code:str,authorization:str|None=Header(default=None)):
    authenticate(authorization); code=normalize_project_code(code)
    with db() as c: row=c.execute('UPDATE projects SET uses=uses+1 WHERE code=%s RETURNING uses',(code,)).fetchone()
    if not row: raise HTTPException(404,'Project not found')
    return {'success':True,'code':code,'uses':row['uses']}
@app.get('/projects/{code}/ratings')
def get_ratings(code:str):
    code=normalize_project_code(code)
    with db() as c: require_project(c,code); stats=rating_stats(c,code)
    return {'success':True,'code':code,**stats}
@app.get('/projects/{code}/reviews')
def get_reviews(code:str,limit:int=Query(default=50,ge=1,le=100),offset:int=Query(default=0,ge=0)):
    code=normalize_project_code(code)
    with db() as c:
        require_project(c,code); rows=c.execute('''SELECT r.id,u.username,r.rating,r.comment,r.created_at,r.updated_at FROM reviews r JOIN users u ON u.id=r.user_id WHERE r.project_code=%s AND r.flagged=FALSE ORDER BY r.updated_at DESC,r.id DESC LIMIT %s OFFSET %s''',(code,limit,offset)).fetchall(); stats=rating_stats(c,code)
    return {'success':True,'code':code,'reviews':rows,**stats}
def save_review(code,data,authorization):
    user=authenticate(authorization); code=normalize_project_code(code); comment=moderate(data.comment,'Comment',True)
    with db() as c:
        p=require_project(c,code)
        if p['owner_id']==user['id']: raise HTTPException(403,'Cannot review your own project')
        throttle(c,user['id'],f'review:{code}',60)
        recent=c.execute("SELECT COUNT(*) AS n FROM reviews r JOIN users u ON u.id=r.user_id WHERE r.project_code=%s AND u.created_at>NOW()-INTERVAL '1 day' AND r.created_at>NOW()-INTERVAL '1 day'",(code,)).fetchone()['n']; account=c.execute('SELECT created_at FROM users WHERE id=%s',(user['id'],)).fetchone(); suspicious=recent>=5 and account['created_at']>datetime.now(timezone.utc)-timedelta(days=1)
        row=c.execute('''INSERT INTO reviews(project_code,user_id,rating,comment,flagged) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(project_code,user_id) DO UPDATE SET rating=EXCLUDED.rating,comment=EXCLUDED.comment,flagged=reviews.flagged OR EXCLUDED.flagged,updated_at=NOW() RETURNING flagged''',(code,user['id'],data.rating,comment,suspicious)).fetchone(); stats=rating_stats(c,code)
    return {'success':True,'code':code,'your_rating':data.rating,'your_comment':comment,'under_review':bool(row['flagged']),**stats}
@app.post('/projects/{code}/reviews')
def post_review(code:str,data:ReviewInput,authorization:str|None=Header(default=None)): return save_review(code,data,authorization)
@app.patch('/projects/{code}/reviews/me')
def patch_review(code:str,data:ReviewInput,authorization:str|None=Header(default=None)): return save_review(code,data,authorization)
@app.delete('/projects/{code}/reviews/me')
def delete_review(code:str,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code)
    with db() as c:
        row=c.execute('DELETE FROM reviews WHERE project_code=%s AND user_id=%s RETURNING id',(code,user['id'])).fetchone()
        if not row: raise HTTPException(404,'Review not found')
        stats=rating_stats(c,code)
    return {'success':True,'code':code,**stats}
@app.post('/projects/{code}/suggestions')
def post_suggestion(code:str,data:SuggestionInput,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code); message=moderate(data.message,'Suggestion')
    with db() as c:
        p=require_project(c,code)
        if p['owner_id']==user['id']: raise HTTPException(403,'Cannot suggest to your own project')
        throttle(c,user['id'],f'suggestion:{code}',60); duplicate=c.execute("SELECT id FROM suggestions WHERE project_code=%s AND sender_id=%s AND message=%s AND created_at>NOW()-INTERVAL '1 day' LIMIT 1",(code,user['id'],message)).fetchone()
        if duplicate: raise HTTPException(409,'Suggestion already sent')
        row=c.execute('INSERT INTO suggestions(project_code,sender_id,message) VALUES(%s,%s,%s) RETURNING id,project_code,message,status,created_at',(code,user['id'],message)).fetchone()
    return {'success':True,'suggestion':row}
@app.get('/projects/{code}/suggestions')
def project_suggestions(code:str,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code)
    with db() as c:
        p=require_project(c,code)
        if p['owner_id']!=user['id']: raise HTTPException(403,'Only project owner can view inbox')
        rows=c.execute('''SELECT s.id,s.project_code,u.username AS sender,s.message,s.status,s.creator_reply,s.created_at,s.updated_at FROM suggestions s JOIN users u ON u.id=s.sender_id WHERE s.project_code=%s ORDER BY s.created_at DESC LIMIT 100''',(code,)).fetchall()
    return {'suggestions':rows,'total':len(rows)}
@app.get('/my/suggestions/inbox')
def suggestions_inbox(authorization:str|None=Header(default=None)):
    user=authenticate(authorization)
    with db() as c: rows=c.execute('''SELECT s.id,s.project_code,p.name AS project_name,u.username AS sender,s.message,s.status,s.creator_reply,s.created_at,s.updated_at FROM suggestions s JOIN projects p ON p.code=s.project_code JOIN users u ON u.id=s.sender_id WHERE p.owner_id=%s ORDER BY s.created_at DESC LIMIT 100''',(user['id'],)).fetchall()
    return {'suggestions':rows,'total':len(rows)}
@app.get('/my/suggestions/sent')
def suggestions_sent(authorization:str|None=Header(default=None)):
    user=authenticate(authorization)
    with db() as c: rows=c.execute('''SELECT s.id,s.project_code,p.name AS project_name,s.message,s.status,s.creator_reply,s.created_at,s.updated_at FROM suggestions s JOIN projects p ON p.code=s.project_code WHERE s.sender_id=%s ORDER BY s.created_at DESC LIMIT 100''',(user['id'],)).fetchall()
    return {'suggestions':rows,'total':len(rows)}
def edit_suggestion(suggestion_id,user,status=None,reply=None):
    with db() as c:
        s=c.execute('SELECT s.id,s.project_code,p.owner_id FROM suggestions s JOIN projects p ON p.code=s.project_code WHERE s.id=%s FOR UPDATE OF s',(suggestion_id,)).fetchone()
        if not s: raise HTTPException(404,'Suggestion not found')
        if s['owner_id']!=user['id']: raise HTTPException(403,'Only project owner can respond')
        if status is None: result=c.execute('UPDATE suggestions SET creator_reply=%s,updated_at=NOW() WHERE id=%s RETURNING id,project_code,message,status,creator_reply,updated_at',(reply,suggestion_id)).fetchone()
        elif reply is None: result=c.execute('UPDATE suggestions SET status=%s,updated_at=NOW() WHERE id=%s RETURNING id,project_code,message,status,creator_reply,updated_at',(status,suggestion_id)).fetchone()
        else: result=c.execute('UPDATE suggestions SET status=%s,creator_reply=%s,updated_at=NOW() WHERE id=%s RETURNING id,project_code,message,status,creator_reply,updated_at',(status,reply,suggestion_id)).fetchone()
    return {'success':True,'suggestion':result}
@app.patch('/suggestions/{suggestion_id}')
def update_suggestion(suggestion_id:int,data:SuggestionUpdate,authorization:str|None=Header(default=None)):
    user=authenticate(authorization)
    if data.status not in {'pending','accepted','rejected','implemented'}: raise HTTPException(400,'Invalid status')
    return edit_suggestion(suggestion_id,user,data.status,moderate(data.reply,'Reply',True) if data.reply is not None else None)
@app.post('/suggestions/{suggestion_id}/reply')
def reply_suggestion(suggestion_id:int,data:ReplyInput,authorization:str|None=Header(default=None)): return edit_suggestion(suggestion_id,authenticate(authorization),reply=moderate(data.reply,'Reply'))
@app.post('/projects/{code}/verification-request')
def request_project_verification(code:str,data:RequestInput,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code); reason=moderate(data.reason,'Reason')
    with db() as c:
        p=c.execute('SELECT owner_id,content_hash,version,name,verified_hash,verified_name FROM projects WHERE code=%s',(code,)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
        if p['owner_id']!=user['id']: raise HTTPException(403,'Only the creator can request verification')
        if p['verified_hash']==p['content_hash'] and p['verified_name']==p['name']: raise HTTPException(409,'Project already verified')
        if c.execute("SELECT id FROM moderation_requests WHERE kind='project_verify' AND status='pending' AND project_code=%s AND snapshot_hash=%s LIMIT 1",(code,p['content_hash'])).fetchone(): raise HTTPException(409,'Verification request already pending')
        throttle(c,user['id'],'project_verification',3600); row=c.execute("INSERT INTO moderation_requests(kind,requester_id,project_code,reason,snapshot_hash,target_username) VALUES('project_verify',%s,%s,%s,%s,%s) RETURNING id",(user['id'],code,reason,p['content_hash'],p['name'])).fetchone()
    webhook('verify',f"Project verification request #{row['id']} | {code} | @{user['username']} | v{p['version']} | {reason}"); return {'success':True,'request_id':row['id'],'status':'pending'}
@app.post('/projects/{code}/reports')
def report_project(code:str,data:ReportInput,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(code); reason=moderate(data.reason,'Report reason'); details=moderate(data.details,'Details',True)
    with db() as c:
        p=c.execute('SELECT p.name,p.version,p.content,p.content_hash,u.username FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.code=%s',(code,)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
        throttle(c,user['id'],'report',300); row=c.execute("INSERT INTO moderation_requests(kind,requester_id,project_code,reason,snapshot_hash) VALUES('report',%s,%s,%s,%s) RETURNING id",(user['id'],code,(reason+' | '+details)[:4500],p['content_hash'])).fetchone()
    webhook('reports',f"Report #{row['id']} | {code} | creator @{p['username']} | reporter @{user['username']} | v{p['version']} | {reason} | {details}",code+'-snapshot.lua',p['content'].get('scriptCode','')); return {'success':True,'request_id':row['id'],'status':'pending'}
@app.post('/account/verification-request')
def request_account_verification(data:RequestInput,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); reason=moderate(data.reason,'Reason')
    if user['verified']: raise HTTPException(409,'Account already verified')
    with db() as c:
        if c.execute("SELECT id FROM moderation_requests WHERE kind='account_verify' AND status='pending' AND requester_id=%s LIMIT 1",(user['id'],)).fetchone(): raise HTTPException(409,'Account verification request already pending')
        throttle(c,user['id'],'account_verification',86400); row=c.execute("INSERT INTO moderation_requests(kind,requester_id,target_username,desired_action,reason) VALUES('account_verify',%s,%s,'add',%s) RETURNING id",(user['id'],user['username'],reason)).fetchone()
    webhook('account',f"Creator badge request #{row['id']} | @{user['username']} | {reason}"); return {'success':True,'request_id':row['id'],'status':'pending'}
@app.get('/my/moderation')
def my_moderation(authorization:str|None=Header(default=None)):
    user=authenticate(authorization,True)
    with db() as c:
        requests=c.execute('SELECT id,kind,project_code,status,reason,created_at,resolved_at FROM moderation_requests WHERE requester_id=%s ORDER BY created_at DESC LIMIT 50',(user['id'],)).fetchall(); warnings=c.execute('SELECT id,message,created_at FROM warnings WHERE user_id=%s ORDER BY created_at DESC LIMIT 50',(user['id'],)).fetchall()
    return {'requests':requests,'warnings':warnings,'verified':bool(user['verified']),'banned':bool(user['banned'])}
@app.post('/account/appeal')
def appeal(data:AppealInput):
    with db() as c:
        user=c.execute('SELECT id,username,password_hash,salt,banned FROM users WHERE normalized_name=%s',(norm(data.username),)).fetchone()
        if not user or not hmac.compare_digest(hash_password(data.password,user['salt']),user['password_hash']): raise HTTPException(401,'Invalid credentials')
        if not user['banned']: raise HTTPException(409,'Account is not suspended')
        if c.execute("SELECT id FROM moderation_requests WHERE kind='appeal' AND status='pending' AND requester_id=%s LIMIT 1",(user['id'],)).fetchone(): raise HTTPException(409,'Appeal already pending')
        throttle(c,user['id'],'appeal',86400); reason=moderate(data.reason,'Appeal'); row=c.execute("INSERT INTO moderation_requests(kind,requester_id,target_username,reason) VALUES('appeal',%s,%s,%s) RETURNING id",(user['id'],user['username'],reason)).fetchone()
    webhook('account',f"Ban appeal #{row['id']} | @{user['username']} | {reason}"); return {'success':True,'request_id':row['id'],'status':'pending'}
@app.post('/admin/auth')
def admin_login(data:AdminLoginInput):
    actor=admin_auth(data.secret); return {'success':True,'authenticated':True,'profile':{'username':actor,'role':'ADMIN','fixed':True},'version':APP_VERSION}
@app.get('/admin/status')
def admin_status(x_nexus_admin_key:str|None=Header(default=None)):
    actor=admin_auth(x_nexus_admin_key)
    with db() as c:
        counts=c.execute("SELECT COUNT(*) FILTER(WHERE status='pending') AS pending,COUNT(*) FILTER(WHERE kind='report' AND status='pending') AS reports FROM moderation_requests").fetchone(); projects=c.execute('SELECT COUNT(*) AS n FROM projects').fetchone()['n']; accounts=c.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
    return {'success':True,'authenticated':True,'admin':actor,'projects':projects,'accounts':accounts,**counts,'webhooks_configured':{k:bool(v) for k,v in WEBHOOKS.items()}}
@app.get('/admin/projects')
def admin_projects(search:str=Query(default='',max_length=100),limit:int=Query(default=100,ge=1,le=200),x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key); term='%'+search.strip()+'%'
    with db() as c: rows=c.execute('''SELECT p.code,p.name,u.username,p.version,p.views,p.uses,p.created_at,p.updated_at,(p.verified_hash IS NOT NULL AND p.verified_hash=p.content_hash AND p.verified_name=p.name) AS project_verified,u.verified AS creator_verified FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.code ILIKE %s OR p.name ILIKE %s OR u.username ILIKE %s ORDER BY p.updated_at DESC LIMIT %s''',(term,term,term,limit)).fetchall()
    return {'success':True,'projects':rows,'total':len(rows)}
@app.get('/admin/accounts')
def admin_accounts(search:str=Query(default='',max_length=100),limit:int=Query(default=100,ge=1,le=200),x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key); term='%'+search.strip()+'%'
    with db() as c: rows=c.execute('SELECT u.id,u.username,u.verified,u.banned,u.created_at,(SELECT COUNT(*) FROM projects p WHERE p.owner_id=u.id) AS projects FROM users u WHERE u.username ILIKE %s ORDER BY u.created_at DESC LIMIT %s',(term,limit)).fetchall()
    return {'success':True,'accounts':rows,'total':len(rows)}
@app.get('/admin/requests')
def admin_requests(kind:str=Query(default=''),status:str=Query(default='pending'),x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key)
    if status not in {'pending','approved','rejected','all'}: raise HTTPException(400,'Invalid status')
    if kind not in {'','project_verify','account_verify','report','appeal'}: raise HTTPException(400,'Invalid kind')
    with db() as c: rows=c.execute("SELECT r.*,u.username AS requester_username FROM moderation_requests r LEFT JOIN users u ON u.id=r.requester_id WHERE (%s='all' OR r.status=%s) AND (%s='' OR r.kind=%s) ORDER BY r.created_at DESC LIMIT 200",(status,status,kind,kind)).fetchall()
    return {'success':True,'requests':rows,'total':len(rows)}
@app.post('/admin/requests/{request_id}/decision')
def decide_request(request_id:int,data:DecisionInput,x_nexus_admin_key:str|None=Header(default=None)):
    actor=admin_auth(x_nexus_admin_key); reason=moderate(data.reason,'Decision reason')
    with db() as c:
        req=c.execute('SELECT * FROM moderation_requests WHERE id=%s FOR UPDATE',(request_id,)).fetchone()
        if not req: raise HTTPException(404,'Request not found')
        if req['status']!='pending': raise HTTPException(409,'Already resolved')
        if data.approve:
            if req['kind']=='project_verify':
                p=c.execute('SELECT content_hash,name FROM projects WHERE code=%s FOR UPDATE',(req['project_code'],)).fetchone()
                if not p or p['content_hash']!=req['snapshot_hash'] or p['name']!=req['target_username']: raise HTTPException(409,'Project changed since verification request')
                c.execute('UPDATE projects SET verified_hash=%s,verified_name=%s WHERE code=%s',(req['snapshot_hash'],p['name'],req['project_code']))
            elif req['kind'] in {'account_verify','appeal'}:
                if not req['requester_id']: raise HTTPException(409,'Account no longer exists')
                c.execute('UPDATE users SET '+('verified=TRUE' if req['kind']=='account_verify' else 'banned=FALSE')+' WHERE id=%s',(req['requester_id'],))
            elif req['kind']!='report': raise HTTPException(400,'Unsupported request type')
        status='approved' if data.approve else 'rejected'; c.execute('UPDATE moderation_requests SET status=%s,resolved_at=NOW() WHERE id=%s',(status,request_id)); audit(c,status,request_id,reason,actor)
    return {'success':True,'status':status}
@app.post('/admin/account-action')
def admin_account_action(data:AdminAccountAction,x_nexus_admin_key:str|None=Header(default=None)):
    actor=admin_auth(x_nexus_admin_key); reason=moderate(data.reason,'Reason')
    with db() as c:
        user=c.execute('SELECT id FROM users WHERE normalized_name=%s FOR UPDATE',(norm(data.username),)).fetchone()
        if not user: raise HTTPException(404,'Account not found')
        action=data.action
        if action=='warn': c.execute('INSERT INTO warnings(user_id,message) VALUES(%s,%s)',(user['id'],reason))
        else:
            col,value={'ban':('banned',True),'unban':('banned',False),'verify':('verified',True),'unverify':('verified',False)}[action]; c.execute(f'UPDATE users SET {col}=%s WHERE id=%s',(value,user['id']))
            if action=='ban': c.execute('DELETE FROM sessions WHERE user_id=%s',(user['id'],))
        audit(c,action,data.username,reason,actor)
    return {'success':True,'action':action,'username':data.username}
@app.post('/admin/reviews/{review_id}/flag')
def admin_flag_review(review_id:int,data:DecisionInput,x_nexus_admin_key:str|None=Header(default=None)):
    actor=admin_auth(x_nexus_admin_key); reason=moderate(data.reason,'Reason')
    with db() as c:
        row=c.execute('UPDATE reviews SET flagged=%s WHERE id=%s RETURNING id',(data.approve,review_id)).fetchone()
        if not row: raise HTTPException(404,'Review not found')
        audit(c,'flag_review' if data.approve else 'restore_review',review_id,reason,actor)
    return {'success':True,'flagged':data.approve}
@app.get('/admin/audit')
def admin_audit(limit:int=Query(default=50,ge=1,le=200),x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key)
    with db() as c: rows=c.execute('SELECT * FROM moderation_audit ORDER BY created_at DESC,id DESC LIMIT %s',(limit,)).fetchall()
    return {'success':True,'audit':rows}
@app.post('/admin/webhooks/test')
def admin_webhook_test(kind:str=Query(...),x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key)
    if kind not in WEBHOOKS: raise HTTPException(400,'Invalid webhook kind')
    if not WEBHOOKS[kind]: raise HTTPException(400,f'Webhook {kind} is not configured')
    if not webhook(kind,f'NEXUS CLOUD V{APP_VERSION} | Webhook test | {kind}'):
        raise HTTPException(503,'Could not queue notification')
    return {'success':True,'kind':kind,'queued':True,'delivered':False,'message':'Notification queued for Discord delivery'}
@app.get('/admin/projects/{code}')
def admin_project_details(code:str,x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key); code=normalize_project_code(code)
    with db() as c: p=c.execute('SELECT p.*,u.username,u.verified AS creator_verified FROM projects p JOIN users u ON u.id=p.owner_id WHERE p.code=%s',(code,)).fetchone()
    if not p: raise HTTPException(404,'Project not found')
    result=project_public_row(p); result['project']=result.pop('content'); source=result['project'].get('scriptCode',''); patterns=((r'\bloadstring\s*\(','Dynamic code loading detected'),(r'\bgetfenv\s*\(','Environment access detected'),(r'\bsetfenv\s*\(','Environment modification detected'),(r'\bHttpGet\s*\(','Remote source loading detected'))
    result['inspection']={'syntax_verified':False,'runtime_verified':False,'inspection':'heuristic_only','warnings':[msg for pat,msg in patterns if re.search(pat,source,re.IGNORECASE)]}
    return {'success':True,'project':result}
@app.post('/admin/projects/{code}/delete')
def admin_delete_project(code:str,data:AdminDeleteInput,x_nexus_admin_key:str|None=Header(default=None)):
    actor=admin_auth(x_nexus_admin_key); code=normalize_project_code(code)
    if data.confirm_code.strip().upper()!=code: raise HTTPException(400,'Project confirmation code does not match')
    reason=moderate(data.reason,'Deletion reason')
    with db() as c:
        p=c.execute('SELECT code FROM projects WHERE code=%s FOR UPDATE',(code,)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
        c.execute('DELETE FROM projects WHERE code=%s',(code,)); audit(c,'delete_project',code,reason,actor)
    return {'success':True,'deleted':code}
@app.get('/admin/reports')
def admin_reports(status:str=Query(default='pending'),x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key)
    if status not in {'pending','approved','rejected','all'}: raise HTTPException(400,'Invalid status')
    with db() as c: rows=c.execute("SELECT r.*,u.username AS reporter FROM moderation_requests r LEFT JOIN users u ON u.id=r.requester_id WHERE r.kind='report' AND (%s='all' OR r.status=%s) ORDER BY r.created_at DESC LIMIT 100",(status,status)).fetchall()
    return {'success':True,'reports':rows,'total':len(rows)}
@app.get('/admin/webhooks/status')
def admin_webhook_status(x_nexus_admin_key:str|None=Header(default=None)):
    admin_auth(x_nexus_admin_key)
    return {'success':True,'webhooks':{name:{'configured':bool(url),'url_valid_format':url.startswith(('https://discord.com/api/webhooks/','https://discordapp.com/api/webhooks/')) if url else False} for name,url in WEBHOOKS.items()},'note':'Configured does not guarantee successful delivery. Check Render logs.'}
# WEB: login, editor, owner-only browser editing
WEB_EDITOR_FILE=Path(__file__).resolve().parent/'editor.html'; WEB_COOKIE_NAME='nexus_web_session'; WEB_COOKIE_MAX_AGE=60*60*24*30
def web_safe_destination(value):
    value=str(value or '').strip()
    if not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(ch)<32 for ch in value): return '/editor'
    return value
def web_session_user(request,allow_banned=False):
    token=request.cookies.get(WEB_COOKIE_NAME,'')
    if not token: return None
    try: return authenticate('Bearer '+token,allow_banned)
    except HTTPException: return None
def web_require_user(request):
    user=web_session_user(request)
    if not user: raise HTTPException(401,'Web authentication required')
    return user
def web_verify_origin(request):
    origin=request.headers.get('origin'); expected=str(request.base_url).rstrip('/')
    if origin and origin.rstrip('/')!=expected: raise HTTPException(403,'Invalid origin')
    if request.headers.get('sec-fetch-site','').lower()=='cross-site': raise HTTPException(403,'Cross-site request denied')
def web_redirect_login(destination): return RedirectResponse('/login?next='+quote(web_safe_destination(destination),safe=''),status_code=303)
def web_cookie_response(result):
    response=JSONResponse({'success':True,'user':result['user']}); response.set_cookie(WEB_COOKIE_NAME,result['token'],max_age=WEB_COOKIE_MAX_AGE,httponly=True,secure=True,samesite='lax',path='/'); response.headers['Cache-Control']='no-store'; return response
LOGIN_HTML='''<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nexus Cloud | Login</title><style>:root{color-scheme:dark;--bg:#09080d;--panel:#17121d;--border:#3d2c49;--purple:#933ee8;--red:#e63c60;--text:#f7f2fc;--muted:#a99bb5}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:18px;background:radial-gradient(circle at 50% -10%,#47205c,transparent 55%),var(--bg);font-family:Arial,sans-serif;color:var(--text)}.panel{width:100%;max-width:420px;background:var(--panel);border:1px solid var(--border);border-radius:18px;padding:28px;box-shadow:0 25px 80px #0008}.logo{width:58px;height:58px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,var(--red),var(--purple));border-radius:16px;font-size:29px;font-weight:900;margin-bottom:20px}h1{margin:0 0 8px;font-size:25px}.subtitle{font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:24px}label{display:block;font-size:12px;font-weight:bold;color:#d7c9e5;margin:16px 0 8px}input{width:100%;padding:13px;border:1px solid var(--border);border-radius:9px;background:#100d15;color:white;outline:none;font-size:14px}input:focus{border-color:var(--purple)}button{display:block;width:100%;padding:13px;margin-top:18px;border:0;border-radius:9px;background:var(--purple);color:white;font-weight:bold;cursor:pointer}button.secondary{background:#342640;margin-top:9px}button:disabled{opacity:.5;cursor:wait}#error{color:#ff8199;font-size:12px;min-height:18px;margin-top:15px;overflow-wrap:anywhere}.footer{margin-top:20px;text-align:center;color:var(--muted);font-size:11px}</style></head><body><main class="panel"><div class="logo">N</div><h1>Nexus Cloud</h1><div class="subtitle">Entre na sua conta para acessar seus projetos e continuar editando pelo navegador.</div><form id="form"><label for="username">USUÁRIO</label><input id="username" autocomplete="username" minlength="3" maxlength="20" required placeholder="Seu nome Nexus"><label for="password">SENHA</label><input id="password" type="password" autocomplete="current-password" minlength="8" required placeholder="Sua senha"><button id="loginButton" type="submit">ENTRAR</button><button id="registerButton" class="secondary" type="button">CRIAR CONTA</button><div id="error" role="status"></div></form><div class="footer">NEXUS CLOUD • SECURE WEB ACCESS</div></main><script>"use strict";const destination=__DESTINATION__;const form=document.getElementById('form'),error=document.getElementById('error'),loginButton=document.getElementById('loginButton'),registerButton=document.getElementById('registerButton');async function authenticateWeb(mode){const username=document.getElementById('username').value.trim(),password=document.getElementById('password').value;error.textContent='';loginButton.disabled=true;registerButton.disabled=true;try{const response=await fetch(mode==='register'?'/web/register':'/web/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Não foi possível autenticar.');window.location.replace(destination)}catch(e){error.textContent=e.message}finally{loginButton.disabled=false;registerButton.disabled=false}}form.addEventListener('submit',e=>{e.preventDefault();authenticateWeb('login')});registerButton.addEventListener('click',()=>authenticateWeb('register'));</script></body></html>'''
@app.get('/login',response_class=HTMLResponse)
def web_login_page(request:Request,next:str='/editor'):
    destination=web_safe_destination(next)
    if web_session_user(request): return RedirectResponse(destination,status_code=303)
    html=LOGIN_HTML.replace('__DESTINATION__',json.dumps(destination).replace('<','\\u003c'))
    return HTMLResponse(html,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
@app.post('/web/login')
def web_login(request:Request,data:Credentials): web_verify_origin(request); return web_cookie_response(login(data))
@app.post('/web/register')
def web_register(request:Request,data:Credentials): web_verify_origin(request); return web_cookie_response(register(data))
@app.get('/web/me')
def web_me(request:Request): return {'authenticated':True,'user':web_require_user(request)}
@app.post('/web/logout')
def web_logout(request:Request):
    web_verify_origin(request); token=request.cookies.get(WEB_COOKIE_NAME,'')
    if token:
        with db() as c: c.execute('DELETE FROM sessions WHERE token_hash=%s',(hash_token(token),))
    response=JSONResponse({'success':True}); response.delete_cookie(WEB_COOKIE_NAME,path='/'); response.headers['Cache-Control']='no-store'; return response
def get_owned_web_project(code,user):
    code=normalize_project_code(code)
    with db() as c: p=c.execute('SELECT code,owner_id,name,version FROM projects WHERE code=%s',(code,)).fetchone()
    if not p: raise HTTPException(404,'Project not found')
    if p['owner_id']!=user['id']: raise HTTPException(403,'You are not the owner of this project')
    return p
@app.get('/web/projects/{code}')
def web_project_data(code:str,request:Request):
    user=web_require_user(request); code=normalize_project_code(code); get_owned_web_project(code,user)
    response=JSONResponse(content=json.loads(json.dumps(project_details(code,user),default=str,ensure_ascii=False))); response.headers['Cache-Control']='no-store'; return response
def serve_web_editor(code=None):
    if not WEB_EDITOR_FILE.is_file(): return HTMLResponse('<!DOCTYPE html><html lang="pt-BR"><meta charset="UTF-8"><title>Nexus Cloud</title><body style="background:#100d18;color:white;font-family:Arial;padding:30px"><h2>Editor não encontrado</h2><p>Adicione editor.html na mesma pasta do main.py no GitHub.</p></body></html>',status_code=503)
    html=WEB_EDITOR_FILE.read_text(encoding='utf-8'); bootstrap='<script>window.NEXUS_WEB_MODE=true;window.NEXUS_PROJECT_CODE='+json.dumps(code or '')+';</script>'; html=html.replace('</head>',bootstrap+'\n</head>',1) if '</head>' in html else bootstrap+html
    return HTMLResponse(html,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin'})
@app.get('/editor')
def web_editor_home(request:Request):
    if not web_session_user(request): return web_redirect_login('/editor')
    return serve_web_editor()
@app.middleware('http')
async def nexus_browser_project_router(request:Request,call_next):
    path=request.url.path; accept=request.headers.get('accept','').lower(); fetch_mode=request.headers.get('sec-fetch-mode','').lower()
    if request.method=='GET' and re.fullmatch(r'/projects/NXS-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}/?',path,re.IGNORECASE) and ('text/html' in accept or fetch_mode=='navigate'):
        code=path.strip('/').split('/')[-1].upper(); user=web_session_user(request)
        if not user: return web_redirect_login('/projects/'+code)
        try: get_owned_web_project(code,user)
        except HTTPException as exc: return HTMLResponse('<!DOCTYPE html><html lang="pt-BR"><meta charset="UTF-8"><title>Nexus Cloud | Acesso negado</title><body style="background:#100d18;color:white;font-family:Arial;padding:35px"><h2>Acesso ao projeto indisponível</h2><p>Você não tem permissão para editar este projeto ou ele não existe.</p><a href="/editor" style="color:#a04cf0">Voltar ao Nexus Cloud</a></body></html>',status_code=exc.status_code,headers={'Cache-Control':'no-store'})
        return serve_web_editor(code)
    return await call_next(request)
# Execution V4.6.1: temporary, single-use, purpose-bound tickets.
EXECUTION_TICKET_SECONDS=15; EXECUTION_TICKET_PATTERN=re.compile(r'^[A-Za-z0-9_-]{32,128}$')
class ExecutionRequest(BaseModel): code:str=Field(min_length=13,max_length=13)
def create_execution_ticket(c,code,user_id,purpose):
    ticket=secrets.token_urlsafe(48)
    c.execute("INSERT INTO execution_tickets(ticket_hash,project_code,requester_id,purpose,expires_at) VALUES(%s,%s,%s,%s,NOW()+(%s*INTERVAL '1 second'))",(hash_token(ticket),code,user_id,purpose,EXECUTION_TICKET_SECONDS))
    return ticket
def consume_execution_ticket(c,ticket,purpose):
    if not EXECUTION_TICKET_PATTERN.fullmatch(ticket): raise HTTPException(404,'Execution ticket not found')
    row=c.execute('DELETE FROM execution_tickets WHERE ticket_hash=%s AND purpose=%s AND expires_at>NOW() RETURNING project_code,requester_id',(hash_token(ticket),purpose)).fetchone()
    if not row: raise HTTPException(410,'Execution ticket expired or already consumed')
    return row
def execution_headers(): return {'Cache-Control':'private, no-store, no-cache, must-revalidate, max-age=0','Pragma':'no-cache','Expires':'0','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex, nofollow, noarchive'}
def execution_page_html(name,code,version,source,execution_id):
    html='''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>Nexus Cloud | Execution</title><style>*{box-sizing:border-box}body{margin:0;background:#050507;color:#e9e6f1;font:14px system-ui,Arial,sans-serif}header{padding:18px 24px;border-bottom:1px solid #302337;background:#100d14}h1{font-size:17px;margin:0 0 7px}small{color:#a998b7;overflow-wrap:anywhere}.id{display:block;margin-top:8px;color:#c59aff;font-size:12px;overflow-wrap:anywhere}pre{margin:0;padding:24px;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 Consolas,monospace;color:#e3d4ff}</style></head><body><header><h1>__NAME__</h1><small>__CODE__ • VERSION __VERSION__</small><span class="id">Execution ID: __EXECUTION_ID__</span></header><pre><code>__SOURCE__</code></pre></body></html>'''
    return html.replace('__NAME__',escape(str(name))).replace('__CODE__',escape(str(code))).replace('__VERSION__',escape(str(version))).replace('__EXECUTION_ID__',escape(str(execution_id))).replace('__SOURCE__',escape(str(source)))
@app.post('/execution/request')
def request_execution(data:ExecutionRequest,authorization:str|None=Header(default=None)):
    user=authenticate(authorization); code=normalize_project_code(data.code)
    with db() as c:
        c.execute('DELETE FROM execution_tickets WHERE expires_at<=NOW()')
        if not c.execute('SELECT code FROM projects WHERE code=%s',(code,)).fetchone(): raise HTTPException(404,'Project not found')
        throttle(c,user['id'],'execution_request',2)
        execution_ticket=create_execution_ticket(c,code,user['id'],'redeem'); page_ticket=create_execution_ticket(c,code,user['id'],'page')
    return JSONResponse({'success':True,'code':code,'ticket':execution_ticket,'page_ticket':page_ticket,'expires_in':EXECUTION_TICKET_SECONDS,'redeem_url':'/execution/redeem/'+execution_ticket,'page_url':'/execute/ticket/'+page_ticket},headers=execution_headers())
@app.get('/execution/redeem/{ticket}')
def redeem_execution(ticket:str):
    with db() as c:
        row=consume_execution_ticket(c,ticket,'redeem'); p=c.execute('UPDATE projects SET uses=uses+1 WHERE code=%s RETURNING code,name,content,version,uses',(row['project_code'],)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
    return JSONResponse({'success':True,'code':p['code'],'projectCode':p['code'],'name':p['name'],'version':p['version'],'project':p['content'],'scriptCode':p['content'].get('scriptCode',''),'uses':p['uses']},headers=execution_headers())
@app.get('/execute/ticket/{ticket}',response_class=HTMLResponse)
def public_execution_ticket_page(ticket:str):
    with db() as c:
        row=consume_execution_ticket(c,ticket,'page'); p=c.execute('SELECT code,name,content,version FROM projects WHERE code=%s',(row['project_code'],)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
    headers=execution_headers(); headers['Content-Security-Policy']="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    return HTMLResponse(execution_page_html(p['name'],p['code'],p['version'],p['content'].get('scriptCode',''),ticket),headers=headers)
@app.get('/execute/{code}')
def blocked_direct_execution(code:str): raise HTTPException(404,'Execution not found. Generate a temporary ticket through the Nexus Cloud panel.')
