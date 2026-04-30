"""
Bot Candidature — Serveur Web Multi-Utilisateurs
Démarre avec : python app.py
Puis ouvre : http://localhost:5000
"""

import os, json, sqlite3, datetime, re, hashlib, secrets, uuid
from pathlib import Path
from flask import (Flask, render_template_string, request, redirect,
                   url_for, session, jsonify, send_from_directory)
from werkzeug.utils import secure_filename

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DB_PATH     = BASE_DIR / "database.db"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
(BASE_DIR / "lettres").mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024   # 10 MB max CV

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Base de données ───────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        email       TEXT UNIQUE NOT NULL,
        password    TEXT NOT NULL,
        name        TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS profiles (
        user_id         INTEGER PRIMARY KEY REFERENCES users(id),
        postes          TEXT DEFAULT '[]',
        villes          TEXT DEFAULT '["Paris"]',
        remote          INTEGER DEFAULT 1,
        salaire_min     INTEGER DEFAULT 35000,
        contrats        TEXT DEFAULT '["CDI"]',
        mots_exclus     TEXT DEFAULT '[]',
        score_min       INTEGER DEFAULT 60,
        auto_postuler   INTEGER DEFAULT 0,
        gen_lettre      INTEGER DEFAULT 1,
        cv_filename     TEXT DEFAULT '',
        cv_text         TEXT DEFAULT '',
        updated_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS candidatures (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        job_id      TEXT,
        poste       TEXT,
        entreprise  TEXT,
        localisation TEXT,
        salaire     TEXT,
        plateforme  TEXT DEFAULT 'indeed',
        lien        TEXT,
        score_cv    INTEGER DEFAULT 0,
        lettre      TEXT DEFAULT '',
        statut      TEXT DEFAULT 'À postuler',
        created_at  TEXT DEFAULT (datetime('now')),
        UNIQUE(user_id, job_id)
    );
    """)
    db.commit()
    db.close()

# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_password(pwd): return hashlib.sha256(pwd.encode()).hexdigest()

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    db.close()
    return u

def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# ── CV parsing ────────────────────────────────────────────────────────────────
def extract_pdf_text(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return ""

# ── Score CV/Offre ────────────────────────────────────────────────────────────
def score_cv_job(cv_text: str, title: str, description: str) -> int:
    if not cv_text:
        return 50
    def tok(t): return set(re.findall(r'\b[a-zA-ZÀ-ÿ]{3,}\b', t.lower()))
    cv = tok(cv_text)
    job = tok(f"{title} {description}")
    if not job:
        return 50
    return min(100, int(len(cv & job) / max(len(job), 1) * 200))

# ── Cover letter ──────────────────────────────────────────────────────────────
def generate_letter(cv_text: str, job: dict, user_name: str) -> str:
    if not ANTHROPIC_KEY:
        return ""
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = c.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": f"""
Rédige une lettre de motivation professionnelle et percutante.

Candidat : {user_name}
Poste visé : {job.get('title','')} chez {job.get('company','')}
Localisation : {job.get('location','')}

Description du poste :
{str(job.get('description',''))[:2000]}

CV du candidat :
{cv_text[:2500]}

Consignes : lettre en français, 3 paragraphes max, ton professionnel, personnalisée, sans formule générique.
"""}]
        )
        return msg.content[0].text
    except Exception as e:
        return ""

# ════════════════════════════════════════════════════════════════════════════════
#  TEMPLATES HTML
# ════════════════════════════════════════════════════════════════════════════════

LAYOUT = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}JobBot{% endblock %} — Bot Candidature</title>
<style>
:root{--bg:#0f1117;--s:#1a1d27;--s2:#222535;--acc:#6c63ff;--gr:#00d4aa;--rd:#ff5f6d;--yw:#ffc107;--tx:#e8eaf0;--mt:#8b90a7;--bd:#2e3249;--r:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--tx);min-height:100vh}
a{color:var(--acc);text-decoration:none}
input,select,textarea{background:var(--s2);border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:10px 14px;font-size:.9rem;width:100%;outline:none;transition:border-color .2s;font-family:inherit}
input:focus,select:focus,textarea:focus{border-color:var(--acc)}
.btn{padding:11px 22px;border-radius:9px;border:none;font-size:.9rem;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;display:inline-flex;align-items:center;gap:8px}
.btn:hover{opacity:.85}.btn:active{transform:scale(.97)}
.btn-pr{background:var(--acc);color:#fff}
.btn-gr{background:var(--gr);color:#0f1117}
.btn-sm{padding:7px 14px;font-size:.8rem;border-radius:7px}
.btn-sec{background:var(--s2);color:var(--tx);border:1px solid var(--bd)}
.card{background:var(--s);border:1px solid var(--bd);border-radius:var(--r);padding:24px}
.pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.72rem;font-weight:600}
.pill-gr{background:#00d4aa22;color:var(--gr);border:1px solid var(--gr)}
.pill-acc{background:#6c63ff22;color:var(--acc);border:1px solid var(--acc)}
.pill-rd{background:#ff5f6d22;color:var(--rd);border:1px solid var(--rd)}
.pill-yw{background:#ffc10722;color:var(--yw);border:1px solid var(--yw)}
.pill-mt{background:#fff1;color:var(--mt);border:1px solid var(--bd)}
nav{background:var(--s);border-bottom:1px solid var(--bd);padding:0 32px;display:flex;align-items:center;height:60px;gap:24px}
nav .logo{font-weight:800;font-size:1.1rem;color:var(--tx)}
nav .logo span{color:var(--acc)}
nav .spacer{flex:1}
nav a{color:var(--mt);font-size:.88rem;font-weight:500}
nav a:hover{color:var(--tx)}
.page{max-width:1100px;margin:0 auto;padding:32px 24px}
.flash{padding:12px 18px;border-radius:8px;margin-bottom:18px;font-size:.88rem}
.flash.err{background:#ff5f6d22;border:1px solid var(--rd);color:var(--rd)}
.flash.ok{background:#00d4aa22;border:1px solid var(--gr);color:var(--gr)}
label{font-size:.82rem;color:var(--mt);font-weight:600;display:block;margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em}
.form-group{margin-bottom:18px}
h2{font-size:1.4rem;font-weight:800;margin-bottom:4px}
h3{font-size:1rem;font-weight:700;margin-bottom:12px}
p.sub{color:var(--mt);font-size:.88rem;margin-bottom:24px}
table{width:100%;border-collapse:collapse}
thead th{background:var(--s2);padding:10px 14px;text-align:left;font-size:.72rem;text-transform:uppercase;color:var(--mt);letter-spacing:.05em;border-bottom:1px solid var(--bd)}
tbody tr{border-bottom:1px solid var(--bd);transition:background .15s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:var(--s2)}
tbody td{padding:11px 14px;font-size:.85rem;vertical-align:middle}
</style>
{% block head %}{% endblock %}
</head>
<body>
<nav>
  <span class="logo">🤖 Job<span>Bot</span></span>
  {% if user %}
  <a href="/dashboard">Dashboard</a>
  <a href="/profile">Mon profil</a>
  <span class="spacer"></span>
  <span style="color:var(--mt);font-size:.85rem">👤 {{ user.name }}</span>
  <a href="/logout" style="margin-left:12px">Déconnexion</a>
  {% else %}
  <span class="spacer"></span>
  <a href="/login">Connexion</a>
  <a href="/register" style="margin-left:12px;background:var(--acc);color:#fff;padding:7px 16px;border-radius:8px;font-weight:600">S'inscrire</a>
  {% endif %}
</nav>
{% block body %}{% endblock %}
</body>
</html>"""

LANDING = LAYOUT.replace("{% block body %}{% endblock %}", """
<div style="text-align:center;padding:80px 24px 60px">
  <div style="font-size:3.5rem;margin-bottom:16px">🤖</div>
  <h1 style="font-size:2.6rem;font-weight:900;margin-bottom:12px">Postule partout.<br><span style="color:var(--acc)">Automatiquement.</span></h1>
  <p style="color:var(--mt);font-size:1.1rem;max-width:520px;margin:0 auto 40px">Upload ton CV, définis tes critères, et le bot trouve les offres qui matchent et génère une lettre personnalisée pour chacune.</p>
  <div style="display:flex;gap:14px;justify-content:center;flex-wrap:wrap">
    <a href="/register" class="btn btn-pr" style="font-size:1rem;padding:14px 32px">🚀 Commencer gratuitement</a>
    <a href="/login" class="btn btn-sec" style="font-size:1rem;padding:14px 32px">Se connecter</a>
  </div>
</div>
<div style="max-width:900px;margin:0 auto 80px;padding:0 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px">
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;margin-bottom:12px">📄</div>
    <h3>Upload ton CV</h3>
    <p style="color:var(--mt);font-size:.87rem">Le bot lit ton CV et score automatiquement chaque offre selon ta cohérence de profil.</p>
  </div>
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;margin-bottom:12px">🎯</div>
    <h3>Définis tes critères</h3>
    <p style="color:var(--mt);font-size:.87rem">Poste, salaire, localisation, contrat — le bot ne te montre que les offres qui matchent vraiment.</p>
  </div>
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;margin-bottom:12px">✉️</div>
    <h3>Lettres personnalisées</h3>
    <p style="color:var(--mt);font-size:.87rem">Une lettre de motivation unique générée par IA pour chaque offre, basée sur ton profil.</p>
  </div>
  <div class="card" style="text-align:center">
    <div style="font-size:2rem;margin-bottom:12px">📊</div>
    <h3>Suivi en temps réel</h3>
    <p style="color:var(--mt);font-size:.87rem">Dashboard personnel : toutes tes candidatures, leur statut et tes lettres au même endroit.</p>
  </div>
</div>
""")

REGISTER_HTML = """
<div class="page" style="max-width:420px">
  <div class="card" style="margin-top:40px">
    <h2>Créer un compte</h2>
    <p class="sub">Rejoins JobBot et commence à postuler automatiquement.</p>
    {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
    <form method="POST">
      <div class="form-group"><label>Prénom & Nom</label><input name="name" placeholder="Marie Dupont" required></div>
      <div class="form-group"><label>Email</label><input type="email" name="email" placeholder="marie@email.com" required></div>
      <div class="form-group"><label>Mot de passe</label><input type="password" name="password" placeholder="••••••••" required></div>
      <button class="btn btn-pr" style="width:100%;justify-content:center;margin-top:8px">Créer mon compte →</button>
    </form>
    <p style="text-align:center;margin-top:18px;color:var(--mt);font-size:.85rem">Déjà un compte ? <a href="/login">Se connecter</a></p>
  </div>
</div>
"""

LOGIN_HTML = """
<div class="page" style="max-width:420px">
  <div class="card" style="margin-top:40px">
    <h2>Connexion</h2>
    <p class="sub">Content de te revoir 👋</p>
    {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
    <form method="POST">
      <div class="form-group"><label>Email</label><input type="email" name="email" required></div>
      <div class="form-group"><label>Mot de passe</label><input type="password" name="password" required></div>
      <button class="btn btn-pr" style="width:100%;justify-content:center;margin-top:8px">Se connecter →</button>
    </form>
    <p style="text-align:center;margin-top:18px;color:var(--mt);font-size:.85rem">Pas de compte ? <a href="/register">S'inscrire</a></p>
  </div>
</div>
"""

PROFILE_HTML = """
<div class="page" style="max-width:820px">
  <h2>⚙️ Mon profil & critères</h2>
  <p class="sub">Configure tes préférences. Le bot les utilisera à chaque recherche.</p>
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="flash err">{{ err }}</div>{% endif %}

  <form method="POST" enctype="multipart/form-data">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">

    <div class="card">
      <h3>📄 Mon CV</h3>
      {% if profile.cv_filename %}
      <div style="background:var(--s2);border:1px solid var(--gr);border-radius:8px;padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;gap:10px">
        <span style="font-size:1.4rem">✅</span>
        <div>
          <div style="font-weight:600;font-size:.88rem">{{ profile.cv_filename }}</div>
          <div style="color:var(--mt);font-size:.78rem">CV chargé — le scoring est actif</div>
        </div>
      </div>
      {% endif %}
      <label>{{ "Remplacer le CV" if profile.cv_filename else "Uploader mon CV" }} (PDF)</label>
      <input type="file" name="cv" accept=".pdf,.docx" style="padding:8px">
    </div>

    <div class="card">
      <h3>🎯 Postes visés</h3>
      <label>Intitulés (séparés par des virgules)</label>
      <textarea name="postes" rows="3" placeholder="Chef de projet, Product Manager, Business Analyst">{{ profile.postes | replace('[','') | replace(']','') | replace('"','') }}</textarea>
    </div>

    <div class="card">
      <h3>📍 Localisation</h3>
      <div class="form-group">
        <label>Villes (séparées par des virgules)</label>
        <input name="villes" value="{{ profile.villes | replace('[','') | replace(']','') | replace('"','') }}" placeholder="Paris, Lyon">
      </div>
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
        <input type="checkbox" name="remote" {{ 'checked' if profile.remote }} style="width:auto;accent-color:var(--acc)">
        <span>Accepter le remote / hybride</span>
      </label>
    </div>

    <div class="card">
      <h3>💰 Salaire & contrat</h3>
      <div class="form-group">
        <label>Salaire minimum brut annuel (€)</label>
        <input type="number" name="salaire_min" value="{{ profile.salaire_min }}" step="1000">
      </div>
      <label>Types de contrat (Ctrl+clic pour multi-select)</label>
      <select name="contrats" multiple style="height:90px">
        {% for c in ['CDI','CDD','freelance','stage','alternance','intérim'] %}
        <option {{ 'selected' if c in profile.contrats }}>{{ c }}</option>
        {% endfor %}
      </select>
    </div>

    <div class="card">
      <h3>🚫 Mots-clés à exclure</h3>
      <label>Le bot ignorera les offres contenant ces mots</label>
      <textarea name="mots_exclus" rows="2" placeholder="stage, bénévolat, alternance…">{{ profile.mots_exclus | replace('[','') | replace(']','') | replace('"','') }}</textarea>
    </div>

    <div class="card">
      <h3>🤖 Automatisation</h3>
      <div class="form-group">
        <label>Score CV minimum pour retenir une offre : <strong id="score-lbl">{{ profile.score_min }}%</strong></label>
        <input type="range" name="score_min" min="0" max="100" value="{{ profile.score_min }}"
               oninput="document.getElementById('score-lbl').textContent=this.value+'%'"
               style="background:none;border:none;padding:0;margin-top:8px">
      </div>
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:12px">
        <input type="checkbox" name="gen_lettre" {{ 'checked' if profile.gen_lettre }} style="width:auto;accent-color:var(--acc)">
        <span>Générer une lettre par offre (IA)</span>
      </label>
    </div>

  </div>
  <div style="margin-top:20px">
    <button class="btn btn-pr" type="submit">💾 Sauvegarder le profil</button>
  </div>
  </form>
</div>
"""

DASHBOARD_HTML = """
<div class="page">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px">
    <div>
      <h2>Bonjour {{ user.name }} 👋</h2>
      <p class="sub" style="margin:0">Voici tes candidatures et recommandations du jour.</p>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <form method="POST" action="/search">
        <button class="btn btn-pr">🔍 Lancer une recherche</button>
      </form>
      <a href="/profile" class="btn btn-sec">⚙️ Mes critères</a>
    </div>
  </div>

  <!-- Stats -->
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:28px">
    {% for label, val, color in [
        ('Total', stats.total, 'var(--acc)'),
        ('À postuler', stats.a_pos, 'var(--mt)'),
        ('Envoyées', stats.envoye, 'var(--gr)'),
        ('En attente', stats.attente, 'var(--yw)'),
        ('Refus', stats.refus, 'var(--rd)')
    ] %}
    <div class="card" style="text-align:center;padding:18px">
      <div style="font-size:2rem;font-weight:800;color:{{ color }}">{{ val }}</div>
      <div style="font-size:.75rem;color:var(--mt);text-transform:uppercase;letter-spacing:.05em;margin-top:4px">{{ label }}</div>
    </div>
    {% endfor %}
  </div>

  {% if not profile.cv_filename %}
  <div class="card" style="border-color:var(--yw);margin-bottom:24px;display:flex;align-items:center;gap:16px">
    <span style="font-size:2rem">⚠️</span>
    <div>
      <div style="font-weight:700">CV manquant</div>
      <div style="color:var(--mt);font-size:.87rem">Upload ton CV pour que le bot puisse scorer les offres et générer des lettres. <a href="/profile">Configurer →</a></div>
    </div>
  </div>
  {% endif %}

  <!-- Candidatures -->
  <h3 style="margin-bottom:14px">📋 Mes candidatures</h3>
  {% if candidatures %}
  <div class="card" style="padding:0;overflow:hidden">
    <table>
      <thead><tr>
        <th>Date</th><th>Poste</th><th>Entreprise</th><th>Localisation</th>
        <th>Score CV</th><th>Lettre IA</th><th>Statut</th><th>Lien</th>
      </tr></thead>
      <tbody>
      {% for c in candidatures %}
      <tr>
        <td style="color:var(--mt);font-size:.78rem">{{ c.created_at[:10] }}</td>
        <td><strong>{{ c.poste }}</strong></td>
        <td>{{ c.entreprise }}</td>
        <td>{{ c.localisation or '—' }}</td>
        <td>
          <div style="display:flex;align-items:center;gap:6px">
            <div style="width:60px;height:5px;background:var(--bd);border-radius:3px;overflow:hidden">
              <div style="height:100%;width:{{ c.score_cv }}%;background:{% if c.score_cv>=75 %}var(--gr){% elif c.score_cv>=50 %}var(--acc){% else %}var(--rd){% endif %};border-radius:3px"></div>
            </div>
            <span style="font-size:.78rem">{{ c.score_cv }}%</span>
          </div>
        </td>
        <td>{% if c.lettre %}<span class="pill pill-gr">✓ Oui</span>{% else %}<span class="pill pill-mt">Non</span>{% endif %}</td>
        <td>
          <form method="POST" action="/candidature/{{ c.id }}/statut">
            <select name="statut" onchange="this.form.submit()" style="padding:5px 8px;font-size:.78rem;width:120px">
              {% for s in ['À postuler','Envoyé','En attente','Relance','Refus'] %}
              <option {{ 'selected' if c.statut==s }}>{{ s }}</option>
              {% endfor %}
            </select>
          </form>
        </td>
        <td>
          {% if c.lien %}
          <a href="{{ c.lien }}" target="_blank" style="font-size:.8rem;color:var(--acc)">Voir →</a>
          {% endif %}
          {% if c.lettre %}
          &nbsp;<a href="/lettre/{{ c.id }}" style="font-size:.8rem;color:var(--mt)">Lettre</a>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="card" style="text-align:center;padding:50px">
    <div style="font-size:2.5rem;margin-bottom:12px">🔍</div>
    <p style="color:var(--mt)">Aucune candidature pour l'instant.<br>Lance une recherche pour trouver des offres qui matchent ton profil !</p>
  </div>
  {% endif %}
</div>
"""

RESULTS_HTML = """
<div class="page">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:24px">
    <a href="/dashboard" style="color:var(--mt);font-size:.85rem">← Retour</a>
    <h2>🎯 Offres recommandées</h2>
  </div>
  <p class="sub">{{ jobs|length }} offres trouvées et filtrées selon ton profil.</p>

  {% if not jobs %}
  <div class="card" style="text-align:center;padding:50px">
    <div style="font-size:2rem;margin-bottom:12px">😶</div>
    <p style="color:var(--mt)">Aucune offre trouvée avec ces critères.<br>Essaie d'élargir ta recherche dans <a href="/profile">tes critères</a>.</p>
  </div>
  {% endif %}

  <div style="display:grid;gap:16px">
  {% for job in jobs %}
  <div class="card" style="display:flex;flex-direction:column;gap:14px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px">
      <div>
        <div style="font-size:1.05rem;font-weight:700;margin-bottom:4px">{{ job.title }}</div>
        <div style="color:var(--mt);font-size:.88rem">{{ job.company }} — {{ job.location }}</div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        {% if job.salary %}<span class="pill pill-gr">💰 {{ job.salary }}</span>{% endif %}
        <span class="pill {% if job.score>=75 %}pill-gr{% elif job.score>=50 %}pill-acc{% else %}pill-rd{% endif %}">
          Score CV : {{ job.score }}%
        </span>
      </div>
    </div>

    <p style="color:var(--mt);font-size:.85rem;line-height:1.55">{{ job.snippet }}</p>

    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <a href="{{ job.url }}" target="_blank" class="btn btn-sec btn-sm">🔗 Voir l'offre</a>
      <form method="POST" action="/postuler" style="display:inline">
        <input type="hidden" name="job_id"    value="{{ job.job_id }}">
        <input type="hidden" name="title"     value="{{ job.title }}">
        <input type="hidden" name="company"   value="{{ job.company }}">
        <input type="hidden" name="location"  value="{{ job.location }}">
        <input type="hidden" name="salary"    value="{{ job.salary }}">
        <input type="hidden" name="url"       value="{{ job.url }}">
        <input type="hidden" name="snippet"   value="{{ job.snippet }}">
        <input type="hidden" name="score"     value="{{ job.score }}">
        <button class="btn btn-gr btn-sm" type="submit">✉️ Générer lettre & postuler</button>
      </form>
    </div>
  </div>
  {% endfor %}
  </div>

  {% if jobs %}
  <div style="margin-top:24px;text-align:center">
    <form method="POST" action="/postuler-tout">
      {% for job in jobs %}
      <input type="hidden" name="jobs" value="{{ job|tojson|e }}">
      {% endfor %}
      <button class="btn btn-pr" style="font-size:1rem;padding:14px 36px">🚀 Postuler à toutes ({{ jobs|length }})</button>
    </form>
  </div>
  {% endif %}
</div>
"""

LETTRE_HTML = """
<div class="page" style="max-width:700px">
  <a href="/dashboard" style="color:var(--mt);font-size:.85rem">← Retour au dashboard</a>
  <div class="card" style="margin-top:18px">
    <h2 style="margin-bottom:4px">✉️ Lettre de motivation</h2>
    <p class="sub">{{ candidature.poste }} — {{ candidature.entreprise }}</p>
    <div style="background:var(--s2);border-radius:8px;padding:20px;white-space:pre-wrap;font-size:.9rem;line-height:1.65;border:1px solid var(--bd)">{{ candidature.lettre }}</div>
    <div style="margin-top:16px;display:flex;gap:10px">
      <button onclick="navigator.clipboard.writeText(document.querySelector('div[style*=pre-wrap]').textContent);alert('Copié !')" class="btn btn-sec btn-sm">📋 Copier</button>
      <a href="{{ candidature.lien }}" target="_blank" class="btn btn-pr btn-sm">🔗 Aller postuler</a>
    </div>
  </div>
</div>
"""

# ════════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════════

def render(template, **kw):
    u = current_user()
    return render_template_string(template, user=u, **kw)

@app.route("/")
def index():
    return render(LANDING)

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        name     = request.form.get("name","").strip()
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        if not all([name, email, password]):
            return render(LAYOUT.replace("{% block body %}{% endblock %}", REGISTER_HTML), error="Tous les champs sont requis.")
        db = get_db()
        try:
            db.execute("INSERT INTO users (name,email,password) VALUES (?,?,?)",
                       (name, email, hash_password(password)))
            db.commit()
            user_id = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
            db.execute("INSERT INTO profiles (user_id) VALUES (?)", (user_id,))
            db.commit()
            session["user_id"] = user_id
            return redirect(url_for("profile"))
        except sqlite3.IntegrityError:
            return render(LAYOUT.replace("{% block body %}{% endblock %}", REGISTER_HTML), error="Cet email est déjà utilisé.")
        finally:
            db.close()
    return render(LAYOUT.replace("{% block body %}{% endblock %}", REGISTER_HTML))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        db = get_db()
        u = db.execute("SELECT * FROM users WHERE email=? AND password=?",
                       (email, hash_password(password))).fetchone()
        db.close()
        if u:
            session["user_id"] = u["id"]
            return redirect(url_for("dashboard"))
        return render(LAYOUT.replace("{% block body %}{% endblock %}", LOGIN_HTML), error="Email ou mot de passe incorrect.")
    return render(LAYOUT.replace("{% block body %}{% endblock %}", LOGIN_HTML))

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ── Profil ────────────────────────────────────────────────────────────────────
@app.route("/profile", methods=["GET","POST"])
@login_required
def profile():
    user = current_user()
    db   = get_db()
    prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()

    if request.method == "POST":
        postes      = json.dumps([x.strip() for x in request.form.get("postes","").split(",") if x.strip()])
        villes      = json.dumps([x.strip() for x in request.form.get("villes","").split(",") if x.strip()])
        remote      = 1 if request.form.get("remote") else 0
        salaire_min = int(request.form.get("salaire_min") or 35000)
        contrats    = json.dumps(request.form.getlist("contrats"))
        mots_exclus = json.dumps([x.strip() for x in request.form.get("mots_exclus","").split(",") if x.strip()])
        score_min   = int(request.form.get("score_min") or 60)
        gen_lettre  = 1 if request.form.get("gen_lettre") else 0

        cv_filename = prof["cv_filename"] if prof else ""
        cv_text     = prof["cv_text"] if prof else ""

        # Upload CV
        f = request.files.get("cv")
        if f and f.filename:
            fn = secure_filename(f.filename)
            cv_path = UPLOADS_DIR / f"{user['id']}_{fn}"
            f.save(cv_path)
            cv_filename = fn
            cv_text = extract_pdf_text(cv_path)

        db.execute("""INSERT INTO profiles
            (user_id,postes,villes,remote,salaire_min,contrats,mots_exclus,score_min,gen_lettre,cv_filename,cv_text,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
            postes=excluded.postes, villes=excluded.villes, remote=excluded.remote,
            salaire_min=excluded.salaire_min, contrats=excluded.contrats,
            mots_exclus=excluded.mots_exclus, score_min=excluded.score_min,
            gen_lettre=excluded.gen_lettre, cv_filename=excluded.cv_filename,
            cv_text=excluded.cv_text, updated_at=excluded.updated_at""",
            (user["id"],postes,villes,remote,salaire_min,contrats,mots_exclus,score_min,gen_lettre,cv_filename,cv_text))
        db.commit()
        prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
        db.close()
        return render(LAYOUT.replace("{% block body %}{% endblock %}", PROFILE_HTML), profile=prof, msg="✅ Profil sauvegardé !")

    db.close()
    return render(LAYOUT.replace("{% block body %}{% endblock %}", PROFILE_HTML), profile=prof or {}, err=None, msg=None)

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    db   = get_db()
    prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    cands = db.execute("SELECT * FROM candidatures WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                       (user["id"],)).fetchall()
    db.close()

    def count(s): return sum(1 for c in cands if c["statut"]==s)
    stats = type("S",(),{
        "total" : len(cands),
        "a_pos" : count("À postuler"),
        "envoye": count("Envoyé"),
        "attente": count("En attente"),
        "refus" : count("Refus")
    })()
    return render(LAYOUT.replace("{% block body %}{% endblock %}", DASHBOARD_HTML),
                  profile=prof or {}, candidatures=cands, stats=stats)

# ── Recherche d'offres ────────────────────────────────────────────────────────
@app.route("/search", methods=["POST"])
@login_required
def search():
    user = current_user()
    db   = get_db()
    prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    db.close()

    if not prof:
        return redirect(url_for("profile"))

    postes   = json.loads(prof["postes"] or "[]")
    villes   = json.loads(prof["villes"] or '["Paris"]')
    remote   = prof["remote"]
    exclus   = json.loads(prof["mots_exclus"] or "[]")
    score_min = prof["score_min"]
    cv_text  = prof["cv_text"] or ""

    if not postes:
        return redirect(url_for("profile"))

    locations = list(villes)
    if remote:
        locations.append("remote")

    # Simulation de résultats (remplace par un vrai appel API Indeed)
    # En mode assistant Claude, cette route reçoit les résultats via /api/add-jobs
    sample_jobs = _get_sample_jobs(postes, locations)

    # Filtrage & scoring
    filtered = []
    for job in sample_jobs:
        full = (job.get("title","") + " " + job.get("snippet","")).lower()
        skip = False
        for kw in exclus:
            if kw.lower() in full:
                skip = True
                break
        if skip:
            continue
        score = score_cv_job(cv_text, job.get("title",""), job.get("snippet",""))
        if score < score_min:
            continue
        job["score"] = score
        filtered.append(job)

    filtered.sort(key=lambda j: j["score"], reverse=True)
    return render(LAYOUT.replace("{% block body %}{% endblock %}", RESULTS_HTML), jobs=filtered)

def _get_sample_jobs(postes, locations):
    """Données de démonstration — remplacé par appel API réel."""
    samples = []
    for poste in postes[:2]:
        for loc in locations[:2]:
            samples.append({
                "job_id"  : str(uuid.uuid4())[:8],
                "title"   : poste,
                "company" : "Entreprise Exemple",
                "location": loc,
                "salary"  : "45 000 – 55 000 € / an",
                "snippet" : f"Nous recherchons un(e) {poste} expérimenté(e) pour rejoindre notre équipe à {loc}. Vous serez responsable de la gestion de projets digitaux, coordination des équipes et suivi des KPIs.",
                "url"     : "https://fr.indeed.com",
                "plateforme": "indeed"
            })
    return samples

# ── Postuler (une offre) ──────────────────────────────────────────────────────
@app.route("/postuler", methods=["POST"])
@login_required
def postuler():
    user = current_user()
    db   = get_db()
    prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()

    job = {
        "job_id"     : request.form.get("job_id",""),
        "title"      : request.form.get("title",""),
        "company"    : request.form.get("company",""),
        "location"   : request.form.get("location",""),
        "salary"     : request.form.get("salary",""),
        "url"        : request.form.get("url",""),
        "description": request.form.get("snippet",""),
    }
    score = int(request.form.get("score", 0))

    lettre = ""
    if prof and prof["gen_lettre"]:
        lettre = generate_letter(prof["cv_text"] or "", job, user["name"])

    try:
        db.execute("""INSERT INTO candidatures
            (user_id,job_id,poste,entreprise,localisation,salaire,plateforme,lien,score_cv,lettre)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], job["job_id"], job["title"], job["company"],
             job["location"], job["salary"], "indeed", job["url"], score, lettre))
        db.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        db.close()

    return redirect(url_for("dashboard"))

# ── Statut candidature ────────────────────────────────────────────────────────
@app.route("/candidature/<int:cid>/statut", methods=["POST"])
@login_required
def update_statut(cid):
    user   = current_user()
    statut = request.form.get("statut","")
    db     = get_db()
    db.execute("UPDATE candidatures SET statut=? WHERE id=? AND user_id=?",
               (statut, cid, user["id"]))
    db.commit()
    db.close()
    return redirect(url_for("dashboard"))

# ── Voir lettre ───────────────────────────────────────────────────────────────
@app.route("/lettre/<int:cid>")
@login_required
def voir_lettre(cid):
    user = current_user()
    db   = get_db()
    c    = db.execute("SELECT * FROM candidatures WHERE id=? AND user_id=?",
                      (cid, user["id"])).fetchone()
    db.close()
    if not c:
        return redirect(url_for("dashboard"))
    return render(LAYOUT.replace("{% block body %}{% endblock %}", LETTRE_HTML), candidature=c)

# ── API : ajouter offres depuis Claude ───────────────────────────────────────
@app.route("/api/add-jobs", methods=["POST"])
@login_required
def api_add_jobs():
    """Claude envoie les offres trouvées via Indeed MCP vers cette route."""
    user = current_user()
    db   = get_db()
    prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    jobs = request.get_json(force=True) or []
    added = 0
    for job in jobs:
        score  = score_cv_job(prof["cv_text"] or "", job.get("title",""), job.get("description",""))
        lettre = ""
        if prof["gen_lettre"]:
            lettre = generate_letter(prof["cv_text"] or "", job, user["name"])
        try:
            db.execute("""INSERT INTO candidatures
                (user_id,job_id,poste,entreprise,localisation,salaire,plateforme,lien,score_cv,lettre)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user["id"], job.get("job_id",str(uuid.uuid4())[:8]),
                 job.get("title",""), job.get("company",""), job.get("location",""),
                 job.get("salary",""), job.get("plateforme","indeed"),
                 job.get("url",""), score, lettre))
            added += 1
        except sqlite3.IntegrityError:
            pass
    db.commit()
    db.close()
    return jsonify({"added": added})

# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    print("\n" + "═"*50)
    print("🤖 JobBot — Serveur démarré !")
    print("📌 Ouvre : http://localhost:5000")
    print("🔗 Partage sur ton réseau : http://<ton-ip>:5000")
    print("═"*50 + "\n")
    app.run(debug=False, host="0.0.0.0", port=5000)
