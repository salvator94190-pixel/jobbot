"""
JobBot — Moteur de recherche d'offres multi-ATS
Inspiré de career-ops (github.com/santifer/career-ops)

Interroge directement les APIs publiques :
  - Greenhouse  : boards-api.greenhouse.io
  - Ashby       : api.ashbyhq.com
  - Lever       : api.lever.co

Zéro quota, zéro clé API, réponses en temps réel.
"""

import re
import uuid
import yaml
import logging
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

BASE_DIR       = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.yml"

# Job boards français
try:
    from job_search_fr import search_french_boards
    FR_BOARDS_AVAILABLE = True
except ImportError:
    FR_BOARDS_AVAILABLE = False
    log.warning("job_search_fr.py introuvable — boards FR désactivés")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)",
    "Accept": "application/json",
}
TIMEOUT = 10  # secondes


# ── Chargement des entreprises ────────────────────────────────────────────────
def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        log.warning("companies.yml introuvable")
        return []
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [c for c in (data.get("companies") or []) if c.get("enabled", True)]


# ── APIs ATS ──────────────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        jobs = r.json().get("jobs", [])
        return [_normalize_greenhouse(j, slug) for j in jobs]
    except Exception as e:
        log.debug(f"Greenhouse {slug}: {e}")
        return []


def _normalize_greenhouse(j: dict, slug: str) -> dict:
    location = ""
    if j.get("location"):
        location = j["location"].get("name", "")
    return {
        "job_id"     : f"gh_{j.get('id', uuid.uuid4().hex[:8])}",
        "title"      : j.get("title", ""),
        "company"    : j.get("company_name", slug),
        "location"   : location,
        "salary"     : "",
        "description": "",
        "url"        : j.get("absolute_url", f"https://boards.greenhouse.io/{slug}"),
        "plateforme" : "greenhouse",
    }


def fetch_ashby(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        jobs = r.json().get("jobPostings", [])
        return [_normalize_ashby(j, slug) for j in jobs]
    except Exception as e:
        log.debug(f"Ashby {slug}: {e}")
        return []


def _normalize_ashby(j: dict, slug: str) -> dict:
    # Salaire depuis compensationTierSummary
    salary = ""
    comp = j.get("compensationTierSummary", "")
    if comp:
        salary = comp

    location = j.get("locationName", "") or j.get("location", "")

    return {
        "job_id"     : f"ash_{j.get('id', uuid.uuid4().hex[:8])}",
        "title"      : j.get("title", ""),
        "company"    : j.get("organizationName", slug),
        "location"   : location,
        "salary"     : salary,
        "description": j.get("descriptionPlain", "")[:500],
        "url"        : j.get("jobUrl", f"https://jobs.ashbyhq.com/{slug}"),
        "plateforme" : "ashby",
    }


def fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        jobs = r.json()
        if not isinstance(jobs, list):
            return []
        return [_normalize_lever(j, slug) for j in jobs]
    except Exception as e:
        log.debug(f"Lever {slug}: {e}")
        return []


def _normalize_lever(j: dict, slug: str) -> dict:
    location = ""
    categories = j.get("categories", {})
    if categories:
        location = categories.get("location", "") or categories.get("country", "")

    # Description courte
    desc = ""
    lists = j.get("lists", [])
    if lists:
        desc = lists[0].get("content", "")[:300]

    return {
        "job_id"     : f"lv_{j.get('id', uuid.uuid4().hex[:8])}",
        "title"      : j.get("text", ""),
        "company"    : slug.replace("-", " ").title(),
        "location"   : location,
        "salary"     : "",
        "description": desc,
        "url"        : j.get("hostedUrl", f"https://jobs.lever.co/{slug}"),
        "plateforme" : "lever",
    }


# ── Fetch une entreprise ──────────────────────────────────────────────────────
def fetch_company(company: dict) -> list[dict]:
    ats  = company.get("ats", "greenhouse").lower()
    slug = company.get("slug", "")
    name = company.get("name", slug)

    if not slug:
        return []

    if ats == "greenhouse":
        jobs = fetch_greenhouse(slug)
    elif ats == "ashby":
        jobs = fetch_ashby(slug)
    elif ats == "lever":
        jobs = fetch_lever(slug)
    else:
        log.warning(f"ATS inconnu pour {name}: {ats}")
        return []

    # Injecte le nom de l'entreprise si vide
    for j in jobs:
        if not j.get("company"):
            j["company"] = name

    log.info(f"  {name} ({ats}): {len(jobs)} offres")
    return jobs


# ── Filtrage ──────────────────────────────────────────────────────────────────
def matches_filters(job: dict, postes: list, villes: list,
                    remote: bool, mots_exclus: list) -> bool:
    title    = (job.get("title") or "").lower()
    desc     = (job.get("description") or "").lower()
    location = (job.get("location") or "").lower()
    full     = f"{title} {desc}"

    # Au moins un poste ciblé doit matcher
    if postes:
        poste_ok = any(p.lower() in title for p in postes)
        # Matching partiel : au moins 1 mot du poste dans le titre
        if not poste_ok:
            poste_ok = any(
                any(word in title for word in p.lower().split())
                for p in postes
            )
        if not poste_ok:
            return False

    # Mots exclus
    for kw in mots_exclus:
        if kw.lower() in full:
            return False

    # Localisation
    if villes or remote:
        loc_ok = False
        if remote and any(r in location for r in ["remote", "télétravail", "partout"]):
            loc_ok = True
        if not loc_ok and villes:
            loc_ok = any(v.lower() in location for v in villes)
        if not loc_ok and not location:
            loc_ok = True   # localisation vide → on garde
        if not loc_ok:
            return False

    return True


# ── Recherche principale ──────────────────────────────────────────────────────
def search_jobs(
    postes      : list[str],
    villes      : list[str]  = None,
    remote      : bool       = True,
    mots_exclus : list[str]  = None,
    max_workers : int        = 12,
    ft_client_id: str        = "",
    ft_secret   : str        = "",
) -> list[dict]:
    """
    Lance la recherche sur TOUTES les sources en parallèle :
      - 60+ entreprises directement via ATS (Greenhouse / Ashby / Lever)
      - Job boards français : WTTJ, APEC, Cadremploi, HelloWork, France Travail
    Retourne la liste des offres filtrées et dédupliquées.
    """
    villes      = villes or []
    mots_exclus = mots_exclus or []
    companies   = load_companies()

    all_jobs = []
    seen_ids = set()

    def add_jobs(jobs):
        for job in jobs:
            jid = job.get("job_id", "")
            if jid and jid in seen_ids:
                continue
            seen_ids.add(jid)
            if matches_filters(job, postes, villes, remote, mots_exclus):
                all_jobs.append(job)

    # ── 1. ATS directs (Greenhouse / Ashby / Lever) ──────────────────────────
    if companies:
        log.info(f"🏢 ATS directs : {len(companies)} entreprises")
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_company, c): c for c in companies}
            for future in as_completed(futures):
                try:
                    add_jobs(future.result())
                except Exception as e:
                    log.debug(f"ATS future: {e}")

    # ── 2. Job boards français ────────────────────────────────────────────────
    if FR_BOARDS_AVAILABLE:
        log.info("🇫🇷 Job boards français : WTTJ · APEC · Cadremploi · HelloWork")
        try:
            fr_jobs = search_french_boards(
                postes=postes,
                villes=villes,
                remote=remote,
                ft_client_id=ft_client_id,
                ft_secret=ft_secret,
            )
            add_jobs(fr_jobs)
        except Exception as e:
            log.warning(f"Boards FR erreur: {e}")

    log.info(f"✅ TOTAL : {len(all_jobs)} offres après filtrage et déduplication")
    return all_jobs


# ── Test standalone ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    results = search_jobs(
        postes=["Product Manager", "Chef de projet"],
        villes=["Paris", "France"],
        remote=True,
    )
    print(f"\n{len(results)} offres trouvées :\n")
    for j in results[:20]:
        print(f"  [{j['plateforme'].upper()}] {j['title']} @ {j['company']} — {j['location']}")
        print(f"    {j['url']}")
