#!/usr/bin/env python3
"""
Bot de candidature automatique
Auteur : généré par Claude / Cowork
Usage  : python bot_candidature.py
"""

import json
import os
import sys
import time
import csv
import re
import datetime
import subprocess
from pathlib import Path

# ─── Chemins ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CV_PATH     = BASE_DIR / "cv.pdf"
TRACKER_CSV = BASE_DIR / "candidatures.csv"
LOG_PATH    = BASE_DIR / "bot.log"

# ─── Logger simple ────────────────────────────────────────────────────────────
def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ─── Chargement config ────────────────────────────────────────────────────────
def load_config():
    if not CONFIG_PATH.exists():
        log("config.json introuvable. Créez-le d'abord.", "ERROR")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

# ─── Tracker CSV ──────────────────────────────────────────────────────────────
TRACKER_HEADERS = [
    "date", "statut", "poste", "entreprise", "localisation",
    "salaire", "plateforme", "lien", "score_cv", "lettre_generee", "notes"
]

def init_tracker():
    if not TRACKER_CSV.exists():
        with open(TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=TRACKER_HEADERS).writeheader()
        log("Tracker CSV créé.")

def save_to_tracker(entry: dict):
    entry["date"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(TRACKER_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKER_HEADERS)
        writer.writerow({k: entry.get(k, "") for k in TRACKER_HEADERS})

def already_applied(job_url: str) -> bool:
    if not TRACKER_CSV.exists():
        return False
    with open(TRACKER_CSV, encoding="utf-8") as f:
        return job_url in f.read()

# ─── Lecture CV ───────────────────────────────────────────────────────────────
def extract_cv_text(cv_path: Path) -> str:
    """Extrait le texte brut du CV (PDF)."""
    try:
        result = subprocess.run(
            ["pdftotext", str(cv_path), "-"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except FileNotFoundError:
        pass
    # Fallback python
    try:
        import pdfplumber
        with pdfplumber.open(cv_path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass
    log("Impossible d'extraire le CV. Vérifiez que pdfplumber est installé.", "WARN")
    return ""

# ─── Score CV/Offre ───────────────────────────────────────────────────────────
def score_cv_vs_job(cv_text: str, job_title: str, job_description: str) -> int:
    """
    Score simple basé sur les mots communs entre le CV et l'offre.
    Retourne un score de 0 à 100.
    """
    if not cv_text:
        return 50  # Pas de CV → score neutre

    def tokenize(text):
        return set(re.findall(r'\b[a-zA-ZÀ-ÿ]{3,}\b', text.lower()))

    cv_words  = tokenize(cv_text)
    job_words = tokenize(f"{job_title} {job_description}")

    if not job_words:
        return 50

    common    = cv_words & job_words
    score     = min(100, int(len(common) / max(len(job_words), 1) * 200))
    return score

# ─── Filtrage offre ───────────────────────────────────────────────────────────
def passes_filters(job: dict, config: dict) -> tuple[bool, str]:
    """Retourne (ok, raison_rejet)."""
    title       = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    full_text   = f"{title} {description}"

    # Mots-clés exclus
    for kw in config.get("mots_cles_exclus", []):
        if kw.lower() in full_text:
            return False, f"mot exclu: '{kw}'"

    # Entreprises exclues
    company = (job.get("company") or "").lower()
    for exc in config.get("entreprises_exclues", []):
        if exc.lower() in company:
            return False, f"entreprise exclue: '{exc}'"

    # Mots-clés requis
    for kw in config.get("mots_cles_requis", []):
        if kw.lower() not in full_text:
            return False, f"mot requis absent: '{kw}'"

    return True, ""

# ─── Génération lettre de motivation ──────────────────────────────────────────
def generate_cover_letter(cv_text: str, job: dict, config: dict) -> str:
    """
    Génère une lettre de motivation personnalisée via Claude API.
    Nécessite la variable d'environnement ANTHROPIC_API_KEY.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("ANTHROPIC_API_KEY non définie — lettre non générée.", "WARN")
        return ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""Tu es un expert RH. Rédige une lettre de motivation professionnelle et percutante.

POSTE VISÉ : {job.get('title', '')} chez {job.get('company', '')}
LOCALISATION : {job.get('location', '')}

DESCRIPTION DU POSTE :
{job.get('description', '')[:2000]}

MON CV (extrait) :
{cv_text[:3000]}

CONSIGNES :
- Lettre en français, ton professionnel mais chaleureux
- 3 paragraphes maximum : accroche / compétences clés / motivation
- Personnalise en fonction du poste ET de l'entreprise
- Pas de formule générique
- Termine par une formule de politesse classique
"""
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        log(f"Erreur génération lettre : {e}", "ERROR")
        return ""

# ─── Moteur principal ─────────────────────────────────────────────────────────
def run_bot():
    log("═" * 50)
    log("🤖 BOT CANDIDATURE — Démarrage")
    log("═" * 50)

    config = load_config()
    init_tracker()

    # Charger le CV
    cv_text = ""
    cv_file = BASE_DIR / config.get("cv_path", "cv.pdf")
    if cv_file.exists():
        log(f"Chargement du CV : {cv_file.name}")
        cv_text = extract_cv_text(cv_file)
        log(f"CV extrait ({len(cv_text)} caractères)")
    else:
        log(f"CV non trouvé ({cv_file}). Le scoring CV sera désactivé.", "WARN")

    postes     = config.get("postes", [])
    villes     = config.get("localisation", {}).get("villes", ["Paris"])
    remote     = config.get("localisation", {}).get("remote", True)
    score_min  = config.get("score_minimum_cv", 60)
    max_cand   = config.get("candidature", {}).get("max_candidatures_par_session", 20)
    delai      = config.get("candidature", {}).get("delai_entre_candidatures_secondes", 30)
    gen_lettre = config.get("candidature", {}).get("generer_lettre_motivation", True)

    locations = list(villes)
    if remote:
        locations.append("remote")

    total_candidatures = 0
    offres_analysees   = 0
    offres_retenues    = []

    log(f"Postes ciblés   : {', '.join(postes)}")
    log(f"Localisations   : {', '.join(locations)}")
    log(f"Score CV min    : {score_min}%")
    log(f"Max candidatures: {max_cand}")
    log("")

    # ── Recherche Indeed (via MCP — appelé par Claude en contexte) ──
    # Note : en exécution autonome, remplace cette section par un appel HTTP
    # à l'API Indeed ou à un service de scraping.
    # Le fichier offres_found.json sera rempli par le mode "assistant".
    offres_found_path = BASE_DIR / "offres_found.json"

    if offres_found_path.exists():
        log("Chargement des offres pré-trouvées (offres_found.json)...")
        with open(offres_found_path, encoding="utf-8") as f:
            all_jobs = json.load(f)
        log(f"{len(all_jobs)} offres chargées.")
    else:
        log("Aucun fichier offres_found.json. Lance d'abord la recherche via l'assistant.", "WARN")
        all_jobs = []

    # ── Filtrage & scoring ──
    for job in all_jobs:
        if total_candidatures >= max_cand:
            log(f"Limite de {max_cand} candidatures atteinte.")
            break

        offres_analysees += 1
        job_url = job.get("url", "")

        # Déjà postulé ?
        if job_url and already_applied(job_url):
            log(f"Déjà postulé — skip : {job.get('title')} @ {job.get('company')}")
            continue

        # Filtres
        ok, raison = passes_filters(job, config)
        if not ok:
            log(f"  ✗ Exclu ({raison}) : {job.get('title')} @ {job.get('company')}")
            continue

        # Score CV
        score = score_cv_vs_job(
            cv_text,
            job.get("title", ""),
            job.get("description", "")
        )
        job["score_cv"] = score

        if score < score_min:
            log(f"  ✗ Score trop faible ({score}%) : {job.get('title')} @ {job.get('company')}")
            continue

        log(f"  ✓ Retenu (score {score}%) : {job.get('title')} @ {job.get('company')}")
        offres_retenues.append(job)

    log(f"\n{len(offres_retenues)} offres retenues sur {offres_analysees} analysées.\n")

    # ── Génération lettres & enregistrement ──
    for job in offres_retenues:
        if total_candidatures >= max_cand:
            break

        lettre = ""
        if gen_lettre and cv_text:
            log(f"Génération lettre pour : {job.get('title')} @ {job.get('company')}")
            lettre = generate_cover_letter(cv_text, job, config)
            if lettre:
                lettre_path = BASE_DIR / "lettres" / f"{job.get('company','cie').replace(' ','_')}_{job.get('job_id','')}.txt"
                lettre_path.parent.mkdir(exist_ok=True)
                lettre_path.write_text(lettre, encoding="utf-8")
                log(f"  → Lettre sauvegardée : {lettre_path.name}")

        save_to_tracker({
            "statut"        : "À postuler",
            "poste"         : job.get("title", ""),
            "entreprise"    : job.get("company", ""),
            "localisation"  : job.get("location", ""),
            "salaire"       : job.get("salary", "N/A"),
            "plateforme"    : job.get("plateforme", "indeed"),
            "lien"          : job.get("url", ""),
            "score_cv"      : job.get("score_cv", ""),
            "lettre_generee": "Oui" if lettre else "Non",
            "notes"         : ""
        })

        total_candidatures += 1
        log(f"  ✓ Enregistré ({total_candidatures}/{max_cand})")
        time.sleep(1)  # petite pause entre enregistrements

    log("")
    log("═" * 50)
    log(f"✅ SESSION TERMINÉE — {total_candidatures} candidatures enregistrées")
    log(f"📄 Tracker : {TRACKER_CSV}")
    log("═" * 50)

if __name__ == "__main__":
    run_bot()
