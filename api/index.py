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
app.secret_key = os.getenv('FLASK_SECRET_KEY') or 'edith-development-secret-change-this'
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
    if not os.getenv('BOT_API_URL') or not os.getenv('CONTROL_API_KEY'):
        return jsonify({'error':'bot_api_not_configured','detail':'Set BOT_API_URL and CONTROL_API_KEY in Vercel.'}),503
    if not os.getenv('SUPER_ADMIN_ID'):
        return jsonify({'error':'super_admin_id_not_configured','detail':'Set SUPER_ADMIN_ID in Vercel.'}),503
    status,data=_bot_request('/api/v1/superadmin/guilds')
    if not data:
        data={'error':'bot_api_unavailable','status':status}
    return jsonify(data),status

@app.route('/api/guild/<guild_id>')
def api_guild_proxy(guild_id):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/guild/'+guild_id+'?actor_id='+str(session['user_id'])+'&full=1'); return jsonify(data or {}),status

@app.route('/api/action/<action>', methods=['POST'])
def api_action_proxy(action):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    payload=request.get_json(silent=True) or {}
    status,data=_bot_request('/api/v1/action/'+action, 'POST', payload)
    return jsonify(data or {'error':'bot_api_unavailable'}),status

@app.route('/api/warnings/<guild_id>/<user_id>')
def api_warnings(guild_id,user_id):
    if not _login_required(): return jsonify({'error':'unauthorized'}),401
    status,data=_bot_request('/api/v1/warnings/'+guild_id+'/'+user_id); return jsonify(data or {}),status

@app.route('/api/public/stats')
def public_stats():
    status,data=_bot_request('/api/v1/stats')
    return jsonify(data or {'ok':False,'stats':{}}),status

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
        
        # The Railway bot is the authoritative verifier. Do NOT mark the user verified in
        # Firebase before Discord role assignment succeeds. This prevents stale/false verified data.
        bot_api_url = (os.getenv('BOT_API_URL') or '').rstrip('/')
        control_key = os.getenv('CONTROL_API_KEY') or ''
        if not bot_api_url or not control_key:
            logger.error('BOT_API_URL or CONTROL_API_KEY is missing')
            return "<h1>Verification unavailable</h1><p>The EditH control API is not configured. Please contact the administrator.</p>", 503
        try:
            async def notify_bot():
                payload = {'user_id': str(discord_id), 'guild_id': str(guild_id), 'email': email}
                headers = {'X-API-Key': control_key, 'Content-Type': 'application/json'}
                async with aiohttp.ClientSession() as http_session:
                    async with http_session.post(f'{bot_api_url}/api/v1/verify', json=payload, headers=headers, timeout=20) as resp:
                        return resp.status, await resp.text()
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            bot_status, bot_text = loop.run_until_complete(notify_bot()); loop.close()
            try:
                bot_result = json.loads(bot_text) if isinstance(bot_text, str) else bot_text
            except Exception:
                bot_result = {}
            if bot_status >= 400 or not isinstance(bot_result, dict) or not bot_result.get('ok'):
                logger.error(f'EditH verification API failed ({bot_status}): {bot_text[:800]}')
                return f"<h1>EditH verification failed</h1><p>Railway returned HTTP {bot_status}.</p><pre>{bot_text[:1600]}</pre>", 502
            if not bot_result.get('role_assigned'):
                return "<h1>Verification failed</h1><p>EditH did not confirm the Verified role assignment.</p>", 502

            # Store the OAuth-discovered profile only AFTER Railway confirms Discord verification.
            if rtdb_client:
                firebase_set(f'profiles/{discord_id}', {
                    'discord_id':str(discord_id),'username':username,'email':email,
                    'avatar':avatar_url,'verified':True,
                    'verified_guild_id':str(guild_id),'updated_at':datetime.utcnow().isoformat(),
                    'login_role':bot_result.get('role_type','member')
                })
            dm_note = 'Your login credentials were sent to your Discord DM.' if bot_result.get('dm_sent') else 'Your credentials were created and saved, but Discord did not allow the bot to send the DM.'
        except Exception as e:
            logger.exception(f'Could not contact Railway EditH bot: {e}')
            return "<h1>EditH verification unavailable</h1><p>Could not reach the bot. Please try again.</p>", 502

        # Only consume the OAuth state after the entire verification pipeline succeeded.
        if state and rtdb_client:
            try:
                rtdb_client.child(f'oauth_states/{state}').delete()
            except Exception as e:
                logger.warning(f'⚠️ Could not delete OAuth state after success: {e}')

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Verification Successful</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: white; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }}
                .container {{ background: #2d2d44; padding: 50px; border-radius: 20px; text-align: center; max-width: 500px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,0.5); border: 1px solid #3d3d5c; }}
                .success {{ color: #4caf50; font-size: 80px; margin-bottom: 20px; }}
                h1 {{ color: #ffffff; font-size: 28px; margin-bottom: 10px; }}
                .subtitle {{ color: #b5b5c4; font-size: 16px; margin-bottom: 30px; }}
                .user-info {{ background: #1e1e32; border-radius: 12px; padding: 20px; margin: 20px 0; text-align: left; }}
                .user-info .row {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #2d2d44; }}
                .user-info .row:last-child {{ border-bottom: none; }}
                .user-info .label {{ color: #6d6d8a; font-size: 13px; }}
                .user-info .value {{ color: #ffffff; font-size: 14px; }}
                .status-box {{ padding: 15px; border-radius: 10px; margin: 15px 0; background: #1e1e32; color: #4caf50; font-weight: 600; }}
                .button {{ background: #5865f2; color: white; border: none; padding: 16px 40px; font-size: 18px; font-weight: 600; border-radius: 10px; cursor: pointer; width: 100%; margin-top: 20px; text-decoration: none; display: inline-block; }}
                .button:hover {{ background: #4752c4; transform: translateY(-2px); box-shadow: 0 10px 30px rgba(88,101,242,0.3); }}
                .footer {{ margin-top: 25px; color: #4d4d6a; font-size: 12px; border-top: 1px solid #2d2d44; padding-top: 20px; }}
                .badge {{ display: inline-block; background: #4caf50; color: white; padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
                .avatar {{ width: 80px; height: 80px; border-radius: 50%; margin: 10px auto; display: block; border: 3px solid #5865f2; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="success">✅</div>
                <h1>EditH Verification Successful!</h1>
                <p class="subtitle">Welcome to EditH! 🎉</p>
                
                <img src="{avatar_url}" class="avatar" onerror="this.style.display='none'">
                
                <div class="user-info">
                    <div class="row">
                        <span class="label">👤 Username</span>
                        <span class="value">{username}</span>
                    </div>
                    <div class="row">
                        <span class="label">🆔 User ID</span>
                        <span class="value">{discord_id}</span>
                    </div>
                    <div class="row">
                        <span class="label">📧 Email</span>
                        <span class="value">{email}</span>
                    </div>
                    <div class="row">
                        <span class="label">🔓 Status</span>
                        <span class="value"><span class="badge">Verified ✅</span></span>
                    </div>
                </div>
                
                <div class="status-box">{dm_note}<br>✅ You now have full access to the server!</div>
                
                <a href="https://discord.com/app" class="button">Return to Discord</a>
                
                <p class="footer">You can now close this tab. A verification DM has been sent to you.</p>
            </div>
        </body>
        </html>
        """
    
    except Exception as e:
        logger.error(f"❌ Callback error: {e}")
        return f"<h1>Error: {str(e)}</h1>"

@app.route('/api/public/stats')
def api_public_stats():
    # Homepage stats are live Discord data from the Railway EditH bot.
    if not os.getenv('BOT_API_URL') or not os.getenv('CONTROL_API_KEY'):
        return jsonify({'error':'bot_api_not_configured','stats':{}}),503
    status,data=_bot_request('/api/v1/stats')
    if not data:
        return jsonify({'error':'bot_api_unavailable','stats':{},'status':status}),status
    return jsonify(data),status

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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
