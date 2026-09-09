from flask import Flask, request, redirect, jsonify, render_template, session, url_for, send_from_directory
from functools import wraps
from pathlib import Path
import os, json, secrets, hashlib, hmac, time, requests, logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger=logging.getLogger('anion-web')
BASE=Path(__file__).resolve().parent
app=Flask(__name__, template_folder=str(BASE/'templates'), static_folder=str(BASE/'static'), static_url_path='/static')
app.secret_key=os.getenv('WEB_SESSION_SECRET') or os.getenv('CLIENT_SECRET') or hashlib.sha256(os.getenv('FIREBASE_URL','anion').encode()).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=True,SESSION_COOKIE_SAMESITE='Lax',PERMANENT_SESSION_LIFETIME=86400)

try:
    import firebase_admin
    from firebase_admin import credentials, db
    if not firebase_admin._apps:
        raw=os.getenv('FIREBASE_KEY_JSON')
        if raw:
            firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)),{'databaseURL':os.getenv('FIREBASE_URL','https://edith-ultimate-mit-project-default-rtdb.firebaseio.com')})
    firebase_ref=db.reference() if firebase_admin._apps else None
except Exception as exc:
    logger.error('Firebase unavailable: %s',exc); firebase_ref=None

RAILWAY_API=os.getenv('RAILWAY_API_URL','').rstrip('/')
CONTROL_API_KEY=os.getenv('CONTROL_API_KEY','')
SUPER_USER=os.getenv('SUPERADMIN_USERNAME','')
SUPER_PASS=os.getenv('SUPERADMIN_PASSWORD','')
SUPER_ID=str(os.getenv('SUPER_ADMIN_ID',''))

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(str(BASE/'static'), filename)

def fb_get(path):
    return firebase_ref.child(path).get() if firebase_ref else None
def fb_set(path,value):
    if firebase_ref: firebase_ref.child(path).set(value)
def fb_delete(path):
    if firebase_ref: firebase_ref.child(path).delete()

def railway(path, method='GET', actor=None, payload=None, timeout=12):
    if not RAILWAY_API or not CONTROL_API_KEY: return None, {'error':'backend_not_configured'}
    headers={'X-API-Key':CONTROL_API_KEY}
    if actor is not None: headers['X-Actor-ID']=str(actor)
    try:
        r=requests.request(method,RAILWAY_API+path,headers=headers,json=payload,timeout=timeout)
        try: body=r.json()
        except Exception: body={'error':r.text[:300]}
        return r.status_code,body
    except requests.RequestException as exc:
        logger.error('Railway request failed: %s',exc); return None,{'error':'backend_unreachable'}

def logged_in(fn):
    @wraps(fn)
    def wrapper(*a,**kw):
        if not session.get('user_id'): return redirect(url_for('login',next=request.path))
        return fn(*a,**kw)
    return wrapper

def role_required(role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a,**kw):
            if not session.get('user_id'): return redirect(url_for('login',next=request.path))
            current=session.get('role','user')
            if role=='superadmin' and current!='superadmin': return redirect(url_for('login'))
            if role=='moderator' and current not in {'moderator','superadmin'}: return redirect(url_for('login'))
            return fn(*a,**kw)
        return wrapper
    return deco

def find_credentials(username):
    idx=fb_get(f'credentials_by_username/{username}')
    if idx and idx.get('user_id'):
        return fb_get(f"credentials/{idx['user_id']}")
    # Legacy fallback: repair missing reverse index.
    allc=fb_get('credentials') or {}
    for uid,cred in allc.items():
        if isinstance(cred,dict) and hmac.compare_digest(str(cred.get('username','')),username):
            fb_set(f'credentials_by_username/{username}',{'user_id':str(uid),'repaired_at':datetime.utcnow().isoformat()+'Z'})
            return cred
    return None

def verify_password(stored, supplied):
    if stored is None: return False
    # Compatibility with existing plaintext credentials; new credentials can use password_hash later.
    if stored.startswith('sha256$'):
        return hmac.compare_digest(stored.split('$',1)[1],hashlib.sha256(supplied.encode()).hexdigest())
    return hmac.compare_digest(stored,supplied)

def establish(user_id,username,role):
    session.clear(); session.permanent=True
    session.update(user_id=str(user_id),username=username,role=role,login_at=int(time.time()))

def current_user():
    uid=session.get('user_id'); return fb_get(f'profiles/{uid}') if uid else None

@app.route('/')
def home(): return render_template('index.html', config_client_id=os.getenv('CLIENT_ID',''))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        username=request.form.get('username','').strip()
        password=request.form.get('password','')
        if not username or not password: return render_template('login.html',error='Enter both username and password.')
        if SUPER_USER and hmac.compare_digest(username,SUPER_USER) and SUPER_PASS and hmac.compare_digest(password,SUPER_PASS):
            establish(SUPER_ID or 'superadmin',username,'superadmin'); return redirect(request.args.get('next') or '/superadmin.html')
        cred=find_credentials(username)
        if not cred or not verify_password(str(cred.get('password','')),password): return render_template('login.html',error='Invalid credentials.')
        uid=str(cred.get('user_id',''))
        profile=fb_get(f'profiles/{uid}') or {}
        status,guild_body=railway(f'/api/v1/user/{uid}/guilds',actor=uid)
        dynamic_mod=any(g.get('is_admin') or g.get('is_owner') for g in (guild_body.get('guilds',[]) if isinstance(guild_body,dict) else []))
        role='moderator' if cred.get('role')=='moderator' or dynamic_mod else 'user'
        establish(uid,username,role)
        session['profile']=profile
        return redirect(request.args.get('next') or ('/moderator.html' if role=='moderator' else '/user.html'))
    return render_template('login.html',error=None)

@app.route('/logout',methods=['GET','POST'])
def logout(): session.clear(); return redirect('/')

@app.route('/user.html')
@logged_in
def user_page(): return render_template('user.html', config_client_id=os.getenv('CLIENT_ID',''))
@app.route('/moderator.html')
@role_required('moderator')
def moderator_page(): return render_template('moderator.html')
@app.route('/superadmin.html')
@role_required('superadmin')
def superadmin_page(): return render_template('superadmin.html')

@app.route('/api/me')
@logged_in
def api_me():
    return jsonify({'ok':True,'user':{'id':session['user_id'],'username':session.get('username'),'role':session.get('role'),'profile':current_user() or {}}})

@app.route('/api/guilds')
@logged_in
def api_guilds():
    status,body=railway(f"/api/v1/user/{session['user_id']}/guilds",actor=session['user_id'])
    mutual=body.get('guilds',[]) if isinstance(body,dict) else []
    known=fb_get(f"user_discord_guilds/{session['user_id']}") or {}
    mutual_ids={g['id'] for g in mutual}
    allg=[]
    for gid,g in known.items() if isinstance(known,dict) else []:
        if str(gid) not in mutual_ids: allg.append({'id':str(gid),'name':g.get('name','Unknown'),'icon':g.get('icon'),'member_count':None,'bot_present':False,'is_admin':bool(g.get('owner') or (int(g.get('permissions','0')) & 8)),'is_owner':bool(g.get('owner'))})
    for g in mutual: g['bot_present']=True; allg.append(g)
    return jsonify({'ok':True,'guilds':allg,'mutual_count':len(mutual)})

@app.route('/api/superadmin/guilds')
@role_required('superadmin')
def api_super_guilds():
    status,body=railway('/api/v1/superadmin/guilds',actor=session['user_id'])
    return jsonify(body), status or 502

@app.route('/api/guild/<guild_id>')
@logged_in
def api_guild(guild_id):
    status,body=railway(f'/api/v1/guild/{guild_id}?actor_id={session["user_id"]}&full={1 if session.get("role") in {"moderator","superadmin"} else 0}',actor=session['user_id'])
    return jsonify(body), status or 502

@app.route('/api/warnings/<guild_id>/<user_id>')
@role_required('moderator')
def api_warnings(guild_id,user_id):
    status,body=railway(f'/api/v1/guild/{guild_id}/warnings/{user_id}',actor=session['user_id'])
    return jsonify(body), status or 502

@app.route('/api/action/<action>',methods=['POST'])
@logged_in
def api_action(action):
    if session.get('role') not in {'user','moderator','superadmin'}: return jsonify({'error':'login_required'}),403
    if action != 'nickname' and session.get('role') not in {'moderator','superadmin'}: return jsonify({'error':'moderator_required'}),403
    status,body=railway(f'/api/v1/action/{action}',method='POST',actor=session['user_id'],payload=request.get_json(silent=True) or {})
    return jsonify(body), status or 502

@app.route('/api/public/stats')
def public_stats():
    status,body=railway('/api/v1/stats')
    return jsonify(body), status or 502

@app.route('/api/health')
def health():
    status,body=railway('/api/health')
    return jsonify({'ok':True,'web':True,'backend':body}), status or 502

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
