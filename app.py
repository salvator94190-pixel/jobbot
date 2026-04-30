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
from job_search import search_jobs, score_cv_job as _score
from cv_optimizer import (process_cover_letter, score_ai_detection,
                          extract_ats_keywords, adapt_cv_for_job)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DB_PATH     = BASE_DIR / "database.db"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
(BASE_DIR / "lettres").mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024   # 10 MB max CV

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
FT_CLIENT_ID   = os.environ.get("FT_CLIENT_ID", "")    # France Travail (optionnel)
FT_SECRET      = os.environ.get("FT_CLIENT_SECRET", "") # France Travail (optionnel)

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
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER REFERENCES users(id),
        job_id              TEXT,
        poste               TEXT,
        entreprise          TEXT,
        localisation        TEXT,
        salaire             TEXT,
        plateforme          TEXT DEFAULT 'indeed',
        lien                TEXT,
        description_offre   TEXT DEFAULT '',
        score_cv            INTEGER DEFAULT 0,
        lettre_brute        TEXT DEFAULT '',
        lettre              TEXT DEFAULT '',
        score_ia_avant      INTEGER DEFAULT 0,
        score_ia_apres      INTEGER DEFAULT 0,
        keywords_ats        TEXT DEFAULT '[]',
        validation_status   TEXT DEFAULT 'pending',
        note_utilisateur    TEXT DEFAULT '',
        statut              TEXT DEFAULT 'À postuler',
        created_at          TEXT DEFAULT (datetime('now')),
        validated_at        TEXT DEFAULT '',
        UNIQUE(user_id, job_id)
    );

    -- Migration silencieuse pour bases existantes
    -- (SQLite ignore les colonnes déjà présentes)
    """)

    # Migration colonnes manquantes (bases existantes)
    cols_needed = {
        "candidatures": [
            ("description_offre",  "TEXT DEFAULT ''"),
            ("lettre_brute",       "TEXT DEFAULT ''"),
            ("score_ia_avant",     "INTEGER DEFAULT 0"),
            ("score_ia_apres",     "INTEGER DEFAULT 0"),
            ("keywords_ats",       "TEXT DEFAULT '[]'"),
            ("validation_status",  "TEXT DEFAULT 'pending'"),
            ("note_utilisateur",   "TEXT DEFAULT ''"),
            ("validated_at",       "TEXT DEFAULT ''"),
            ("cv_adapte",          "TEXT DEFAULT ''"),
            ("cv_docx_path",       "TEXT DEFAULT ''"),
        ]
    }
    for table, cols in cols_needed.items():
        existing = [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]
        for col_name, col_def in cols:
            if col_name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
    db.commit()
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

# ── Analyse CV ────────────────────────────────────────────────────────────────
def analyze_cv(cv_text: str, postes: list, user_name: str) -> dict:
    """Analyse le CV face aux postes ciblés et retourne un rapport structuré JSON."""
    if not ANTHROPIC_KEY:
        return {}
    if not cv_text:
        return {}
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        postes_str = ", ".join(postes) if postes else "non précisés"
        msg = c.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1800,
            messages=[{"role": "user", "content": f"""
Tu es un expert RH et coach carrière. Analyse ce CV en profondeur par rapport aux postes ciblés.

CANDIDAT : {user_name}
POSTES CIBLÉS : {postes_str}

CV COMPLET :
{cv_text[:4000]}

Retourne UNIQUEMENT un JSON valide (pas de texte autour) avec cette structure exacte :
{{
  "score_global": <entier 0-100>,
  "resume": "<2 phrases résumant le profil et son adéquation aux postes>",
  "points_forts": [
    {{"titre": "<force>", "detail": "<explication concrète>"}},
    {{"titre": "<force>", "detail": "<explication concrète>"}},
    {{"titre": "<force>", "detail": "<explication concrète>"}}
  ],
  "points_faibles": [
    {{"titre": "<faiblesse>", "detail": "<ce qui manque par rapport aux postes visés>"}},
    {{"titre": "<faiblesse>", "detail": "<ce qui manque par rapport aux postes visés>"}}
  ],
  "ameliorations": [
    {{"priorite": "haute", "action": "<action concrète à faire>", "impact": "<pourquoi ça aide>"}},
    {{"priorite": "haute", "action": "<action concrète>", "impact": "<pourquoi>"}},
    {{"priorite": "moyenne", "action": "<action concrète>", "impact": "<pourquoi>"}},
    {{"priorite": "moyenne", "action": "<action concrète>", "impact": "<pourquoi>"}},
    {{"priorite": "basse", "action": "<action concrète>", "impact": "<pourquoi>"}}
  ],
  "mots_cles_manquants": ["<mot-clé>", "<mot-clé>", "<mot-clé>", "<mot-clé>", "<mot-clé>"],
  "mots_cles_presents": ["<mot-clé>", "<mot-clé>", "<mot-clé>"],
  "conseil_format": "<conseil sur la mise en forme et la structure du CV>",
  "sections": {{
    "experience": <entier 0-100>,
    "competences": <entier 0-100>,
    "formation": <entier 0-100>,
    "impact": <entier 0-100>
  }}
}}
"""}]
        )
        raw = msg.content[0].text.strip()
        # Nettoyer si le modèle ajoute du texte autour
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
        return json.loads(raw)
    except Exception as e:
        return {}

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
  <a href="/valider" style="color:var(--yw);font-weight:700">✅ À valider</a>
  <a href="/analyse-cv">🧠 Analyser mon CV</a>
  <a href="/optimiser">🛡️ Anti-détection IA</a>
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
      <a href="/analyse-cv" class="btn btn-sec">🧠 Analyser mon CV</a>
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

ANALYSE_HTML = """
<div class="page" style="max-width:900px">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:8px">
    <a href="/dashboard" style="color:var(--mt);font-size:.85rem">← Dashboard</a>
  </div>
  <h2>🧠 Analyse de ton CV</h2>
  <p class="sub">L'IA compare ton CV avec tes postes ciblés et te dit exactement quoi améliorer.</p>

  {% if not profile.cv_filename %}
  <div class="card" style="border-color:var(--yw);text-align:center;padding:50px">
    <div style="font-size:2.5rem;margin-bottom:12px">📄</div>
    <p style="font-weight:700;margin-bottom:8px">Aucun CV uploadé</p>
    <p style="color:var(--mt);margin-bottom:20px">Upload ton CV dans ton profil pour lancer l'analyse.</p>
    <a href="/profile" class="btn btn-pr">Uploader mon CV →</a>
  </div>

  {% elif not postes %}
  <div class="card" style="border-color:var(--yw);text-align:center;padding:50px">
    <div style="font-size:2.5rem;margin-bottom:12px">🎯</div>
    <p style="font-weight:700;margin-bottom:8px">Aucun poste ciblé défini</p>
    <p style="color:var(--mt);margin-bottom:20px">Ajoute des postes ciblés dans ton profil pour que l'IA puisse calibrer l'analyse.</p>
    <a href="/profile" class="btn btn-pr">Configurer mes critères →</a>
  </div>

  {% elif not analyse %}
  <div style="text-align:center;padding:40px 0">
    <form method="POST" action="/analyse-cv">
      <button class="btn btn-pr" style="font-size:1rem;padding:16px 40px">
        🚀 Lancer l'analyse IA
      </button>
      <p style="color:var(--mt);font-size:.82rem;margin-top:14px">Prend ~10 secondes · Postes ciblés : {{ postes|join(', ') }}</p>
    </form>
  </div>

  {% else %}

  <!-- Score global -->
  <div class="card" style="margin-bottom:20px;background:linear-gradient(135deg,#1a1d27,#222535)">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px">
      <div>
        <div style="font-size:.8rem;color:var(--mt);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Score global</div>
        <div style="font-size:3.5rem;font-weight:900;line-height:1;color:{% if analyse.score_global>=75 %}var(--gr){% elif analyse.score_global>=50 %}var(--acc){% else %}var(--rd){% endif %}">
          {{ analyse.score_global }}<span style="font-size:1.5rem">/100</span>
        </div>
        <div style="color:var(--mt);font-size:.88rem;margin-top:8px;max-width:500px">{{ analyse.resume }}</div>
      </div>
      <!-- Radar scores -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;min-width:220px">
        {% for label, key in [('Expérience','experience'),('Compétences','competences'),('Formation','formation'),('Impact','impact')] %}
        <div>
          <div style="font-size:.72rem;color:var(--mt);margin-bottom:4px">{{ label }}</div>
          <div style="display:flex;align-items:center;gap:6px">
            <div style="flex:1;height:6px;background:var(--bd);border-radius:3px;overflow:hidden">
              <div style="height:100%;width:{{ analyse.sections[key] }}%;background:{% if analyse.sections[key]>=75 %}var(--gr){% elif analyse.sections[key]>=50 %}var(--acc){% else %}var(--rd){% endif %};border-radius:3px"></div>
            </div>
            <span style="font-size:.75rem;font-weight:700">{{ analyse.sections[key] }}%</span>
          </div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">
    <!-- Points forts -->
    <div class="card">
      <h3 style="color:var(--gr)">✅ Points forts</h3>
      {% for p in analyse.points_forts %}
      <div style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--bd)">
        <div style="font-weight:700;font-size:.9rem;margin-bottom:3px">{{ p.titre }}</div>
        <div style="color:var(--mt);font-size:.83rem;line-height:1.5">{{ p.detail }}</div>
      </div>
      {% endfor %}
    </div>

    <!-- Points faibles -->
    <div class="card">
      <h3 style="color:var(--rd)">⚠️ Points à améliorer</h3>
      {% for p in analyse.points_faibles %}
      <div style="margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--bd)">
        <div style="font-weight:700;font-size:.9rem;margin-bottom:3px">{{ p.titre }}</div>
        <div style="color:var(--mt);font-size:.83rem;line-height:1.5">{{ p.detail }}</div>
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- Plan d'action -->
  <div class="card" style="margin-bottom:20px">
    <h3>🎯 Plan d'action — ce que tu dois faire</h3>
    {% for a in analyse.ameliorations %}
    <div style="display:flex;gap:14px;padding:14px 0;border-bottom:1px solid var(--bd);align-items:flex-start">
      <span class="pill {% if a.priorite=='haute' %}pill-rd{% elif a.priorite=='moyenne' %}pill-yw{% else %}pill-mt{% endif %}" style="white-space:nowrap;margin-top:2px">
        {{ '🔴 Urgent' if a.priorite=='haute' else ('🟡 Moyen' if a.priorite=='moyenne' else '🟢 Plus tard') }}
      </span>
      <div>
        <div style="font-weight:700;font-size:.9rem;margin-bottom:3px">{{ a.action }}</div>
        <div style="color:var(--mt);font-size:.82rem">{{ a.impact }}</div>
      </div>
    </div>
    {% endfor %}
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">
    <!-- Mots-clés manquants -->
    <div class="card">
      <h3>🔑 Mots-clés manquants</h3>
      <p style="color:var(--mt);font-size:.82rem;margin-bottom:14px">Ces termes apparaissent dans les offres de ton secteur mais pas dans ton CV. Ajoute-les si pertinent.</p>
      <div>
        {% for kw in analyse.mots_cles_manquants %}
        <span class="pill pill-rd" style="margin:3px">{{ kw }}</span>
        {% endfor %}
      </div>
    </div>

    <!-- Mots-clés présents -->
    <div class="card">
      <h3>✅ Mots-clés détectés</h3>
      <p style="color:var(--mt);font-size:.82rem;margin-bottom:14px">Bonne nouvelle — ces termes clés sont déjà dans ton CV.</p>
      <div>
        {% for kw in analyse.mots_cles_presents %}
        <span class="pill pill-gr" style="margin:3px">{{ kw }}</span>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- Conseil format -->
  <div class="card" style="border-color:var(--acc)">
    <h3 style="color:var(--acc)">💡 Conseil mise en forme</h3>
    <p style="color:var(--mt);font-size:.88rem;line-height:1.6">{{ analyse.conseil_format }}</p>
  </div>

  <div style="margin-top:20px;display:flex;gap:12px;flex-wrap:wrap">
    <form method="POST" action="/analyse-cv">
      <button class="btn btn-sec">🔄 Relancer l'analyse</button>
    </form>
    <a href="/profile" class="btn btn-pr">✏️ Modifier mon CV</a>
  </div>

  {% endif %}
</div>
"""

VALIDATION_LIST_HTML = """
<div class="page">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px">
    <div>
      <h2>✅ Candidatures à valider</h2>
      <p class="sub" style="margin:0">Revois et approuve chaque lettre avant qu'elle parte. Rien n'est envoyé sans ton accord.</p>
    </div>
    <a href="/dashboard" class="btn btn-sec">← Dashboard</a>
  </div>

  {% if not pending %}
  <div class="card" style="text-align:center;padding:60px">
    <div style="font-size:2.5rem;margin-bottom:12px">🎉</div>
    <p style="font-weight:700">Tout est validé !</p>
    <p style="color:var(--mt);margin-top:8px">Lance une nouvelle recherche pour obtenir de nouvelles candidatures.</p>
  </div>
  {% else %}
  <div style="display:grid;gap:16px">
  {% for c in pending %}
  <div class="card" style="border-left:4px solid var(--yw)">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">
      <div>
        <div style="font-size:1.05rem;font-weight:700">{{ c.poste }}</div>
        <div style="color:var(--mt);font-size:.88rem;margin-top:2px">
          {{ c.entreprise }} — {{ c.localisation or 'Localisation non précisée' }}
          {% if c.salaire %} · {{ c.salaire }}{% endif %}
        </div>
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <span class="pill pill-mt">{{ c.plateforme }}</span>
          <span class="pill {% if c.score_cv>=75 %}pill-gr{% elif c.score_cv>=50 %}pill-acc{% else %}pill-rd{% endif %}">
            Score CV : {{ c.score_cv }}%
          </span>
          {% if c.score_ia_avant %}
          <span class="pill pill-yw">IA avant : {{ c.score_ia_avant }}/100</span>
          <span class="pill pill-gr">IA après : {{ c.score_ia_apres }}/100</span>
          {% endif %}
        </div>
      </div>
      <div style="display:flex;gap:8px">
        <a href="/valider/{{ c.id }}" class="btn btn-pr btn-sm">👁️ Revoir & Valider</a>
        <form method="POST" action="/valider/{{ c.id }}/rejeter" style="display:inline">
          <button class="btn btn-sec btn-sm" style="color:var(--rd);border-color:var(--rd)">✗ Ignorer</button>
        </form>
      </div>
    </div>
  </div>
  {% endfor %}
  </div>
  {% endif %}
</div>
"""

VALIDATION_DETAIL_HTML = """
<div class="page" style="max-width:1200px">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:20px;flex-wrap:wrap">
    <a href="/valider" style="color:var(--mt);font-size:.85rem">← Retour à la liste</a>
    <h2 style="margin:0">✅ Valider la candidature</h2>
  </div>

  <!-- Header offre -->
  <div class="card" style="margin-bottom:20px;background:linear-gradient(135deg,var(--s),var(--s2))">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">
      <div>
        <div style="font-size:1.2rem;font-weight:800">{{ c.poste }}</div>
        <div style="color:var(--mt);margin-top:4px">{{ c.entreprise }} · {{ c.localisation }} {% if c.salaire %}· 💰 {{ c.salaire }}{% endif %}</div>
        {% if c.lien %}<a href="{{ c.lien }}" target="_blank" style="font-size:.82rem;color:var(--acc);margin-top:6px;display:inline-block">🔗 Voir l'offre originale →</a>{% endif %}
      </div>
      <div style="text-align:right">
        <div style="font-size:.75rem;color:var(--mt)">Score CV</div>
        <div style="font-size:1.8rem;font-weight:800;color:{% if c.score_cv>=75 %}var(--gr){% elif c.score_cv>=50 %}var(--acc){% else %}var(--rd){% endif %}">{{ c.score_cv }}%</div>
      </div>
    </div>
    {% if c.score_ia_avant %}
    <div style="display:flex;gap:12px;margin-top:14px;flex-wrap:wrap">
      <span class="pill pill-yw">🤖 Détection IA avant : {{ c.score_ia_avant }}/100</span>
      <span class="pill pill-gr">✅ Après humanisation : {{ c.score_ia_apres }}/100</span>
      <span class="pill pill-gr">📉 Amélioration : -{{ c.score_ia_avant - c.score_ia_apres }} pts</span>
    </div>
    {% endif %}
  </div>

  <form method="POST" action="/valider/{{ c.id }}/approuver">

  <!-- ══ SECTION CV ══════════════════════════════════════════════════════════ -->
  <div style="margin-bottom:10px">
    <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:var(--mt);font-weight:700;margin-bottom:10px">
      📄 CURRICULUM VITAE
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:6px">

      <!-- CV Original -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0;color:var(--mt);font-size:.88rem">📄 CV original</h3>
        </div>
        <div style="background:var(--s2);border-radius:8px;padding:14px;white-space:pre-wrap;font-size:.78rem;line-height:1.6;border:1px solid var(--bd);max-height:340px;overflow-y:auto;color:var(--mt)">{{ cv_original or '(CV non chargé dans le profil)' }}</div>
      </div>

      <!-- CV Adapté (éditable) -->
      <div class="card" style="border-color:var(--gr)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0;color:var(--gr);font-size:.88rem">🎯 CV adapté à cette offre</h3>
          <span style="font-size:.72rem;color:var(--mt)">Éditable</span>
        </div>
        {% if c.cv_adapte %}
        <textarea name="cv_final" rows="16"
          style="width:100%;background:var(--s2);border:1px solid var(--gr);border-radius:8px;
                 padding:14px;font-size:.78rem;line-height:1.6;color:var(--tx);resize:vertical">{{ c.cv_adapte }}</textarea>
        {% else %}
        <div style="background:var(--s2);border-radius:8px;padding:14px;color:var(--mt);font-size:.82rem;border:1px dashed var(--bd)">
          ⚠️ Pas de CV uploadé ou clé Anthropic manquante — adaptation non disponible.
          <textarea name="cv_final" rows="8" style="margin-top:10px;width:100%"
            placeholder="Tu peux coller ici ta version adaptée manuellement…"></textarea>
        </div>
        {% endif %}
      </div>
    </div>
  </div>

  <!-- ══ SECTION LETTRE ══════════════════════════════════════════════════════ -->
  <div style="margin-bottom:10px">
    <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:var(--mt);font-weight:700;margin-bottom:10px;margin-top:8px">
      ✉️ LETTRE DE MOTIVATION
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:6px">

      <!-- Lettre brute -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0;color:var(--mt);font-size:.88rem">🤖 Version brute (avant humanisation)</h3>
        </div>
        <div style="background:var(--s2);border-radius:8px;padding:14px;white-space:pre-wrap;font-size:.8rem;line-height:1.6;border:1px solid var(--bd);max-height:340px;overflow-y:auto;color:var(--mt)">{{ c.lettre_brute or '(non disponible)' }}</div>
      </div>

      <!-- Lettre optimisée (éditable) -->
      <div class="card" style="border-color:var(--acc)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="margin:0;color:var(--acc);font-size:.88rem">✨ Version humanisée & optimisée ATS</h3>
          <span style="font-size:.72rem;color:var(--mt)">Éditable</span>
        </div>
        <textarea name="lettre_finale" rows="16"
          style="width:100%;background:var(--s2);border:1px solid var(--acc);border-radius:8px;
                 padding:14px;font-size:.8rem;line-height:1.65;color:var(--tx);resize:vertical">{{ c.lettre }}</textarea>
      </div>
    </div>
  </div>

  {% if keywords %}
  <div class="card" style="margin-bottom:16px">
    <h3>🔑 Mots-clés ATS injectés</h3>
    <div style="margin-top:6px">{% for kw in keywords %}<span style="display:inline-block;background:var(--s2);border:1px solid var(--acc);color:var(--acc);border-radius:6px;padding:3px 10px;font-size:.8rem;margin:3px">{{ kw }}</span>{% endfor %}</div>
  </div>
  {% endif %}

  <!-- Note de l'utilisateur -->
  <div class="card" style="margin-bottom:20px">
    <h3>💬 Ta note (optionnel)</h3>
    <textarea name="note_utilisateur" rows="2" placeholder="Ajoute une note — rappel, contact, contexte…" style="width:100%;margin-top:6px">{{ c.note_utilisateur or '' }}</textarea>
  </div>

  <!-- Boutons de validation -->
  <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
    <button type="submit" class="btn btn-gr" style="font-size:1rem;padding:14px 32px">
      ✅ Valider cette candidature
    </button>
    <a href="/valider" class="btn btn-sec">Revenir à la liste</a>
    <form method="POST" action="/valider/{{ c.id }}/rejeter" style="display:inline;margin:0">
      <button type="submit" class="btn btn-sec" style="color:var(--rd);border-color:var(--rd)">✗ Ignorer cette offre</button>
    </form>
  </div>
  </form>
</div>
"""

OPTIMIZER_HTML = """
<div class="page" style="max-width:900px">
  <h2>🛡️ Optimiseur Anti-Détection IA</h2>
  <p class="sub">Colle ta lettre ou une section de CV — le bot la réécrit pour qu'elle passe les filtres ATS et les détecteurs d'IA.</p>

  {% if result %}
  <!-- Résultats -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
    <div class="card" style="text-align:center">
      <div style="font-size:.75rem;color:var(--mt);text-transform:uppercase;margin-bottom:8px">Score IA avant</div>
      <div style="font-size:2.5rem;font-weight:900;color:{% if result.score_avant.score>=70 %}var(--rd){% elif result.score_avant.score>=40 %}var(--yw){% else %}var(--gr){% endif %}">
        {{ result.score_avant.score }}<span style="font-size:1rem">/100</span>
      </div>
      <div style="font-size:.8rem;color:var(--mt);margin-top:4px">{{ result.score_avant.niveau }}</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:.75rem;color:var(--mt);text-transform:uppercase;margin-bottom:8px">Score IA après</div>
      <div style="font-size:2.5rem;font-weight:900;color:{% if result.score_apres.score>=70 %}var(--rd){% elif result.score_apres.score>=40 %}var(--yw){% else %}var(--gr){% endif %}">
        {{ result.score_apres.score }}<span style="font-size:1rem">/100</span>
      </div>
      <div style="font-size:.8rem;color:var(--mt);margin-top:4px">{{ result.score_apres.niveau }}</div>
      {% if result.gain > 0 %}
      <div style="color:var(--gr);font-size:.8rem;font-weight:700;margin-top:4px">▼ -{{ result.gain }} pts d'amélioration</div>
      {% endif %}
    </div>
  </div>

  {% if result.keywords_ats %}
  <div class="card" style="margin-bottom:20px">
    <h3>🔑 Mots-clés ATS injectés</h3>
    <div>{% for kw in result.keywords_ats %}<span style="display:inline-block;background:var(--s2);border:1px solid var(--acc);color:var(--acc);border-radius:6px;padding:3px 10px;font-size:.8rem;margin:3px">{{ kw }}</span>{% endfor %}</div>
  </div>
  {% endif %}

  <div class="card" style="margin-bottom:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <h3 style="margin:0">✅ Texte optimisé</h3>
      <button onclick="navigator.clipboard.writeText(document.getElementById('result-text').textContent);this.textContent='✅ Copié !';setTimeout(()=>this.textContent='📋 Copier',2000)" class="btn btn-sec btn-sm">📋 Copier</button>
    </div>
    <div id="result-text" style="background:var(--s2);border-radius:8px;padding:18px;white-space:pre-wrap;font-size:.88rem;line-height:1.65;border:1px solid var(--bd)">{{ result.text }}</div>
  </div>

  <form method="POST" action="/optimiser">
    <input type="hidden" name="texte" value="{{ form_texte or '' }}">
    <input type="hidden" name="offre" value="{{ form_offre or '' }}">
    <button class="btn btn-sec">🔄 Relancer l'optimisation</button>
  </form>

  {% else %}
  <!-- Formulaire -->
  <form method="POST" action="/optimiser">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">
      <div class="card">
        <label>Ton texte à optimiser</label>
        <textarea name="texte" rows="12" placeholder="Colle ici ta lettre de motivation ou une section de ton CV…" style="margin-top:8px;resize:vertical">{{ form_texte or '' }}</textarea>
      </div>
      <div class="card">
        <label>Description du poste visé (optionnel mais recommandé)</label>
        <textarea name="offre" rows="12" placeholder="Colle ici la description de l'offre d'emploi pour que le bot injecte les bons mots-clés ATS…" style="margin-top:8px;resize:vertical">{{ form_offre or '' }}</textarea>
      </div>
    </div>

    <div class="card" style="margin-bottom:20px">
      <h3>🎯 Score actuel (avant optimisation)</h3>
      {% if score_preview %}
      <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
        <div>
          <span style="font-size:2rem;font-weight:800;color:{% if score_preview.score>=70 %}var(--rd){% elif score_preview.score>=40 %}var(--yw){% else %}var(--gr){% endif %}">
            {{ score_preview.score }}/100
          </span>
          <span style="color:var(--mt);font-size:.88rem;margin-left:10px">{{ score_preview.niveau }}</span>
        </div>
        {% if score_preview.patterns %}
        <div>
          <div style="font-size:.75rem;color:var(--mt);margin-bottom:6px">Patterns IA détectés :</div>
          {% for p in score_preview.patterns[:5] %}<span style="display:inline-block;background:#ff5f6d22;border:1px solid var(--rd);color:var(--rd);border-radius:5px;padding:2px 8px;font-size:.75rem;margin:2px">{{ p }}</span>{% endfor %}
        </div>
        {% endif %}
      </div>
      {% else %}
      <p style="color:var(--mt)">Colle ton texte pour voir le score en temps réel.</p>
      {% endif %}
    </div>

    <button class="btn btn-pr" style="font-size:1rem;padding:14px 36px">
      🚀 Humaniser & Optimiser ATS
    </button>
  </form>
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

    # ── Recherche multi-sources : ATS + job boards français ──────────────────
    raw_jobs = search_jobs(
        postes=postes,
        villes=villes,
        remote=bool(remote),
        mots_exclus=exclus,
        ft_client_id=FT_CLIENT_ID,
        ft_secret=FT_SECRET,
    )

    # Scoring CV + filtre score minimum
    filtered = []
    for job in raw_jobs:
        score = score_cv_job(
            cv_text,
            job.get("title", ""),
            job.get("description", "")
        )
        if score < score_min:
            continue
        job["score"]   = score
        job["snippet"] = job.get("description", "")[:200]
        filtered.append(job)

    filtered.sort(key=lambda j: j["score"], reverse=True)
    return render(LAYOUT.replace("{% block body %}{% endblock %}", RESULTS_HTML), jobs=filtered)

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

    lettre_brute     = ""
    lettre_optimisee = ""
    score_ia_avant   = 0
    score_ia_apres   = 0
    keywords_ats     = []
    cv_adapte        = ""
    cv_docx_path     = ""

    cv_text = prof["cv_text"] if prof else ""

    if prof and prof["gen_lettre"]:
        # 1. Adaptation du CV à l'offre (Claude Sonnet)
        if cv_text and ANTHROPIC_KEY:
            try:
                adapt = adapt_cv_for_job(
                    cv_text=cv_text,
                    job_title=job["title"],
                    job_description=job.get("description", ""),
                    user_name=user["name"],
                )
                cv_adapte = adapt.get("cv_complet_adapte", "") or ""
            except Exception as e:
                cv_adapte = ""

        # 2. Génération de la lettre brute
        lettre_brute = generate_letter(cv_text or "", job, user["name"])

        # 3. Humanisation + ATS
        if lettre_brute:
            opt = process_cover_letter(
                lettre=lettre_brute,
                job_description=job.get("description", ""),
                cv_text=cv_text or "",
                user_name=user["name"],
            )
            lettre_optimisee = opt.get("text") or lettre_brute
            score_ia_avant   = opt.get("score_avant", {}).get("score", 0)
            score_ia_apres   = opt.get("score_apres", {}).get("score", 0)
            keywords_ats     = opt.get("keywords_ats", [])
        else:
            lettre_optimisee = lettre_brute

    try:
        db.execute("""INSERT INTO candidatures
            (user_id,job_id,poste,entreprise,localisation,salaire,plateforme,lien,
             description_offre,score_cv,lettre_brute,lettre,
             score_ia_avant,score_ia_apres,keywords_ats,
             cv_adapte,cv_docx_path,
             validation_status,statut)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user["id"], job["job_id"], job["title"], job["company"],
             job["location"], job["salary"],
             job.get("plateforme","indeed"), job["url"],
             job.get("description","")[:1000],
             score, lettre_brute, lettre_optimisee,
             score_ia_avant, score_ia_apres,
             json.dumps(keywords_ats),
             cv_adapte, cv_docx_path,
             "pending",   # ← toujours en attente de validation
             "À valider"
            ))
        db.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        db.close()

    # Redirige vers la file de validation, pas le dashboard
    return redirect(url_for("validation_list"))

# ── Validation — liste des candidatures en attente ───────────────────────────
@app.route("/valider")
@login_required
def validation_list():
    user = current_user()
    db   = get_db()
    pending = db.execute(
        """SELECT * FROM candidatures
           WHERE user_id=? AND validation_status='pending'
           ORDER BY created_at DESC""",
        (user["id"],)
    ).fetchall()
    db.close()
    return render(
        LAYOUT.replace("{% block body %}{% endblock %}", VALIDATION_LIST_HTML),
        pending=pending
    )

# ── Validation — page de review d'une candidature ────────────────────────────
@app.route("/valider/<int:cid>")
@login_required
def validation_detail(cid):
    user = current_user()
    db   = get_db()
    c    = db.execute(
        "SELECT * FROM candidatures WHERE id=? AND user_id=?",
        (cid, user["id"])
    ).fetchone()
    db.close()
    if not c:
        return redirect(url_for("validation_list"))

    try:
        keywords = json.loads(c["keywords_ats"] or "[]")
    except Exception:
        keywords = []

    # CV original du profil (pour affichage côte à côte)
    db2  = get_db()
    prof = db2.execute("SELECT cv_text FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    db2.close()
    cv_original = prof["cv_text"] if prof else ""

    return render(
        LAYOUT.replace("{% block body %}{% endblock %}", VALIDATION_DETAIL_HTML),
        c=c, keywords=keywords, cv_original=cv_original
    )

# ── Validation — approuver ────────────────────────────────────────────────────
@app.route("/valider/<int:cid>/approuver", methods=["POST"])
@login_required
def validation_approuver(cid):
    user          = current_user()
    lettre_finale = request.form.get("lettre_finale", "").strip()
    cv_final      = request.form.get("cv_final", "").strip()
    note          = request.form.get("note_utilisateur", "").strip()
    db = get_db()
    db.execute(
        """UPDATE candidatures SET
           lettre=?, cv_adapte=?, note_utilisateur=?,
           validation_status='validated',
           statut='À postuler',
           validated_at=datetime('now')
           WHERE id=? AND user_id=?""",
        (lettre_finale, cv_final, note, cid, user["id"])
    )
    db.commit()
    db.close()

    # Retour à la liste — s'il reste des pending, sinon dashboard
    db2 = get_db()
    remaining = db2.execute(
        "SELECT COUNT(*) FROM candidatures WHERE user_id=? AND validation_status='pending'",
        (user["id"],)
    ).fetchone()[0]
    db2.close()
    return redirect(url_for("validation_list") if remaining else url_for("dashboard"))

# ── Validation — rejeter ──────────────────────────────────────────────────────
@app.route("/valider/<int:cid>/rejeter", methods=["POST"])
@login_required
def validation_rejeter(cid):
    user = current_user()
    db   = get_db()
    db.execute(
        "UPDATE candidatures SET validation_status='rejected', statut='Ignoré' WHERE id=? AND user_id=?",
        (cid, user["id"])
    )
    db.commit()
    db.close()
    return redirect(url_for("validation_list"))

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

# ── Analyse CV ────────────────────────────────────────────────────────────────
@app.route("/analyse-cv", methods=["GET","POST"])
@login_required
def analyse_cv():
    user = current_user()
    db   = get_db()
    prof = db.execute("SELECT * FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    db.close()

    postes  = json.loads(prof["postes"] or "[]") if prof else []
    analyse = None

    if request.method == "POST" and prof and prof["cv_text"]:
        analyse = analyze_cv(prof["cv_text"], postes, user["name"])

    return render(
        LAYOUT.replace("{% block body %}{% endblock %}", ANALYSE_HTML),
        profile=prof or {},
        postes=postes,
        analyse=analyse
    )

# ── Optimiseur anti-détection IA ─────────────────────────────────────────────
@app.route("/optimiser", methods=["GET","POST"])
@login_required
def optimiser():
    result        = None
    score_preview = None
    form_texte    = ""
    form_offre    = ""

    if request.method == "POST":
        form_texte = request.form.get("texte", "").strip()
        form_offre = request.form.get("offre", "").strip()

        if form_texte:
            result = process_cover_letter(
                lettre=form_texte,
                job_description=form_offre,
            )
        # Score preview pour GET avec texte pré-rempli
        score_preview = score_ai_detection(form_texte) if form_texte else None
    else:
        score_preview = None

    return render(
        LAYOUT.replace("{% block body %}{% endblock %}", OPTIMIZER_HTML),
        result=result,
        score_preview=score_preview,
        form_texte=form_texte,
        form_offre=form_offre,
    )

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
