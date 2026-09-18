from flask import Flask, request, redirect, jsonify, send_from_directory, render_template, session, make_response
import os
import json
import secrets
import asyncio
import hashlib
import base64
import aiohttp
from datetime import datetime
import logging

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(APP_DIR, 'templates')
STATIC_DIR = os.path.join(APP_DIR, 'static')
app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=None)
app.jinja_env.globals['_public_base_url'] = lambda req=None: _public_base_url(req or request) if '_public_base_url' in globals() else request.url_root.rstrip('/')

def safe_render(template_name, **context):
    try:
        return render_template(template_name, **context)
    except Exception as exc:
        logger.exception('Template render failed: %s', template_name)
        title = str(context.get('title') or 'EditH Web Error')
        message = str(context.get('message') or exc)
        username = str(context.get('username') or '')
        discord_id = str(context.get('discord_id') or '')
        bot_result = context.get('bot_result')
        detail = bot_result.get('error', '') if isinstance(bot_result, dict) else ''
        html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>body{{margin:0;background:#090909;color:#eee;font-family:Arial,sans-serif;display:grid;place-items:center;min-height:100vh}}.card{{width:min(680px,90%);background:#111;border:1px solid #2b2b2b;border-radius:16px;padding:28px;box-sizing:border-box}}h1{{margin-top:0}}.muted{{color:#aaa}}pre{{white-space:pre-wrap;background:#080808;padding:14px;border-radius:10px;overflow:auto}}a{{color:#ff4b4b}}</style></head><body><main class="card"><h1>{title}</h1><p>{message}</p><p class="muted">{('User: '+username+' &nbsp; ID: '+discord_id) if username or discord_id else ''}</p><pre>{detail}</pre><p><a href="/login">Return to login</a></p></main></body></html>"""
        return html

# Vercel may run multiple serverless instances. A new random secret on every
# cold start invalidates Flask login cookies and makes dashboards appear logged out.
# FLASK_SECRET_KEY should be configured in Vercel; CLIENT_SECRET is only a stable
# fallback so an omitted Flask secret does not silently break sessions.
_configured_secret = os.getenv('FLASK_SECRET_KEY')
if not _configured_secret:
    _fallback_material = os.getenv('CLIENT_SECRET') or os.getenv('BOT_API_URL') or 'edith-session-fallback'
    _configured_secret = hashlib.sha256(_fallback_material.encode('utf-8')).hexdigest()
    logger.warning('⚠️ FLASK_SECRET_KEY is not configured; using a stable derived fallback. Set FLASK_SECRET_KEY in Vercel.')
app.secret_key = _configured_secret
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ============ FIREBASE SETUP ============
try:
    import firebase_admin
    from firebase_admin import credentials, db
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    logger.warning("⚠️ Firebase not available")

firebase_app = None
rtdb_client = None

if FIREBASE_AVAILABLE:
    try:
        firebase_json = os.getenv('FIREBASE_KEY_JSON')
        firebase_url = os.getenv('FIREBASE_URL', 'https://edith-ultimate-mit-project-default-rtdb.firebaseio.com')
        
        if firebase_json:
            cred_dict = json.loads(firebase_json)
            cred = credentials.Certificate(cred_dict)
            firebase_app = firebase_admin.initialize_app(cred, {
                'databaseURL': firebase_url
            })
            rtdb_client = db.reference()
            logger.info("✅ Firebase connected!")
    except Exception as e:
        logger.error(f"❌ Firebase error: {e}")

# ============ FIREBASE HELPERS ============
def firebase_required():
    if not rtdb_client:
        logger.error('Firebase is unavailable; refusing to process verification')
        return False
    return True

def firebase_get(path):
    if not rtdb_client: return None
    try: return rtdb_client.child(path).get()
    except Exception as e:
        logger.error(f'Firebase get failed at {path}: {e}'); return None

def firebase_set(path, value):
    if not rtdb_client: return False
    try:
        rtdb_client.child(path).set(value); return True
    except Exception as e:
        logger.error(f'Firebase set failed at {path}: {e}'); return False

def firebase_delete(path):
    if not rtdb_client: return False
    try:
        rtdb_client.child(path).delete(); return True
    except Exception as e:
        logger.error(f'Firebase delete failed at {path}: {e}'); return False

# ============ DISCORD OAUTH STORAGE ============
def _oauth_fernet():
    """Derive a stable encryption key from the Flask secret.
    OAuth tokens are never sent to the browser; only encrypted server-side records are stored in RTDB.
    """
    if Fernet is None:
        return None
    secret = os.getenv('FLASK_SECRET_KEY') or app.secret_key
    digest = hashlib.sha256(str(secret).encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))

def _encrypt_oauth(value):
    if not value:
        return None
    f = _oauth_fernet()
    return f.encrypt(str(value).encode()).decode() if f else None

def _decrypt_oauth(value):
    if not value:
        return None
    f = _oauth_fernet()
    if not f:
        return None
    try:
        return f.decrypt(str(value).encode()).decode()
    except Exception:
        return None

def _store_oauth_data(user_id, token_data, guilds, connections=None, guild_members=None):
    """Persist the current Discord OAuth snapshot for this user.

    Only data actually returned by Discord for the granted scopes is stored.
    The guild snapshot is replaced on every OAuth login so departed servers
    do not remain in the current-server view.
    """
    if not rtdb_client:
        return False
    uid = str(user_id)
    expires_in = int(token_data.get('expires_in') or 0)
    now = int(datetime.utcnow().timestamp())
    session_data = {
        'user_id': uid,
        'access_token': _encrypt_oauth(token_data.get('access_token')),
        'refresh_token': _encrypt_oauth(token_data.get('refresh_token')),
        'token_type': token_data.get('token_type'),
        'scope': token_data.get('scope') or 'identify email guilds connections guilds.members.read',
        'expires_in': expires_in,
        'expires_at': now + expires_in,
        'updated_at': datetime.utcnow().isoformat() + 'Z'
    }
    # Replace the guild snapshot so removed/changed memberships do not linger.
    clean = {}
    for g in guilds if isinstance(guilds, list) else []:
        gid = str(g.get('id', ''))
        if not gid.isdigit():
            continue
        clean[gid] = {
            'id': gid,
            'name': g.get('name') or 'Unknown Server',
            'icon': g.get('icon'),
            'owner': bool(g.get('owner')),
            'permissions': str(g.get('permissions') or '0'),
            'features': g.get('features') or [],
            'updated_at': session_data['updated_at']
        }
    members_clean = {}
    if isinstance(guild_members, dict):
        for gid, member in guild_members.items():
            if not isinstance(member, dict) or not str(gid).isdigit():
                continue
            members_clean[str(gid)] = {
                'user_id': str(member.get('user', {}).get('id') or member.get('user_id') or uid),
                'nick': member.get('nick'),
                'avatar': member.get('avatar'),
                'roles': [str(x) for x in (member.get('roles') or [])],
                'joined_at': member.get('joined_at'),
                'premium_since': member.get('premium_since'),
                'deaf': bool(member.get('deaf', False)),
                'mute': bool(member.get('mute', False)),
                'flags': member.get('flags', 0),
                'pending': member.get('pending', False),
                'updated_at': session_data['updated_at']
            }

    connections_clean = {}
    if isinstance(connections, list):
        for item in connections:
            if not isinstance(item, dict):
                continue
            ctype = str(item.get('type') or '')
            cid = str(item.get('id') or '')
            key = cid or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16]
            connections_clean[key] = {
                'id': cid,
                'type': ctype,
                'name': item.get('name'),
                'verified': bool(item.get('verified', False)),
                'visibility': item.get('visibility'),
                'show_activity': item.get('show_activity'),
                'two_way_link': item.get('two_way_link'),
                'friend_sync': item.get('friend_sync'),
                'metadata_visibility': item.get('metadata_visibility'),
                'updated_at': session_data['updated_at']
            }

    try:
        rtdb_client.child(f'oauth_sessions/{uid}').set(session_data)
        rtdb_client.child(f'oauth_guilds/{uid}').set(clean)
        rtdb_client.child(f'oauth_guild_members/{uid}').set(members_clean)
        rtdb_client.child(f'oauth_connections/{uid}').set(connections_clean)
        return True
    except Exception as e:
        logger.error(f'❌ OAuth data storage failed for {uid}: {e}')
        return False

def _stored_oauth_guilds(user_id):
    data = firebase_get(f'oauth_guilds/{str(user_id)}') or {}
    return list(data.values()) if isinstance(data, dict) else []

# ============ ROUTES ============
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route('/favicon.ico')
def favicon():
    return ('', 204)

@app.route('/')
def home():
    return safe_render('index.html', config_client_id=os.getenv('CLIENT_ID',''), page_url=_public_base_url(request))


def _public_base_url(req=None):
    # Derive the canonical public origin from the incoming request so no
    # deployment-specific Vercel hostname is hard-coded into source control.
    r = req or request
    forwarded = r.headers.get('X-Forwarded-Proto') or r.scheme
    host = r.headers.get('X-Forwarded-Host') or r.host
    return f"{forwarded}://{host}".rstrip('/')


def _page_context(req, title, description, path, kind='WebPage', extra_schema=None):
    base = _public_base_url(req)
    canonical = base + (path if path.startswith('/') else '/' + path)
    graph = [
        {'@type': 'WebSite', '@id': base + '/#website', 'url': base + '/', 'name': 'EditH',
         'description': 'All-in-one Discord server management platform created and developed by Udit Singh Dhakrey.'},
        {'@type': 'Person', '@id': base + '/developer#person', 'name': 'Udit Singh Dhakrey',
         'url': base + '/developer', 'jobTitle': 'Creator & Lead Developer of EditH',
         'sameAs': []},
        {'@type': 'SoftwareApplication', '@id': base + '/#edith', 'name': 'EditH',
         'url': base + '/', 'applicationCategory': 'SocialNetworkingApplication',
         'description': 'All-in-one Discord server management platform.',
         'creator': {'@id': base + '/developer#person'},
         'developer': {'@id': base + '/developer#person'}}
    ]
    page = {'@type': kind, '@id': canonical + '#webpage', 'url': canonical, 'name': title,
            'description': description, 'isPartOf': {'@id': base + '/#website'}}
    graph.append(page)
    if extra_schema:
        graph.extend(extra_schema if isinstance(extra_schema, list) else [extra_schema])
    return {'title': title, 'description': description, 'canonical': canonical,
            'schema': {'@context':'https://schema.org','@graph':graph}}


@app.route('/about')
def about_page():
    return safe_render('about.html', **_page_context(request, 'About EditH — Discord Server Management',
        'Learn what EditH is, why it exists, and how its integrated Discord management approach works.', '/about'))

@app.route('/developer')
def developer_page():
    return safe_render('developer.html', **_page_context(request, 'Udit Singh Dhakrey — Creator of EditH',
        'Udit Singh Dhakrey is the creator and lead developer of EditH, an all-in-one Discord server management platform.', '/developer', 'ProfilePage'))

@app.route('/features')
def features_page():
    return safe_render('features.html', **_page_context(request, 'EditH Features — Discord Server Management',
        'Explore the moderation, verification, automation, tickets, giveaways, analytics, dashboard and server-management capabilities implemented in EditH.', '/features'))

@app.route('/docs')
def docs_page():
    return safe_render('docs.html', **_page_context(request, 'EditH Documentation — Architecture & Systems',
        'Technical documentation for EditH, including its Vercel frontend, Railway control API, Discord integration, authentication and Firebase-backed services.', '/docs'))

@app.route('/stats')
def stats_page():
    return safe_render('stats.html', **_page_context(request, 'EditH Stats — Live Project Metrics',
        'Public EditH project metrics served from the deployed bot and Firebase-backed counters.', '/stats'))

@app.route('/changelog')
def changelog_page():
    return safe_render('changelog.html', **_page_context(request, 'EditH Changelog',
        'A factual development history for EditH. Historical entries are added only when they are documented by the project.', '/changelog'))

@app.route('/privacy')
def privacy_page():
    return safe_render('privacy.html', **_page_context(request, 'EditH Privacy',
        'Information about the public website, dashboard authentication and Discord-related data used by EditH.', '/privacy'))

@app.route('/terms')
def terms_page():
    return safe_render('terms.html', **_page_context(request, 'EditH Terms',
        'Terms for using the EditH public website and Discord server management platform.', '/terms'))

@app.route('/robots.txt')
def robots_txt():
    base = _public_base_url(request)
    response = make_response(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /user\nDisallow: /moderator\nDisallow: /superadmin\nDisallow: /login\nDisallow: /callback\nSitemap: {base}/sitemap.xml\n")
    response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    return response

@app.route('/sitemap.xml')
def sitemap_xml():
    base = _public_base_url(request)
    paths = ['/', '/about', '/developer', '/features', '/docs', '/stats', '/changelog', '/privacy', '/terms']
    urls = ''.join(f'<url><loc>{base}{path}</loc></url>' for path in paths)
    response = make_response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>')
    response.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return response


@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        # Superadmin is intentionally configured only through environment variables.
        if username == os.getenv('SUPERADMIN_USERNAME','') and password == os.getenv('SUPERADMIN_PASSWORD','') and username:
            session['user_id'] = str(os.getenv('SUPER_ADMIN_ID','superadmin'))
            session['role'] = 'superadmin'
            return redirect('/superadmin')
        creds = firebase_get('credentials_by_username/' + username)
        uid = creds.get('user_id') if isinstance(creds, dict) else None
        # Recovery path for older credential records whose reverse username index
        # was never written. This also makes migrations between bot versions safer.
        if not uid and rtdb_client and username:
            all_creds = firebase_get('credentials') or {}
            if isinstance(all_creds, dict):
                for candidate_uid, record in all_creds.items():
                    if isinstance(record, dict) and str(record.get('cr_user') or record.get('username') or '') == username:
                        uid = str(candidate_uid)
                        firebase_set('credentials_by_username/' + username, {'user_id': uid, 'created_at': record.get('created_at')})
                        break
        if uid:
            record = firebase_get('credentials/' + str(uid))
            stored_password = str(record.get('cr_password') or record.get('password') or '') if isinstance(record, dict) else ''
            if stored_password and secrets.compare_digest(stored_password, password):
                session.clear()
                session['user_id'] = str(uid)
                session['role'] = str(record.get('role','member'))
                return redirect('/moderator' if session['role'] == 'moderator' else '/user')
        error = 'Invalid username or password.'
    return safe_render('login.html', title='EditH Login', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

def _login_required():
    return bool(session.get('user_id'))

def _bot_request(path, method='GET', payload=None):
    base=(os.getenv('BOT_API_URL') or '').strip().rstrip('/')
    if base and not base.startswith(('http://','https://')):
        base='https://'+base
    key=os.getenv('CONTROL_API_KEY') or ''
    if not base or not key: return None, 503
    async def run():
        headers={'X-API-Key':key,'X-Actor-ID':str(session.get('user_id','')),'Content-Type':'application/json'}
        async with aiohttp.ClientSession() as hs:
            url=base+path
            if method=='POST':
                async with hs.post(url,json=payload or {},headers=headers,timeout=45) as r: return r.status, await r.json(content_type=None)
            async with hs.get(url,headers=headers,timeout=20) as r: return r.status, await r.json(content_type=None)
    loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(run())
    finally: loop.close()

@app.route('/user')
def user_page():
    if not _login_required(): return redirect('/login')
    return safe_render('user.html')

@app.route('/moderator')
def moderator_page():
    if not _login_required(): return redirect('/login')
    return safe_render('moderator.html')

@app.route('/superadmin')
def superadmin_page():
    if not _login_required() or session.get('role') != 'superadmin': return redirect('/login')
    return safe_render('superadmin.html', config_client_id=os.getenv('CLIENT_ID',''), bot_invite_permissions=os.getenv('BOT_INVITE_PERMISSIONS','0'))

@app.route('/api/me')
def api_me():
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    uid=str(session['user_id'])
    creds=firebase_get('credentials/'+uid)
    profile=firebase_get('profiles/'+uid)
    # Credential records are the source of truth for dashboard access. Refresh
    # the session role so a Discord permission change is reflected without logout.
    role = str((creds or {}).get('role') or session.get('role') or 'member')
    if role not in ('member','moderator','superadmin'):
        role = 'member'
    session['role'] = role
    return jsonify({'ok':True,'user':{
        'id':uid,
        'username':profile.get('username') if isinstance(profile,dict) else None,
        'profile':profile or {},
        'credentials':creds or {},
        'role':role
    }})

@app.route('/api/guilds')
def api_guilds():
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    uid=str(session['user_id'])
    oauth_guilds={str(g.get('id')):dict(g) for g in _stored_oauth_guilds(uid) if g.get('id')}
    # Credential-based users may not have completed Discord OAuth recently.
    # Firebase's reverse membership index keeps their server list available.
    stored_memberships = firebase_get(f'user_guilds/{uid}') or {}
    if isinstance(stored_memberships, dict):
        for gid, membership in stored_memberships.items():
            if isinstance(membership, dict):
                merged = oauth_guilds.get(str(gid), {})
                merged.update({
                    'id': str(gid),
                    'name': membership.get('guild_name') or merged.get('name') or 'Unknown Server',
                    'icon': membership.get('guild_icon') or merged.get('icon'),
                    'is_owner': bool(membership.get('is_owner')),
                    'is_admin': bool(membership.get('is_admin')),
                    'permissions': str(membership.get('permissions') or merged.get('permissions') or '0')
                })
                oauth_guilds[str(gid)] = merged
    status,data=_bot_request('/api/v1/bot/guilds')
    if status is None or status >= 400 or not isinstance(data,dict):
        # The dashboard should still show the user's Discord OAuth guilds when
        # the live Railway API is temporarily unavailable. Live-only actions
        # will continue to report their actual Railway error.
        return jsonify({
            'ok':True,
            'degraded':True,
            'error':(data or {}).get('error','bot_api_unavailable') if isinstance(data,dict) else 'bot_api_unavailable',
            'guilds':list(oauth_guilds.values()),
            'source':'discord_oauth'
        })
    for g in data.get('guilds',[]):
        gid=str(g.get('id'))
        merged=oauth_guilds.get(gid,{})
        merged.update(g)
        merged['bot_present']=True
        oauth_guilds[gid]=merged
    guilds=list(oauth_guilds.values())
    # OAuth is the source for the user's complete guild scope; Railway adds the live bot-side state.
    guilds.sort(key=lambda x: (not bool(x.get('bot_present')), str(x.get('name','')).lower()))
    return jsonify({'ok':True,'guilds':guilds,'source':'discord_oauth+railway'})

@app.route('/api/superadmin/guilds')
def api_superadmin_guilds():
    if session.get('role')!='superadmin': return jsonify({'error':'forbidden'}),403
    status,data=_bot_request('/api/v1/superadmin/guilds')
    if status is None or status >= 400 or not isinstance(data,dict):
        code = status if isinstance(status, int) else 503
        return jsonify({'ok':False,'error':(data or {}).get('error','bot_api_unavailable'),'guilds':[]}), code
    return jsonify(data),status

@app.route('/api/guild/<guild_id>')
def api_guild_proxy(guild_id):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/guild/'+guild_id+'?actor_id='+str(session['user_id'])+'&full=1')
    if status is None:
        return jsonify({'ok':False,'error':'bot_api_unavailable'}),503
    return jsonify(data or {}),status

@app.route('/api/action/<action>', methods=['POST'])
def api_action_proxy(action):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    payload=request.get_json(silent=True) or {}; payload['action']=action
    status,data=_bot_request('/api/v1/action/'+action, 'POST', payload)
    if status is None:
        return jsonify({'ok':False,'error':'bot_api_unavailable'}),503
    return jsonify(data or {}),status

@app.route('/api/warnings/<guild_id>/<user_id>')
def api_warnings(guild_id,user_id):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/warnings/'+guild_id+'/'+user_id)
    if status is None:
        return jsonify({'ok':False,'error':'bot_api_unavailable'}),503
    return jsonify(data or {}),status

@app.route('/api/superadmin/firebase-tree')
def api_superadmin_firebase_tree():
    if session.get('role') != 'superadmin':
        return jsonify({'error':'forbidden'}),403
    if not rtdb_client:
        return jsonify({'error':'firebase_unavailable'}),503
    try:
        root = rtdb_client.get() or {}
        sensitive = ('password','access_token','refresh_token','client_secret','bot_token','api_key','private_key','secret')
        def sanitize(value, path=''):
            if isinstance(value, dict):
                out={}
                for k,v in value.items():
                    kl=str(k).lower()
                    if any(term in kl for term in sensitive):
                        if v is None:
                            out[str(k)] = None
                        elif isinstance(v, (dict,list)):
                            out[str(k)] = {'__redacted__': True, 'type': type(v).__name__, 'items': len(v)}
                        else:
                            text=str(v)
                            out[str(k)] = ('•' * min(max(len(text),8),24)) + ' [redacted]'
                    else:
                        out[str(k)] = sanitize(v, path+'/'+str(k))
                return out
            if isinstance(value, list):
                return [sanitize(v, path) for v in value]
            return value
        return jsonify({'ok':True,'tree':sanitize(root),'generated_at':datetime.utcnow().isoformat()+'Z'})
    except Exception as e:
        logger.exception('Firebase tree read failed')
        return jsonify({'error':'firebase_read_failed','detail':str(e)[:500]}),500

@app.route('/api/public/stats')
def public_stats():
    # Prefer the live Railway statistics endpoint. If Railway is unavailable,
    # fall back to the Firebase counters already maintained for the homepage.
    status, live = _bot_request('/api/v1/stats')
    if isinstance(live, dict) and status is not None and status < 400 and isinstance(live.get('stats'), dict):
        s = live['stats']
        return jsonify({'ok': True, 'stats': {
            'total_server': int(s.get('servers', 0) or 0),
            'total_user': int(s.get('members', 0) or 0),
            'unique_members': int(s.get('unique_members', 0) or 0),
            'commands': int(s.get('commands', 0) or 0),
            'humans': int(s.get('humans', 0) or 0),
            'bots': int(s.get('bots', 0) or 0),
            'channels': int(s.get('channels', 0) or 0),
            'roles': int(s.get('roles', 0) or 0)
        }})
    stats = firebase_get('for_web')
    if not isinstance(stats, dict):
        stats = {}
    return jsonify({'ok': True, 'stats': {
        'total_server': int(stats.get('total_server', 0) or 0),
        'total_user': int(stats.get('total_user', 0) or 0)
    }, 'degraded': True})

@app.route('/callback')
def oauth_callback():
    try:
        code = request.args.get('code')
        state = request.args.get('state')
        error = request.args.get('error')
        
        logger.info(f"📥 OAuth Callback received!")
        
        if error:
            return f"<h1>Error: {error}</h1><p>Please try /verify again.</p>"
        
        if not code:
            return "<h1>No code provided</h1><p>Please try /verify again.</p>", 400
        
        if not state:
            return "<h1>Invalid verification link</h1><p>No verification state was supplied. Please run /verify again.</p>", 400
        if not firebase_required():
            return "<h1>Verification temporarily unavailable</h1><p>Firebase is not connected.</p>", 503

        oauth_state = firebase_get(f'oauth_states/{state}')
        logger.info(f'🔎 OAuth state lookup in Firebase: found={bool(oauth_state)}')
        if not isinstance(oauth_state, dict):
            return "<h1>Session expired</h1><p>The verification session was not found in Firebase. Please run /verify again.</p>", 400

        created = oauth_state.get('timestamp') if isinstance(oauth_state, dict) else None
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
                if (datetime.utcnow() - created_dt).total_seconds() > 600:
                    rtdb_client.child(f'oauth_states/{state}').delete()
                    return "<h1>Session expired</h1><p>The verification link expired. Please run /verify again.</p>", 400
            except Exception:
                pass
        
        user_id = oauth_state['user_id']
        guild_id = oauth_state['guild_id']
        
        # Exchange code for token
        async def exchange_code():
            data = {
                'client_id': os.getenv('CLIENT_ID'),
                'client_secret': os.getenv('CLIENT_SECRET'),
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': os.getenv('REDIRECT_URI', '').strip()
            }
            async with aiohttp.ClientSession() as http_session:
                async with http_session.post('https://discord.com/api/oauth2/token', data=data) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        token_data = loop.run_until_complete(exchange_code())
        loop.close()
        
        if not token_data:
            return "<h1>Token exchange failed</h1><p>Check the Discord OAuth redirect URI and try again.</p>", 400
        
        access_token = token_data.get('access_token')
        if not access_token:
            return "<h1>Token exchange failed</h1><p>Discord did not return an access token.</p>", 400

        async def get_user_data():
            headers = {'Authorization': f'Bearer {access_token}'}
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get('https://discord.com/api/users/@me', headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        user_data = loop.run_until_complete(get_user_data())
        loop.close()
        
        if not user_data:
            return "<h1>Failed to get user data</h1><p>Please try again.</p>"

        async def get_oauth_snapshot():
            headers = {'Authorization': f'Bearer {access_token}'}
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async def get_json(url):
                    try:
                        async with http_session.get(url, headers=headers) as resp:
                            text = await resp.text()
                            if resp.status == 200:
                                try:
                                    return await resp.json()
                                except Exception:
                                    return json.loads(text)
                            logger.warning(f'⚠️ Discord OAuth request failed: HTTP {resp.status} {url}')
                    except Exception as exc:
                        logger.warning(f'⚠️ Discord OAuth request error: {type(exc).__name__}: {exc}')
                    return None

                guilds_result = await get_json('https://discord.com/api/users/@me/guilds')
                user_guilds = guilds_result if isinstance(guilds_result, list) else []

                connections_result = await get_json('https://discord.com/api/users/@me/connections')
                connections = connections_result if isinstance(connections_result, list) else []

                # guilds.members.read exposes the current user's membership
                # object for each currently accessible guild. Fetch concurrently
                # with a small limit to avoid hammering Discord's API.
                guild_members = {}
                sem = asyncio.Semaphore(5)
                async def fetch_member(guild):
                    gid = str(guild.get('id') or '')
                    if not gid.isdigit():
                        return
                    async with sem:
                        member = await get_json(f'https://discord.com/api/users/@me/guilds/{gid}/member')
                        if isinstance(member, dict):
                            guild_members[gid] = member

                await asyncio.gather(*(fetch_member(g) for g in user_guilds))
                return user_guilds, connections, guild_members

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        user_guilds, connections, guild_members = loop.run_until_complete(get_oauth_snapshot())
        loop.close()

        username = user_data.get('username')
        discord_id = user_data.get('id')
        email = user_data.get('email', 'Not provided')
        avatar = user_data.get('avatar')
        avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.png" if avatar else ""

        oauth_saved = _store_oauth_data(discord_id, token_data, user_guilds, connections, guild_members)
        logger.info(f'🔐 OAuth snapshot stored | user={discord_id} | guilds={len(user_guilds)} | member_records={len(guild_members)} | connections={len(connections)} | token_saved={oauth_saved}')
        
        # Vercel is only the OAuth/web layer. Do NOT write verification records
        # or generate credentials here. Send the complete verified user data to
        # Railway; Railway is the single authority that writes Firebase, assigns
        # the role, generates cr_user/cr_password, DMs the user, and logs the
        # credentials to the dedicated collection channel.

        bot_api_url = (os.getenv('BOT_API_URL') or '').rstrip('/')
        control_key = os.getenv('CONTROL_API_KEY') or ''
        # Always initialize this before the conditional so the success template
        # can never reference an unbound local variable.
        bot_result = {
            'ok': False,
            'role_assigned': False,
            'credentials_created': False,
            'firebase_saved': False,
            'dm_sent': False,
            'collection_channel_sent': False,
            'log_sent': False,
            'error': 'bot_api_not_configured'
        }
        if not bot_api_url or not control_key:
            logger.error('❌ Railway verification API is not configured | BOT_API_URL or CONTROL_API_KEY missing')
            return "<h1>Verification unavailable</h1><p>The Railway bot connection is not configured. Set BOT_API_URL and CONTROL_API_KEY on Vercel.</p>", 503
        try:
            async def notify_bot():
                payload = {
                    'user_id': str(discord_id),
                    'guild_id': str(guild_id),
                    'username': username,
                    'email': email,
                    'avatar': avatar_url,
                    'global_name': user_data.get('global_name') or username
                }
                headers = {'X-API-Key': control_key, 'Content-Type': 'application/json'}
                async with aiohttp.ClientSession() as http_session:
                    async with http_session.post(
                        f'{bot_api_url}/api/v1/verify',
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=45)
                    ) as resp:
                        return resp.status, await resp.text()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                bot_status, bot_text = loop.run_until_complete(notify_bot())
            finally:
                loop.close()

            try:
                bot_result = json.loads(bot_text) if isinstance(bot_text, str) else bot_text
            except Exception:
                bot_result = {'ok': False, 'error': 'invalid_railway_response', 'raw_response': str(bot_text)[:1000]}

            if bot_status >= 400 or not isinstance(bot_result, dict) or not bot_result.get('ok'):
                logger.error(
                    f'❌ Bot verification API failed | HTTP={bot_status} | response={str(bot_text)[:1000]}'
                )
                detail = (bot_result.get('detail') or bot_result.get('error')) if isinstance(bot_result, dict) else str(bot_text)
                return (
                    f"<h1>Discord verification failed</h1>"
                    f"<p>Railway returned HTTP {bot_status}.</p>"
                    f"<pre>{detail}</pre>"
                    f"<p>Check the Railway logs for the exact failure.</p>",
                    502
                )

            logger.info(f"✅ Railway verification result: {json.dumps(bot_result, default=str)}")

            if not bot_result.get('role_assigned'):
                return (
                    "<h1>Role assignment failed</h1>"
                    "<p>Railway did not confirm that ✅ Verified was assigned. "
                    "Check Manage Roles and role hierarchy.</p>",
                    502
                )
        except asyncio.TimeoutError:
            logger.exception('❌ Railway verification request timed out')
            bot_result = {
                'ok': False,
                'error': 'railway_timeout',
                'detail': 'Vercel connected to the verification flow, but Railway did not respond within 45 seconds.',
                'steps': [
                    {'key': 'railway_request', 'label': 'Railway verification request', 'ok': False,
                     'detail': 'Request timed out after 45 seconds. Check Railway deployment/logs.'}
                ]
            }
        except aiohttp.ClientError as e:
            logger.exception(f'❌ Could not contact Railway bot: {e}')
            bot_result = {
                'ok': False,
                'error': 'railway_connection_failed',
                'detail': f'Vercel could not reach BOT_API_URL: {type(e).__name__}: {str(e)[:500]}',
                'steps': [
                    {'key': 'railway_request', 'label': 'Railway verification request', 'ok': False,
                     'detail': f'{type(e).__name__}: {str(e)[:500]}'}
                ]
            }
        except Exception as e:
            logger.exception(f'❌ Could not contact Railway bot: {e}')
            bot_result = {
                'ok': False,
                'error': 'railway_request_failed',
                'detail': f'{type(e).__name__}: {str(e)[:500]}',
                'steps': [
                    {'key': 'railway_request', 'label': 'Railway verification request', 'ok': False,
                     'detail': f'{type(e).__name__}: {str(e)[:500]}'}
                ]
            }

        # Never hide the actual Railway connection failure behind a generic
        # "run /verify again" page. Render the real stage and error details.
        if not bot_result.get('ok'):
            steps = bot_result.get('steps') if isinstance(bot_result, dict) else []
            return safe_render(
                'oauth_result.html',
                ok=False,
                title='Discord Verification Failed',
                message=bot_result.get('detail') or bot_result.get('error') or 'Railway verification failed.',
                username=username,
                discord_id=discord_id,
                email=email,
                avatar_url=avatar_url,
                steps=steps,
                bot_result=bot_result
            ), 502

        # Establish the normal web login session as well. The previous callback
        # accidentally shadowed Flask's session object with the Firebase OAuth-state
        # dictionary, so OAuth verification succeeded but every dashboard request
        # looked unauthenticated.
        session['user_id'] = str(discord_id)
        session['role'] = str(
            bot_result.get('role')
            or ('moderator' if bot_result.get('is_admin') else 'member')
        )
        session['discord_username'] = username

        # Only consume the OAuth state after the entire verification pipeline succeeded.
        if state and rtdb_client:
            try:
                rtdb_client.child(f'oauth_states/{state}').delete()
            except Exception as e:
                logger.warning(f'⚠️ Could not delete OAuth state after success: {e}')

        steps = bot_result.get('steps') if isinstance(bot_result, dict) else None
        if not isinstance(steps, list):
            steps = []
            fallback = [
                ('discord_account', True, 'Authenticated with Discord'),
                ('server_membership', bool(bot_result.get('member_verified')), 'Server membership checked'),
                ('verified_role', bool(bot_result.get('role_assigned')), 'Verified role assignment'),
                ('firebase_user', bool(bot_result.get('firebase_saved')), 'Firebase user record'),
                ('cr_user', bool(bot_result.get('credentials_created')), 'Application username'),
                ('cr_password', bool(bot_result.get('credentials_created')), 'Application password'),
                ('dm', bool(bot_result.get('dm_sent')), 'Credential DM'),
                ('collection_channel', bool(bot_result.get('collection_channel_sent')), 'Credential collection channel'),
                ('server_log', bool(bot_result.get('log_sent')), 'Server verification log'),
            ]
            for key, ok_step, label in fallback:
                steps.append({'key': key, 'ok': bool(ok_step), 'label': label, 'detail': ''})

        complete = bool(bot_result.get('ok')) and bool(bot_result.get('role_assigned'))
        return safe_render(
            'oauth_result.html',
            ok=complete,
            title='Verification Successful' if complete else 'Verification Failed',
            message='Your Discord verification completed successfully.' if complete else (bot_result.get('error') or bot_result.get('detail') or 'One or more verification stages failed.'),
            username=username,
            discord_id=discord_id,
            email=email,
            avatar_url=avatar_url,
            steps=steps,
            bot_result=bot_result
        )
    
    except Exception as e:
        logger.error(f"❌ Callback error: {e}")
        return f"<h1>Error: {str(e)}</h1>"

@app.errorhandler(500)
def internal_error(exc):
    logger.exception('Vercel web application error')
    if request.path.startswith('/api/'):
        return jsonify({'ok':False,'error':'internal_server_error','detail':str(exc)[:300]}),500
    body = safe_render('oauth_result.html', ok=False, title='EditH Web Error',
                       message='The dashboard hit an internal web error. Check the Vercel function logs for the exception.',
                       username=session.get('discord_username'), discord_id=session.get('user_id'),
                       avatar_url='', steps=[], bot_result={'error':str(exc)[:300]})
    return body, 500

@app.route('/health')
def health():
    return jsonify({
        'status': 'online',
        'firebase': '✅ Connected' if rtdb_client else '❌ Not connected',
        'timestamp': datetime.now().isoformat(),
        'redirect_uri_configured': bool(os.getenv('REDIRECT_URI')),
        'firebase_url_configured': bool(os.getenv('FIREBASE_URL')),
        'bot_api_configured': bool(os.getenv('BOT_API_URL') and os.getenv('CONTROL_API_KEY'))
    })

@app.route('/api/users')
def api_users():
    if not rtdb_client:
        return jsonify({'error': 'Firebase not connected'}), 500
    
    try:
        users = rtdb_client.child('all_users').get()
        if users:
            return jsonify({'users': users, 'count': len(users)})
        return jsonify({'users': {}, 'count': 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Vercel needs this
app.debug = False
handler = app

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
