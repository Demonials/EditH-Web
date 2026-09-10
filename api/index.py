from flask import Flask, request, redirect, jsonify, send_from_directory, render_template, session
import os
import json
import secrets
import asyncio
import aiohttp
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
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

# ============ ROUTES ============
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(app.root_path, 'static'), filename)

@app.route('/favicon.ico')
def favicon():
    return ('', 204)

@app.route('/')
def home():
    return render_template('index.html', config_client_id=os.getenv('CLIENT_ID',''))

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
        if uid:
            record = firebase_get('credentials/' + str(uid))
            if isinstance(record, dict) and secrets.compare_digest(str(record.get('password','')), password):
                session['user_id'] = str(uid)
                session['role'] = str(record.get('role','member'))
                return redirect('/moderator' if session['role'] == 'moderator' else '/user')
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

def _login_required():
    return bool(session.get('user_id'))

def _bot_request(path, method='GET', payload=None):
    base=(os.getenv('BOT_API_URL') or '').rstrip('/')
    key=os.getenv('CONTROL_API_KEY') or ''
    if not base or not key: return None, 503
    async def run():
        headers={'X-API-Key':key,'X-Actor-ID':str(session.get('user_id','')),'Content-Type':'application/json'}
        async with aiohttp.ClientSession() as hs:
            url=base+path
            if method=='POST':
                async with hs.post(url,json=payload or {},headers=headers,timeout=20) as r: return r.status, await r.json(content_type=None)
            async with hs.get(url,headers=headers,timeout=20) as r: return r.status, await r.json(content_type=None)
    loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(run())
    finally: loop.close()

@app.route('/user')
def user_page():
    if not _login_required(): return redirect('/login')
    return render_template('user.html')

@app.route('/moderator')
def moderator_page():
    if not _login_required() or session.get('role') not in ('moderator','superadmin'): return redirect('/login')
    return render_template('moderator.html')

@app.route('/superadmin')
def superadmin_page():
    if not _login_required() or session.get('role') != 'superadmin': return redirect('/login')
    return render_template('superadmin.html')

@app.route('/api/me')
def api_me():
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    uid=str(session['user_id']); creds=firebase_get('credentials/'+uid); profile=firebase_get('profiles/'+uid)
    return jsonify({'ok':True,'user':{'id':uid,'username':profile.get('username') if isinstance(profile,dict) else None,'profile':profile or {},'credentials':creds or {},'role':session.get('role','member')}})

@app.route('/api/guilds')
def api_guilds():
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/user/'+str(session['user_id'])+'/guilds')
    return jsonify(data or {'guilds':[]}), status

@app.route('/api/superadmin/guilds')
def api_superadmin_guilds():
    if session.get('role')!='superadmin': return jsonify({'error':'forbidden'}),403
    status,data=_bot_request('/api/v1/superadmin/guilds'); return jsonify(data or {}),status

@app.route('/api/guild/<guild_id>')
def api_guild_proxy(guild_id):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/guild/'+guild_id+'?actor_id='+str(session['user_id'])+'&full=1'); return jsonify(data or {}),status

@app.route('/api/action/<action>', methods=['POST'])
def api_action_proxy(action):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    payload=request.get_json(silent=True) or {}; payload['action']=action
    status,data=_bot_request('/api/v1/action', 'POST', payload); return jsonify(data or {}),status

@app.route('/api/warnings/<guild_id>/<user_id>')
def api_warnings(guild_id,user_id):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/warnings/'+guild_id+'/'+user_id); return jsonify(data or {}),status

@app.route('/api/public/stats')
def public_stats():
    # Public homepage stats are served directly from Firebase so Vercel handles
    # read-only web traffic without waking/calling the Railway bot.
    stats = firebase_get('for_web')
    if not isinstance(stats, dict):
        return jsonify({'ok': True, 'stats': {'total_server': 0, 'total_user': 0}})
    return jsonify({
        'ok': True,
        'stats': {
            'total_server': int(stats.get('total_server', 0) or 0),
            'total_user': int(stats.get('total_user', 0) or 0)
        }
    })

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

        session = firebase_get(f'oauth_states/{state}')
        logger.info(f'🔎 OAuth state lookup in Firebase: found={bool(session)}')
        if not isinstance(session, dict):
            return "<h1>Session expired</h1><p>The verification session was not found in Firebase. Please run /verify again.</p>", 400

        created = session.get('timestamp') if isinstance(session, dict) else None
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace('Z', '+00:00')).replace(tzinfo=None)
                if (datetime.utcnow() - created_dt).total_seconds() > 600:
                    rtdb_client.child(f'oauth_states/{state}').delete()
                    return "<h1>Session expired</h1><p>The verification link expired. Please run /verify again.</p>", 400
            except Exception:
                pass
        
        user_id = session['user_id']
        guild_id = session['guild_id']
        
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
        
        username = user_data.get('username')
        discord_id = user_data.get('id')
        email = user_data.get('email', 'Not provided')
        avatar = user_data.get('avatar')
        avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.png" if avatar else ""
        
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
            return render_template(
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
        return render_template(
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
