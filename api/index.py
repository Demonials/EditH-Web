from flask import Flask, request, redirect, jsonify, render_template_string
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

# ============ HELPER FUNCTIONS ============
def get_server_stats():
    """Get real-time server statistics from Firebase"""
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
    """Get real-time commands from Firebase"""
    commands = []
    
    if rtdb_client:
        try:
            cmd_data = rtdb_client.child('commands').get()
            if cmd_data:
                return cmd_data
        except Exception as e:
            logger.error(f"Error fetching commands: {e}")
    
    # Fallback default commands
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
    """Get real-time features from Firebase"""
    features = []
    
    if rtdb_client:
        try:
            feat_data = rtdb_client.child('features').get()
            if feat_data:
                return feat_data
        except Exception as e:
            logger.error(f"Error fetching features: {e}")
    
    # Fallback default features
    return [
        {'icon': '◈', 'title': 'Moderation', 'desc': 'Keep your server safe with advanced moderation tools and auto-actions.', 'tag': 'Essential'},
        {'icon': '⚙', 'title': 'Utility', 'desc': 'Useful tools like server info, user info, reminders, and more.', 'tag': 'Essential'},
        {'icon': '⌁', 'title': 'Fun', 'desc': 'Games, memes, economy, and entertainment for your community.', 'tag': 'Fun'},
        {'icon': '◇', 'title': 'Security', 'desc': 'Anti-raid, link protection, and advanced security features.', 'tag': 'Security'},
        {'icon': '✣', 'title': 'Customisation', 'desc': 'Make it yours with custom commands, roles, and settings.', 'tag': 'Essential'},
        {'icon': 'ϟ', 'title': 'Fast & Reliable', 'desc': 'Built for speed, with 24/7 uptime and minimal downtime.', 'tag': 'Performance'}
    ]

# ============ HTML TEMPLATE ============
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>EDITH — The Discord Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;600;700;800;900&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{
      --red:#5865F2;
      --red2:#7984F5;
      --deep:#07090d;
      --panel:#0c1016;
      --line:rgba(88,101,242,.28);
      --text:#f5f7fa;
      --muted:#9ca5b3;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{
      background:#05070a;
      color:var(--text);
      font-family:Inter,system-ui,sans-serif;
      overflow-x:hidden;
    }
    body:before{
      content:"";
      position:fixed;inset:0;pointer-events:none;z-index:20;
      background:linear-gradient(rgba(255,255,255,.012) 1px,transparent 1px);
      background-size:100% 4px;
      opacity:.35;
    }
    a{color:inherit;text-decoration:none}
    .page{
      min-height:100vh;
      background:
        radial-gradient(circle at 82% 15%,rgba(88,101,242,.10),transparent 25%),
        radial-gradient(circle at 15% 40%,rgba(88,101,242,.045),transparent 32%),
        #05070a;
    }

    /* NAV */
    nav{
      height:54px;position:fixed;top:0;left:0;right:0;z-index:50;
      display:flex;align-items:center;justify-content:space-between;
      padding:0 42px;background:rgba(5,8,12,.86);
      border-bottom:1px solid rgba(255,255,255,.12);
      backdrop-filter:blur(14px);
    }
    .brand{display:flex;align-items:center;gap:10px;font-family:Orbitron;font-weight:800;letter-spacing:2px}
    .brand-mark{
      width:40px;height:32px;position:relative;
      clip-path:polygon(50% 0,100% 100%,78% 100%,66% 64%,48% 100%,25% 100%);
      background:linear-gradient(135deg,#5865F2,#7b0815);
      filter:drop-shadow(0 0 9px rgba(88,101,242,.6));
    }
    .brand span{font-size:20px}.brand small{font:500 7px Inter;letter-spacing:2px;color:#9da3ad;margin-left:2px}
    .links{display:flex;align-items:center;gap:32px;height:100%}
    .links a{font-size:12px;color:#c7ccd4;position:relative;padding:20px 0}
    .links a.active,.links a:hover{color:#fff}
    .links a.active:after{
      content:"";position:absolute;left:0;right:0;bottom:0;height:2px;background:var(--red);
      box-shadow:0 0 12px var(--red)
    }
    .login{
      border:1px solid var(--red);border-radius:8px;padding:8px 20px!important;
      color:#fff!important;box-shadow:0 0 18px rgba(88,101,242,.14)
    }

    /* HERO */
    .hero{
      min-height:538px;position:relative;display:flex;align-items:center;
      padding:95px 5.5% 45px;overflow:hidden;
      border-bottom:1px solid rgba(255,255,255,.13);
      background:
        radial-gradient(circle at 73% 52%,rgba(88,101,242,.11),transparent 23%),
        linear-gradient(90deg,#05080c 0%,#05080c 43%,rgba(5,8,12,.4) 64%,rgba(5,8,12,.1) 100%);
    }
    .hero:after{
      content:"";position:absolute;inset:0;pointer-events:none;
      background:
        linear-gradient(115deg,transparent 0 26%,rgba(88,101,242,.18) 26.1%,transparent 26.3%),
        linear-gradient(180deg,transparent 72%,rgba(88,101,242,.06),transparent);
      opacity:.8;
    }
    .hero-copy{width:49%;position:relative;z-index:3}
    .eyebrow{
      display:inline-block;color:var(--red);font:700 10px Orbitron;letter-spacing:3px;
      margin-bottom:15px;padding:7px 12px;border-left:2px solid var(--red);
      background:rgba(88,101,242,.06)
    }
    h1{font:900 clamp(58px,7vw,92px)/.9 Orbitron;letter-spacing:3px}
    .tagline{
      margin-top:14px;font:800 clamp(21px,2.2vw,31px) Orbitron;letter-spacing:1px
    }
    .tagline b{color:var(--red)}
    .hero-copy p{max-width:510px;margin:17px 0 23px;color:#aeb5c0;font-size:13px;line-height:1.65}
    .actions{display:flex;gap:16px;flex-wrap:wrap}
    .btn{
      display:inline-flex;align-items:center;gap:12px;padding:13px 22px;border-radius:8px;
      font:700 12px Inter;transition:.25s;border:1px solid var(--red)
    }
    .btn.primary{background:var(--red);box-shadow:0 0 28px rgba(88,101,242,.32)}
    .btn.primary:hover{transform:translateY(-2px);box-shadow:0 0 40px rgba(88,101,242,.5)}
    .btn.ghost{border-color:#68707b;background:rgba(5,8,12,.55)}
    .btn.ghost:hover{border-color:var(--red)}
    .stats{display:flex;gap:48px;margin-top:34px}
    .stat strong{display:block;font:800 21px Orbitron}.stat span{font-size:9px;color:#7e8794}
    .stat strong:before{content:"";display:inline-block;width:3px;height:19px;background:var(--red);margin-right:8px;vertical-align:-3px}

    /* HERO ORB */
    .space{position:absolute;inset:0;z-index:1;overflow:hidden}
    .hero-art{
      position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
      object-position:center right;filter:saturate(.9) contrast(1.08) brightness(.68);
    }
    .hero-art-shade{
      position:absolute;inset:0;
      background:
        linear-gradient(90deg,#05080c 0%,rgba(5,8,12,.98) 28%,rgba(5,8,12,.72) 48%,rgba(5,8,12,.18) 72%,rgba(5,8,12,.08) 100%),
        linear-gradient(0deg,rgba(0,0,0,.58),transparent 40%,rgba(0,0,0,.22));
    }
    .hero-glow{
      position:absolute;right:6%;top:18%;width:42%;height:60%;
      background:radial-gradient(circle,rgba(88,101,242,.16),transparent 68%);
      pointer-events:none;
    }
    .stars{position:absolute;inset:0;background-image:radial-gradient(#fff 1px,transparent 1px);background-size:83px 83px;opacity:.08}

    /* SECTIONS */
    section.content-section{padding:42px 5.5%;position:relative}
    .section-head{display:flex;justify-content:space-between;align-items:end;margin-bottom:22px}
    .kicker{font:700 9px Orbitron;color:var(--red);letter-spacing:3px}
    .section-head h2{font:800 25px/1.2 Orbitron;margin-top:6px}
    .section-head h2 span{color:var(--red)}
    .section-head p{max-width:330px;color:#8f98a5;font-size:11px;line-height:1.5}

    .feature-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
    .card{
      min-height:103px;padding:20px;background:linear-gradient(145deg,rgba(15,19,26,.9),rgba(8,11,16,.85));
      border:1px solid rgba(255,255,255,.12);border-radius:10px;position:relative;overflow:hidden;
      transition:.25s
    }
    .card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--red);opacity:.8}
    .card:hover{transform:translateY(-3px);border-color:rgba(88,101,242,.55);box-shadow:0 12px 35px rgba(0,0,0,.3)}
    .icon{
      width:39px;height:39px;border:1px solid var(--red);border-radius:50%;display:grid;place-items:center;
      color:var(--red);font-size:17px;float:left;margin-right:14px;box-shadow:0 0 18px rgba(88,101,242,.16)
    }
    .card h3{font:700 14px Orbitron;padding-top:2px}.card p{font-size:10px;color:#8f98a5;line-height:1.45;margin-top:7px}

    .why{
      display:grid;grid-template-columns:1fr 1fr;gap:35px;align-items:center;
      min-height:260px;border-top:1px solid rgba(255,255,255,.12);border-bottom:1px solid rgba(255,255,255,.12);
      background:
        radial-gradient(circle at 9% 50%,rgba(88,101,242,.15),transparent 30%),
        linear-gradient(90deg,rgba(88,101,242,.03),transparent 55%);
    }
    .why-art{
      height:260px;position:relative;overflow:hidden;
      background:#08090c;
    }
    .why-art img{
      width:100%;height:100%;display:block;object-fit:cover;object-position:center 38%;
      filter:saturate(.88) contrast(1.08) brightness(.72);
    }
    .why-art:after{
      content:"";position:absolute;inset:0;
      background:linear-gradient(90deg,rgba(5,7,10,.12),rgba(5,7,10,.02) 55%,rgba(5,7,10,.72));
    }
    .why-copy h2{font:800 27px/1.15 Orbitron;margin:6px 0 12px}.why-copy h2 span{color:var(--red)}
    .why-copy p{font-size:12px;line-height:1.65;color:#929aa6;max-width:510px}
    .why-list{display:flex;gap:34px;margin-top:24px}.why-list div{font-size:10px;color:#adb3bd}.why-list b{display:block;color:var(--red);font:700 12px Orbitron;margin-bottom:6px}

    .command-panel{
      margin-top:18px;border:1px solid rgba(88,101,242,.55);border-radius:10px;background:#090d13;overflow:hidden;
      box-shadow:0 0 35px rgba(88,101,242,.1)
    }
    .command-top{padding:11px 15px;border-bottom:1px solid rgba(255,255,255,.1);font:600 10px Orbitron;color:#cbd0d7}
    .cmd{display:flex;gap:18px;padding:11px 15px;border-bottom:1px solid rgba(255,255,255,.06);font-size:10px}
    .cmd:last-child{border-bottom:0}.cmd b{color:var(--red);width:48px}.cmd span{color:#727b88}

    .bot-status-bar {
      display:flex;align-items:center;gap:14px;
      padding:10px 18px;background:rgba(88,101,242,.06);
      border:1px solid rgba(88,101,242,.12);border-radius:6px;
      margin-top:14px;
    }
    .status-dot{width:8px;height:8px;border-radius:50%;background:#43b581;display:inline-block;position:relative}
    .status-dot:before{content:"";position:absolute;inset:-4px;border-radius:50%;background:#43b581;animation:pulse 2s infinite}
    @keyframes pulse{0%{transform:scale(1);opacity:.4}100%{transform:scale(1.8);opacity:0}}
    .status-label{font-size:10px;color:#8f98a5}
    .status-label strong{color:#43b581}

    footer{
      padding:20px 5.5%;display:flex;justify-content:space-between;align-items:center;color:#6e7682;
      font-size:9px;border-top:1px solid rgba(255,255,255,.1)
    }
    footer strong{color:#e8ebef;font-family:Orbitron}
    .footer-btn{border:1px solid var(--red);padding:9px 15px;border-radius:7px;color:#fff}

    @media(max-width:900px){
      nav{padding:0 18px}.links{gap:15px}.links a{font-size:10px}
      .hero{min-height:680px;padding-top:105px;align-items:flex-start}.hero-copy{width:100%;max-width:600px}
      .space{opacity:.78}.hero-art{object-position:68% center}
      .feature-grid{grid-template-columns:repeat(2,1fr)}.why{grid-template-columns:1fr}.why-art{height:220px}
    }
    @media(max-width:620px){
      nav{height:auto;min-height:54px;flex-wrap:wrap;padding:8px 14px}.links{order:3;width:100%;justify-content:center;gap:12px}.links a{padding:8px 0;font-size:9px}.login{padding:6px 11px!important}
      .hero{padding:115px 22px 40px;min-height:650px}.hero-copy{width:100%}h1{font-size:56px}.tagline{font-size:19px}.stats{gap:20px}.stat strong{font-size:16px}
      .feature-grid{grid-template-columns:1fr}.section-head{display:block}.section-head p{margin-top:12px}
      section.content-section{padding:35px 22px}.why-copy{padding-bottom:30px}.why-list{gap:16px;flex-wrap:wrap}
      .hero-art{object-position:66% center}
      footer{flex-direction:column;gap:14px;text-align:center}
    }
  </style>
</head>
<body>
<div class="page">
  <nav>
    <a class="brand" href="/"><i class="brand-mark"></i><span>EDITH</span><small>THE DISCORD BOT</small></a>
    <div class="links">
      <a class="active" href="#home">Home</a>
      <a href="#commands">Commands</a>
      <a href="#features">Features</a>
      <a href="#about">About</a>
      <a href="#support">Support</a>
      <a class="login" href="#cta">✦ &nbsp;Login</a>
    </div>
  </nav>

  <main id="home">
    <section class="hero">
      <div class="space">
        <img class="hero-art" src="https://images.unsplash.com/photo-1541701494587-cb58502866ab?w=1200&h=600&fit=crop" alt="Deep space visualization">
        <div class="hero-art-shade"></div>
        <div class="hero-glow"></div>
        <div class="stars"></div>
      </div>
      <div class="hero-copy">
        <div class="eyebrow">✦ THE ALL-IN-ONE DISCORD BOT</div>
        <h1>EDITH</h1>
        <div class="tagline">MORE THAN <b>JUST A BOT</b></div>
        <p>{{ description }}</p>
        <div class="actions">
          <a class="btn primary" href="#cta">◉ &nbsp; Add to Discord &nbsp; →</a>
          <a class="btn ghost" href="#commands">›_ &nbsp; View Commands</a>
        </div>
        
        <div class="bot-status-bar">
          <span class="status-dot"></span>
          <span class="status-label"><strong>{{ status }}</strong> · {{ uptime }} uptime · {{ latency }}ms latency</span>
        </div>

        <div class="stats">
          <div class="stat"><strong>{{ total_commands }}+</strong><span>Commands</span></div>
          <div class="stat"><strong>24/7</strong><span>Uptime</span></div>
          <div class="stat"><strong>{{ total_servers }}+</strong><span>Servers</span></div>
          <div class="stat"><strong>{{ total_users }}+</strong><span>Users</span></div>
        </div>
      </div>
    </section>

    <section class="content-section" id="features">
      <div class="section-head">
        <div><div class="kicker">FEATURES</div><h2>EVERYTHING YOU NEED<br><span>IN ONE BOT</span></h2></div>
        <p>From moderation to fun, from utility to security — EDITH has it all. Built for communities, by people who care.</p>
      </div>
      <div class="feature-grid">
        {% for feature in features %}
        <article class="card">
          <div class="icon">{{ feature.icon }}</div>
          <h3>{{ feature.title }}</h3>
          <p>{{ feature.desc }}</p>
        </article>
        {% endfor %}
      </div>
    </section>

    <section class="why" id="about">
      <div class="why-art">
        <img src="https://images.unsplash.com/photo-1614730321146-b6fa6a46bcb4?w=600&h=300&fit=crop" alt="Red moon landscape">
      </div>
      <div class="why-copy">
        <div class="kicker">WHY CHOOSE EDITH</div>
        <h2>BUILT FOR<br>THE <span>NEXT GENERATION</span></h2>
        <p>Whether you're running a small community or a massive server, EDITH adapts to your needs. Simple to set up, easy to use, and packed with features that actually matter.</p>
        <div class="why-list">
          <div><b>↗</b>Simple Setup</div>
          <div><b>⟳</b>Regular Updates</div>
          <div><b>◉</b>Active Support</div>
          <div><b>✦</b>24/7 Uptime</div>
        </div>
      </div>
      <div class="command-panel" id="commands">
        <div class="command-top">›_ &nbsp; /help &nbsp;&nbsp; •••</div>
        {% for cmd in commands[:6] %}
        <div class="cmd"><b>{{ cmd.name }}</b><span>{{ cmd.desc }}</span></div>
        {% endfor %}
      </div>
    </section>
  </main>

  <footer id="support">
    <div><strong>EDITH</strong> &nbsp; THE DISCORD BOT</div>
    <div>Created with <span style="color:#5865F2">♥</span> by Udit Singh Dhakrey</div>
    <a class="footer-btn" href="#">◉ &nbsp; Support Server</a>
  </footer>
</div>
</body>
</html>
"""

# ============ ROUTES ============
@app.route('/')
def home():
    """Main landing page with real-time data"""
    stats = get_server_stats()
    commands = get_commands_list()
    features = get_features()
    
    # Get bot status from environment or Firebase
    bot_status = os.getenv('BOT_STATUS', 'Online')
    bot_latency = os.getenv('BOT_LATENCY', '0')
    
    return render_template_string(
        HTML_TEMPLATE,
        description="EDITH is a powerful, feature-rich Discord bot designed to make your server smarter, safer and more fun. With a clean GUI, lightning-fast performance and endless possibilities — EDITH is your all-in-one solution.",
        total_commands=stats.get('total_commands', 27),
        total_servers=stats.get('total_servers', 10000),
        total_users=stats.get('total_users', 50000),
        uptime=stats.get('uptime', '99.9%'),
        status=bot_status,
        latency=bot_latency,
        commands=commands,
        features=features
    )

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
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #05070a; color: white; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }}
                .container {{ background: #0c1016; padding: 50px; border-radius: 20px; text-align: center; max-width: 500px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,0.5); border: 1px solid rgba(88,101,242,0.3); }}
                .success {{ color: #5865F2; font-size: 80px; margin-bottom: 20px; }}
                h1 {{ color: #ffffff; font-size: 28px; margin-bottom: 10px; font-family: Orbitron; }}
                .subtitle {{ color: #b5b5c4; font-size: 16px; margin-bottom: 30px; }}
                .user-info {{ background: #07090d; border-radius: 12px; padding: 20px; margin: 20px 0; text-align: left; border: 1px solid rgba(255,255,255,0.06); }}
                .user-info .row {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }}
                .user-info .row:last-child {{ border-bottom: none; }}
                .user-info .label {{ color: #6d6d8a; font-size: 13px; }}
                .user-info .value {{ color: #ffffff; font-size: 14px; }}
                .status-box {{ padding: 15px; border-radius: 10px; margin: 15px 0; background: rgba(88,101,242,0.1); color: #5865F2; font-weight: 600; border: 1px solid rgba(88,101,242,0.2); }}
                .button {{ background: #5865f2; color: white; border: none; padding: 16px 40px; font-size: 18px; font-weight: 600; border-radius: 10px; cursor: pointer; width: 100%; margin-top: 20px; text-decoration: none; display: inline-block; font-family: Inter; }}
                .button:hover {{ background: #4752c4; transform: translateY(-2px); box-shadow: 0 10px 30px rgba(88,101,242,0.3); }}
                .footer {{ margin-top: 25px; color: #4d4d6a; font-size: 12px; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 20px; }}
                .badge {{ display: inline-block; background: #5865F2; color: white; padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
                .avatar {{ width: 80px; height: 80px; border-radius: 50%; margin: 10px auto; display: block; border: 3px solid #5865f2; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="success">✓</div>
                <h1>Verification Successful!</h1>
                <p class="subtitle">Welcome to the server! 🚀</p>
                
                <img src="{avatar_url}" class="avatar" onerror="this.style.display='none'">
                
                <div class="user-info">
                    <div class="row">
                        <span class="label">Username</span>
                        <span class="value">{username}</span>
                    </div>
                    <div class="row">
                        <span class="label">User ID</span>
                        <span class="value">{discord_id}</span>
                    </div>
                    <div class="row">
                        <span class="label">Email</span>
                        <span class="value">{email}</span>
                    </div>
                    <div class="row">
                        <span class="label">Status</span>
                        <span class="value"><span class="badge">Verified</span></span>
                    </div>
                </div>
                
                <div class="status-box">✓ You now have full access to the server!</div>
                
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
    """Health check endpoint for monitoring"""
    stats = get_server_stats()
    return jsonify({
        'status': 'online',
        'firebase': '✅ Connected' if rtdb_client else '❌ Not connected',
        'timestamp': datetime.now().isoformat(),
        'stats': stats
    })

@app.route('/api/stats')
def api_stats():
    """API endpoint for real-time statistics"""
    stats = get_server_stats()
    return jsonify(stats)

@app.route('/api/commands')
def api_commands():
    """API endpoint for commands list"""
    commands = get_commands_list()
    return jsonify({'commands': commands, 'count': len(commands)})

@app.route('/api/features')
def api_features():
    """API endpoint for features list"""
    features = get_features()
    return jsonify({'features': features, 'count': len(features)})

@app.route('/api/users')
def api_users():
    """API endpoint for users data"""
    if not rtdb_client:
        return jsonify({'error': 'Firebase not connected'}), 500
    
    try:
        users = rtdb_client.child('all_users').get()
        if users:
            return jsonify({'users': users, 'count': len(users)})
        return jsonify({'users': {}, 'count': 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/guilds')
def api_guilds():
    """API endpoint for guilds data"""
    if not rtdb_client:
        return jsonify({'error': 'Firebase not connected'}), 500
    
    try:
        guilds = rtdb_client.child('guilds').get()
        if guilds:
            return jsonify({'guilds': guilds, 'count': len(guilds)})
        return jsonify({'guilds': {}, 'count': 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============ VERCEL ============
app.debug = False

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
