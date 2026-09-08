from flask import Flask, request, redirect, jsonify
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
app.secret_key = secrets.token_hex(32)

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

# ============ ROUTES ============
@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>EDITH Bot - Verification</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: white; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
            .container { background: #2d2d44; padding: 50px; border-radius: 20px; text-align: center; max-width: 500px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,0.5); border: 1px solid #3d3d5c; }
            .logo { font-size: 80px; margin-bottom: 20px; }
            h1 { color: #ffffff; font-size: 32px; margin-bottom: 10px; }
            .subtitle { color: #b5b5c4; font-size: 16px; margin-bottom: 30px; }
            .status { background: #1e1e32; padding: 20px; border-radius: 12px; margin: 20px 0; }
            .status .label { color: #6d6d8a; font-size: 13px; }
            .status .value { color: #4caf50; font-weight: 600; font-size: 16px; }
            .footer { color: #4d4d6a; font-size: 12px; margin-top: 30px; border-top: 1px solid #2d2d44; padding-top: 20px; }
            .badge { display: inline-block; background: #4caf50; color: white; padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 600; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="logo">🤖</div>
            <h1>EDITH Bot</h1>
            <p class="subtitle">Ultimate Server Management Bot</p>
            <div class="status">
                <div style="margin-bottom: 10px;"><span class="label">Status</span></div>
                <div><span class="value">✅ Online & Ready</span></div>
                <div style="margin-top: 10px;"><span class="badge">Verification System Active</span></div>
            </div>
            <p style="color: #b5b5c4; font-size: 14px; margin: 20px 0;">
                Use <code style="background: #1a1a2e; padding: 4px 8px; border-radius: 4px; color: #5865f2;">/verify</code> in Discord to start verification.
            </p>
            <p class="footer">EDITH Authentication System v2.0 • Built with ❤️</p>
        </div>
    </body>
    </html>
    """

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
        
        session = None
        if state and rtdb_client:
            try:
                session = rtdb_client.child(f'oauth_states/{state}').get()
                if session:
                    rtdb_client.child(f'oauth_states/{state}').delete()
            except:
                pass
        
        if not session:
            return "<h1>Session expired</h1><p>Please run /verify again.</p>"
        
        user_id = session['user_id']
        guild_id = session['guild_id']
        
        # Exchange code for token
        async def exchange_code():
            data = {
                'client_id': os.getenv('CLIENT_ID'),
                'client_secret': os.getenv('CLIENT_SECRET'),
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': os.getenv('REDIRECT_URI', 'https://your-vercel-app.vercel.app/callback')
            }
            async with aiohttp.ClientSession() as session:
                async with session.post('https://discord.com/api/oauth2/token', data=data) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        token_data = loop.run_until_complete(exchange_code())
        loop.close()
        
        if not token_data:
            return "<h1>Token exchange failed</h1><p>Please try again.</p>"
        
        access_token = token_data.get('access_token')
        
        async def get_user_data():
            headers = {'Authorization': f'Bearer {access_token}'}
            async with aiohttp.ClientSession() as session:
                async with session.get('https://discord.com/api/users/@me', headers=headers) as resp:
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
        
        # Store in Firebase
        if rtdb_client:
            try:
                rtdb_client.child(f'guilds/{guild_id}/verified/{discord_id}').set({
                    'discord_id': discord_id,
                    'username': username,
                    'email': email,
                    'avatar': avatar_url,
                    'guild_id': guild_id,
                    'verified_at': datetime.now().isoformat(),
                    'verified': True
                })
                logger.info(f"✅ Stored in Firebase: {username}")
            except Exception as e:
                logger.error(f"❌ Firebase storage failed: {e}")
        
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
                <h1>Verification Successful!</h1>
                <p class="subtitle">Welcome to the server! 🎉</p>
                
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
                
                <div class="status-box">✅ You now have full access to the server!</div>
                
                <a href="https://discord.com/app" class="button">Return to Discord</a>
                
                <p class="footer">You can now close this tab. A verification DM has been sent to you.</p>
            </div>
        </body>
        </html>
        """
    
    except Exception as e:
        logger.error(f"❌ Callback error: {e}")
        return f"<h1>Error: {str(e)}</h1>"

@app.route('/health')
def health():
    return jsonify({
        'status': 'online',
        'firebase': '✅ Connected' if rtdb_client else '❌ Not connected',
        'timestamp': datetime.now().isoformat()
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
