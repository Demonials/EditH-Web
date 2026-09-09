from flask import Flask, request, redirect, jsonify, render_template, session, flash, url_for
import os
import json
import secrets
import asyncio
import aiohttp
from datetime import datetime, timedelta
import logging
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(hours=24)

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

# ============ HELPER FUNCTIONS ============
def get_server_stats():
    """Get REAL statistics from Firebase"""
    stats = {
        'total_servers': 0,
        'total_users': 0,
        'total_commands': 0,
        'uptime': '99.9%'
    }
    
    if rtdb_client:
        try:
            # Get all guilds
            guilds = rtdb_client.child('guilds').get()
            if guilds:
                stats['total_servers'] = len(guilds)
                
                # Count verified users across all guilds
                total_users = 0
                for guild_id, guild_data in guilds.items():
                    if isinstance(guild_data, dict):
                        verified = guild_data.get('verified', {})
                        if isinstance(verified, dict):
                            total_users += len(verified)
                stats['total_users'] = total_users
            
            # Get command count from stats
            cmd_stats = rtdb_client.child('stats/commands').get()
            if cmd_stats:
                stats['total_commands'] = cmd_stats.get('total', 0)
                
        except Exception as e:
            logger.error(f"Error fetching stats: {e}")
    
    return stats

def get_commands_list():
    """Get REAL commands from Firebase"""
    commands = []
    
    if rtdb_client:
        try:
            cmd_data = rtdb_client.child('commands').get()
            if cmd_data:
                # Convert to list if it's a dict
                if isinstance(cmd_data, dict):
                    return list(cmd_data.values())
                return cmd_data
        except Exception as e:
            logger.error(f"Error fetching commands: {e}")
    
    # Fallback default commands if Firebase fails
    return [
        {'name': '/ban', 'desc': 'Ban a user from the server', 'perm': 'admin'},
        {'name': '/kick', 'desc': 'Kick a user from the server', 'perm': 'admin'},
        {'name': '/ping', 'desc': 'Check bot latency', 'perm': 'user'},
        {'name': '/setup', 'desc': 'Setup the server automatically', 'perm': 'admin'},
        {'name': '/info', 'desc': 'Get server information', 'perm': 'user'},
        {'name': '/verify', 'desc': 'Start Discord OAuth verification', 'perm': 'user'},
        {'name': '/sync', 'desc': 'Sync members and generate credentials', 'perm': 'admin'},
        {'name': '/credentials', 'desc': 'Get your login credentials', 'perm': 'user'},
        {'name': '/get_creds', 'desc': 'Get credentials for a user', 'perm': 'admin'},
        {'name': '/reset_creds', 'desc': 'Reset credentials for a user', 'perm': 'admin'}
    ]

def get_features():
    """Get REAL features from Firebase"""
    features = []
    
    if rtdb_client:
        try:
            feat_data = rtdb_client.child('features').get()
            if feat_data:
                if isinstance(feat_data, dict):
                    return list(feat_data.values())
                return feat_data
        except Exception as e:
            logger.error(f"Error fetching features: {e}")
    
    # Fallback default features
    return [
        {'icon': '◈', 'title': 'Moderation', 'desc': 'Keep your server safe with advanced moderation tools and auto-actions.'},
        {'icon': '⚙', 'title': 'Utility', 'desc': 'Useful tools like server info, user info, reminders, and more.'},
        {'icon': '⌁', 'title': 'Fun', 'desc': 'Games, memes, economy, and entertainment for your community.'},
        {'icon': '◇', 'title': 'Security', 'desc': 'Anti-raid, link protection, and advanced security features.'},
        {'icon': '✣', 'title': 'Customisation', 'desc': 'Make it yours with custom commands, roles, and settings.'},
        {'icon': 'ϟ', 'title': 'Fast & Reliable', 'desc': 'Built for speed, with 24/7 uptime and minimal downtime.'}
    ]

def verify_credentials(username, password):
    """Verify login credentials against Firebase"""
    if not rtdb_client:
        return None
    
    try:
        creds_data = rtdb_client.child('credentials').get()
        
        if not creds_data:
            return None
        
        for user_id, creds in creds_data.items():
            if creds.get('username') == username and creds.get('password') == password:
                return {
                    'user_id': user_id,
                    'username': creds.get('username'),
                    'role': creds.get('role', 'user'),
                    'created_at': creds.get('created_at')
                }
        
        return None
    except Exception as e:
        logger.error(f"❌ Verification error: {e}")
        return None

def get_user_data(user_id):
    """Get user data from Firebase"""
    if not rtdb_client:
        return None
    
    try:
        creds = rtdb_client.child(f'credentials/{user_id}').get()
        if not creds:
            return None
        
        user_data = {
            'user_id': user_id,
            'username': creds.get('username'),
            'role': creds.get('role', 'user'),
            'created_at': creds.get('created_at'),
            'verified_in': []
        }
        
        # Find which guilds this user is verified in
        guilds = rtdb_client.child('guilds').get()
        if guilds:
            for guild_id, guild_data in guilds.items():
                if isinstance(guild_data, dict):
                    verified = guild_data.get('verified', {})
                    if user_id in verified:
                        user_data['verified_in'].append({
                            'guild_id': guild_id,
                            'guild_name': guild_data.get('config', {}).get('name', 'Unknown Server')
                        })
        
        return user_data
    except Exception as e:
        logger.error(f"❌ Error getting user data: {e}")
        return None

def get_all_users():
    """Get all users from Firebase"""
    if not rtdb_client:
        return []
    
    try:
        creds_data = rtdb_client.child('credentials').get()
        if not creds_data:
            return []
        
        users = []
        for user_id, creds in creds_data.items():
            users.append({
                'user_id': user_id,
                'username': creds.get('username'),
                'role': creds.get('role', 'user'),
                'created_at': creds.get('created_at')
            })
        
        return sorted(users, key=lambda x: x.get('created_at', ''), reverse=True)
    except Exception as e:
        logger.error(f"❌ Error getting users: {e}")
        return []

def update_user_role(user_id, new_role):
    """Update a user's role in Firebase"""
    if not rtdb_client:
        return False
    
    try:
        creds = rtdb_client.child(f'credentials/{user_id}').get()
        if not creds:
            return False
        
        creds['role'] = new_role
        rtdb_client.child(f'credentials/{user_id}').set(creds)
        logger.info(f"✅ Updated role for {user_id} to {new_role}")
        return True
    except Exception as e:
        logger.error(f"❌ Error updating role: {e}")
        return False

# ============ DECORATORS ============
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page.', 'warning')
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# ============ ROUTES ============

@app.route('/')
def home():
    """Landing page with REAL data"""
    stats = get_server_stats()
    features = get_features()
    
    return render_template('login.html', 
                         stats=stats,
                         features=features[:6])

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            flash('Please enter both username and password.', 'warning')
            return render_template('login.html')
        
        user_data = verify_credentials(username, password)
        
        if user_data:
            session.permanent = True
            session['user_id'] = user_data['user_id']
            session['username'] = user_data['username']
            session['role'] = user_data['role']
            session['logged_in'] = True
            
            logger.info(f"✅ User logged in: {username} ({user_data['role']})")
            flash(f'Welcome back, {username}!', 'success')
            
            role = user_data['role']
            if role == 'super_admin':
                return redirect(url_for('superadmin_dashboard'))
            elif role == 'moderator':
                return redirect(url_for('moderator_dashboard'))
            else:
                return redirect(url_for('user_dashboard'))
        else:
            flash('Invalid username or password.', 'danger')
            return render_template('login.html')
    
    if 'user_id' in session:
        role = session.get('role', 'user')
        if role == 'super_admin':
            return redirect(url_for('superadmin_dashboard'))
        elif role == 'moderator':
            return redirect(url_for('moderator_dashboard'))
        else:
            return redirect(url_for('user_dashboard'))
    
    return render_template('login.html')

@app.route('/dashboard/user')
@login_required
def user_dashboard():
    """User dashboard"""
    stats = get_server_stats()
    commands = get_commands_list()
    user_data = get_user_data(session.get('user_id'))
    
    return render_template('user.html', 
                         user=user_data,
                         stats=stats,
                         commands=commands[:8])

@app.route('/dashboard/moderator')
@login_required
def moderator_dashboard():
    """Moderator dashboard"""
    if session.get('role') not in ['moderator', 'super_admin']:
        flash('Access denied. Moderator privileges required.', 'danger')
        return redirect(url_for('user_dashboard'))
    
    stats = get_server_stats()
    commands = get_commands_list()
    user_data = get_user_data(session.get('user_id'))
    
    return render_template('moderator.html', 
                         user=user_data,
                         stats=stats,
                         commands=commands[:8])

@app.route('/dashboard/superadmin')
@login_required
def superadmin_dashboard():
    """Super Admin dashboard"""
    if session.get('role') != 'super_admin':
        flash('Access denied. Super Admin privileges required.', 'danger')
        return redirect(url_for('user_dashboard'))
    
    stats = get_server_stats()
    commands = get_commands_list()
    user_data = get_user_data(session.get('user_id'))
    all_users = get_all_users()
    
    return render_template('superadmin.html', 
                         user=user_data,
                         stats=stats,
                         commands=commands[:8],
                         all_users=all_users)

@app.route('/admin/update_role', methods=['POST'])
@login_required
def update_role():
    """Update user role (Super Admin only)"""
    if session.get('role') != 'super_admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('user_dashboard'))
    
    user_id = request.form.get('user_id')
    new_role = request.form.get('new_role')
    
    if not user_id or not new_role:
        flash('Missing required fields.', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    
    if new_role not in ['user', 'moderator', 'super_admin']:
        flash('Invalid role.', 'danger')
        return redirect(url_for('superadmin_dashboard'))
    
    if update_user_role(user_id, new_role):
        flash(f'✅ User role updated to {new_role}.', 'success')
    else:
        flash('❌ Failed to update user role.', 'danger')
    
    return redirect(url_for('superadmin_dashboard'))

@app.route('/logout', methods=['POST'])
def logout():
    username = session.get('username', 'Unknown')
    session.clear()
    flash(f'Goodbye, {username}!', 'success')
    return redirect(url_for('login_page'))

# ============ OAUTH CALLBACK ============
@app.route('/callback')
def oauth_callback():
    # Your existing callback code here
    pass

# ============ API ENDPOINTS ============
@app.route('/api/stats')
def api_stats():
    stats = get_server_stats()
    return jsonify(stats)

@app.route('/api/commands')
def api_commands():
    commands = get_commands_list()
    return jsonify({'commands': commands, 'count': len(commands)})

@app.route('/api/users')
def api_users():
    if not rtdb_client:
        return jsonify({'error': 'Firebase not connected'}), 500
    
    try:
        users = rtdb_client.child('credentials').get()
        if users:
            return jsonify({'users': users, 'count': len(users)})
        return jsonify({'users': {}, 'count': 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    stats = get_server_stats()
    return jsonify({
        'status': 'online',
        'firebase': '✅ Connected' if rtdb_client else '❌ Not connected',
        'timestamp': datetime.now().isoformat(),
        'stats': stats
    })

# ============ RUN ============
app.debug = False

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080, debug=True)
