"""
JobBot — Job boards français & internationaux
=============================================
Sources intégrées (par ordre de priorité) :

  1. Indeed France        — Flux RSS officiel (le + fiable, zéro clé)
  2. Monster France       — API JSON publique
  3. Welcome to the Jungle — Algolia API publique
  4. Cadremploi           — API publique
  5. APEC                 — API publique (cadres)
  6. HelloWork            — API publique
  7. France Travail       — API officielle (optionnelle, clé gratuite)
  8. Jobijoba             — Agrégateur RSS
"""

import os
import re
import uuid
import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, quote_plus

import requests

log = logging.getLogger(__name__)

# ── Session HTTP partagée avec retry ─────────────────────────────────────────
session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "application/json, text/html, application/xhtml+xml, */*",
})

TIMEOUT = 14


# ════════════════════════════════════════════════════════════════════════════════
#  1. INDEED FRANCE — Flux RSS officiel
#     Endpoint stable, zéro clé, résultats frais
# ════════════════════════════════════════════════════════════════════════════════

def fetch_indeed(query: str, lieu: str = "", nb: int = 50) -> list[dict]:
    """
    Indeed France via son flux RSS public.
    URL : https://fr.indeed.com/rss?q=...&l=...&radius=30&sort=date
    """
    try:
        params = {
            "q"      : query,
            "l"      : lieu,
            "radius" : 30,
            "sort"   : "date",
            "limit"  : min(nb, 50),
            "fromage": 14,        # offres des 14 derniers jours
        }
        url = "https://fr.indeed.com/rss?" + urlencode(
            {k: v for k, v in params.items() if v}
        )
        r = session.get(url, timeout=TIMEOUT,
                        headers={"Accept": "application/rss+xml, text/xml, */*"})
        if r.status_code != 200:
            log.debug(f"Indeed RSS HTTP {r.status_code}")
            return []

        return _parse_indeed_rss(r.text)

    except Exception as e:
        log.debug(f"Indeed: {e}")
        return []


def _parse_indeed_rss(xml_text: str) -> list[dict]:
    jobs = []
    try:
        root = ET.fromstring(xml_text)
        ns   = {"georss": "http://www.georss.org/georss"}

        for item in root.findall(".//item"):
            def tag(name):
                el = item.find(name)
                return el.text.strip() if el is not None and el.text else ""

            title   = tag("title")
            company = ""
            loc     = ""

            # Indeed encode "Titre - Entreprise - Lieu" dans le titre parfois
            if " - " in title:
                parts   = title.split(" - ")
                title   = parts[0].strip()
                if len(parts) >= 2: company = parts[1].strip()
                if len(parts) >= 3: loc     = parts[2].strip()

            # Source dans le tag <source>
            src_el = item.find("source")
            if src_el is not None and src_el.text and not company:
                company = src_el.text.strip()

            desc = tag("description")
            # Nettoie le HTML basique de la description
            desc = re.sub(r"<[^>]+>", " ", desc).strip()[:500]

            # Salaire dans le titre ou la description
            salary = ""
            sal_match = re.search(
                r"(\d[\d\s]*[\d])\s*[€$k][\s/]*(an|mois|ans)?",
                title + " " + desc, re.IGNORECASE
            )
            if sal_match:
                salary = sal_match.group(0).strip()

            link = tag("link") or tag("guid")

            jobs.append({
                "job_id"     : f"ind_{uuid.uuid4().hex[:10]}",
                "title"      : title,
                "company"    : company,
                "location"   : loc,
                "salary"     : salary,
                "description": desc,
                "url"        : link,
                "plateforme" : "indeed",
            })
    except ET.ParseError as e:
        log.debug(f"Indeed XML parse: {e}")
    return jobs


# ════════════════════════════════════════════════════════════════════════════════
#  2. MONSTER FRANCE — API JSON
# ════════════════════════════════════════════════════════════════════════════════

def fetch_monster(query: str, lieu: str = "", nb: int = 25) -> list[dict]:
    """Monster France — API de recherche JSON."""
    try:
        params = {
            "q"       : query,
            "where"   : lieu or "France",
            "stpage"  : 1,
            "page"    : nb,
        }
        r = session.get(
            "https://www.monster.fr/jobs/search/",
            params=params,
            headers={"Accept": "text/html,*/*"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []

        # Monster retourne du HTML — extraction JSON embarqué
        jobs = _extract_monster_json(r.text)
        return jobs

    except Exception as e:
        log.debug(f"Monster: {e}")
        return []


def _extract_monster_json(html: str) -> list[dict]:
    """Extrait le JSON embarqué dans la page Monster."""
    jobs = []
    try:
        # Monster injecte un objet JS window.__INITIAL_STATE__ ou JSON-LD
        pattern = re.compile(
            r'"jobId"\s*:\s*"([^"]+)".*?"jobTitle"\s*:\s*"([^"]+)".*?'
            r'"companyName"\s*:\s*"([^"]+)".*?"locationFullAddress"\s*:\s*"([^"]+)"',
            re.DOTALL
        )
        for m in pattern.finditer(html):
            jobs.append({
                "job_id"     : f"mon_{m.group(1)}",
                "title"      : m.group(2),
                "company"    : m.group(3),
                "location"   : m.group(4),
                "salary"     : "",
                "description": "",
                "url"        : f"https://www.monster.fr/emploi/recherche/?q={quote_plus(m.group(2))}",
                "plateforme" : "monster",
            })
        if not jobs:
            # Fallback : JSON-LD schema.org
            ld_blocks = re.findall(
                r'<script type="application/ld\+json">(.*?)</script>',
                html, re.DOTALL
            )
            import json as _json
            for block in ld_blocks:
                try:
                    data = _json.loads(block)
                    if isinstance(data, list):
                        items = data
                    elif data.get("@type") == "ItemList":
                        items = data.get("itemListElement", [])
                    else:
                        items = [data]
                    for item in items:
                        j = item.get("item", item)
                        if j.get("@type") == "JobPosting":
                            sal = ""
                            if j.get("baseSalary"):
                                v = j["baseSalary"].get("value", {})
                                sal = f"{v.get('minValue','')}-{v.get('maxValue','')} {v.get('unitText','')}"
                            jobs.append({
                                "job_id"     : f"mon_{uuid.uuid4().hex[:8]}",
                                "title"      : j.get("title", ""),
                                "company"    : j.get("hiringOrganization", {}).get("name", ""),
                                "location"   : j.get("jobLocation", {}).get("address", {}).get("addressLocality", ""),
                                "salary"     : sal,
                                "description": j.get("description", "")[:500],
                                "url"        : j.get("url", ""),
                                "plateforme" : "monster",
                            })
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"Monster JSON extract: {e}")
    return jobs


# ════════════════════════════════════════════════════════════════════════════════
#  3. WELCOME TO THE JUNGLE — Algolia API publique
# ════════════════════════════════════════════════════════════════════════════════

WTTJ_APP_ID  = "CSEKHVMS53"
WTTJ_API_KEY = "9ba3a54d7ac28f3fcc7ded22580eda68"

def fetch_wttj(query: str, lieu: str = "", nb: int = 40) -> list[dict]:
    """Welcome to the Jungle via Algolia (index public)."""
    try:
        filters = 'published:true'
        if lieu and "remote" not in lieu.lower():
            pass  # filtrage sur location_display_value

        payload = {
            "requests": [{
                "indexName": "wttj_jobs_production_fr",
                "params"   : urlencode({
                    "query"          : query,
                    "hitsPerPage"    : nb,
                    "page"           : 0,
                    "filters"        : filters,
                    "attributesToRetrieve": ",".join([
                        "name","organization","offices","salary_min","salary_max",
                        "contract_type","remote","slug","description_plaintext"
                    ]),
                }),
            }]
        }
        r = session.post(
            f"https://{WTTJ_APP_ID}-dsn.algolia.net/1/indexes/*/queries"
            f"?x-algolia-application-id={WTTJ_APP_ID}"
            f"&x-algolia-api-key={WTTJ_API_KEY}",
            json=payload,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log.debug(f"WTTJ Algolia HTTP {r.status_code}")
            return []

        hits = r.json().get("results", [{}])[0].get("hits", [])
        return [_normalize_wttj(h) for h in hits]

    except Exception as e:
        log.debug(f"WTTJ: {e}")
        return []


def _normalize_wttj(j: dict) -> dict:
    org  = j.get("organization", {}) or {}
    slug_org = org.get("slug", "")
    slug_job = j.get("slug", "")
    url = (
        f"https://www.welcometothejungle.com/fr/companies/{slug_org}/jobs/{slug_job}"
        if slug_org and slug_job else "https://www.welcometothejungle.com"
    )

    offices  = j.get("offices", []) or []
    location = ", ".join(
        o.get("city", "") for o in offices[:2] if o.get("city")
    ) or j.get("location", "")

    s_min = j.get("salary_min")
    s_max = j.get("salary_max")
    salary = (
        f"{s_min:,} – {s_max:,} €/an".replace(",", " ")
        if s_min and s_max else (f"À partir de {s_min:,} €/an".replace(",", " ") if s_min else "")
    )

    return {
        "job_id"     : f"wttj_{j.get('objectID', uuid.uuid4().hex[:8])}",
        "title"      : j.get("name", ""),
        "company"    : org.get("name", ""),
        "location"   : location,
        "salary"     : salary,
        "description": (j.get("description_plaintext") or "")[:500],
        "url"        : url,
        "plateforme" : "welcome_to_the_jungle",
    }


# ════════════════════════════════════════════════════════════════════════════════
#  4. CADREMPLOI — API publique
# ════════════════════════════════════════════════════════════════════════════════

def fetch_cadremploi(query: str, lieu: str = "", nb: int = 30) -> list[dict]:
    """Cadremploi — API Jobboard publique."""
    try:
        # Endpoint de recherche Cadremploi
        params = {
            "kw"    : query,
            "lw"    : lieu,
            "nb"    : nb,
            "p"     : 1,
            "tri"   : "date",
        }
        r = session.get(
            "https://www.cadremploi.fr/api/v1/editorial/recherche/offres",
            params=params,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            # Fallback endpoint alternatif
            return _fetch_cadremploi_v2(query, lieu, nb)

        data = r.json()
        offres = data.get("offres", data.get("results", []))
        return [_normalize_cadremploi(o) for o in offres]

    except Exception as e:
        log.debug(f"Cadremploi: {e}")
        return _fetch_cadremploi_v2(query, lieu, nb)


def _fetch_cadremploi_v2(query: str, lieu: str, nb: int) -> list[dict]:
    """Fallback Cadremploi via flux RSS."""
    try:
        params = {"q": query, "l": lieu}
        r = session.get(
            "https://www.cadremploi.fr/rss?" + urlencode({k: v for k, v in params.items() if v}),
            headers={"Accept": "application/rss+xml, */*"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return _parse_rss_generic(r.text, "cadremploi", "https://www.cadremploi.fr")
    except Exception as e:
        log.debug(f"Cadremploi RSS: {e}")
        return []


def _normalize_cadremploi(o: dict) -> dict:
    sal = o.get("salaire", o.get("salary", o.get("remuneration", "")))
    if isinstance(sal, dict):
        sal = sal.get("libelle", sal.get("label", ""))

    return {
        "job_id"     : f"cad_{o.get('id', o.get('reference', uuid.uuid4().hex[:8]))}",
        "title"      : o.get("titre", o.get("title", o.get("intitule", ""))),
        "company"    : (o.get("entreprise") or {}).get("nom", o.get("company", "")),
        "location"   : o.get("lieu", o.get("location", o.get("localisation", ""))),
        "salary"     : str(sal),
        "description": o.get("description", o.get("texte", ""))[:500],
        "url"        : o.get("url", o.get("link", o.get("lien", ""))),
        "plateforme" : "cadremploi",
    }


# ════════════════════════════════════════════════════════════════════════════════
#  5. APEC — API publique (cadres & managers)
# ════════════════════════════════════════════════════════════════════════════════

def fetch_apec(query: str, lieu: str = "", nb: int = 50) -> list[dict]:
    """APEC — offres pour cadres, API sans authentification."""
    try:
        body = {
            "motsCles"           : query,
            "lieuTravail"        : lieu,
            "typesContrat"       : ["CDI", "CDD", "Freelance/Portage"],
            "nombreOffresParPage": nb,
            "page"               : 0,
            "tri"                : 0,
        }
        r = session.post(
            "https://www.apec.fr/cms/webservices/rechercheOffre/resultatRecherche",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return _fetch_apec_get(query, lieu, nb)

        data   = r.json()
        offres = data.get("resultats", data.get("listeOffres", []))
        return [_normalize_apec(o) for o in offres]

    except Exception as e:
        log.debug(f"APEC POST: {e}")
        return _fetch_apec_get(query, lieu, nb)


def _fetch_apec_get(query: str, lieu: str, nb: int) -> list[dict]:
    try:
        params = {
            "motsCles"           : query,
            "lieuTravail"        : lieu,
            "nombreOffresParPage": nb,
        }
        r = session.get(
            "https://www.apec.fr/cms/webservices/rechercheOffre/resultatRecherche",
            params={k: v for k, v in params.items() if v},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        data   = r.json()
        offres = data.get("resultats", data.get("listeOffres", []))
        return [_normalize_apec(o) for o in offres]
    except Exception as e:
        log.debug(f"APEC GET: {e}")
        return []


def _normalize_apec(o: dict) -> dict:
    sal = ""
    if o.get("salaireMin") and o.get("salaireMax"):
        sal = f"{o['salaireMin']} – {o['salaireMax']} K€/an"
    elif o.get("libelleSalaire"):
        sal = o["libelleSalaire"]

    num = o.get("numeroOffre", "")
    return {
        "job_id"     : f"apec_{num or uuid.uuid4().hex[:8]}",
        "title"      : o.get("intitulePoste", o.get("titre", "")),
        "company"    : o.get("nomEntreprise", ""),
        "location"   : o.get("lieuTravail", o.get("lieu", "")),
        "salary"     : sal,
        "description": o.get("texteOffre", o.get("description", ""))[:500],
        "url"        : f"https://www.apec.fr/candidat/recherche-emploi.html/emploi/{num}" if num else "",
        "plateforme" : "apec",
    }


# ════════════════════════════════════════════════════════════════════════════════
#  6. HELLOWORK — Agrégateur français
# ════════════════════════════════════════════════════════════════════════════════

def fetch_hellowork(query: str, lieu: str = "", nb: int = 30) -> list[dict]:
    """HelloWork — API publique."""
    try:
        params = {
            "q"   : query,
            "l"   : lieu,
            "d"   : 40,
            "nb"  : nb,
            "p"   : 1,
        }
        r = session.get(
            "https://www.hellowork.com/fr-fr/offres-emploi.html",
            params={k: v for k, v in params.items() if v},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        # Extraction JSON embarqué dans le HTML
        return _extract_jsonld_jobs(r.text, "hellowork")
    except Exception as e:
        log.debug(f"HelloWork: {e}")
        return []


# ════════════════════════════════════════════════════════════════════════════════
#  7. FRANCE TRAVAIL (ex Pôle Emploi) — API officielle gratuite
#  Inscription : https://francetravail.io/data/api/offres-emploi
# ════════════════════════════════════════════════════════════════════════════════

def fetch_france_travail(query: str, lieu: str = "",
                         client_id: str = "", client_secret: str = "") -> list[dict]:
    if not client_id or not client_secret:
        return []
    try:
        token_r = session.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
            data={
                "grant_type"   : "client_credentials",
                "client_id"    : client_id,
                "client_secret": client_secret,
                "scope"        : "api_offresdemploiv2 o2dsoffre",
            },
            timeout=TIMEOUT,
        )
        token = token_r.json().get("access_token", "")
        if not token:
            return []

        params = {"motsCles": query, "range": "0-49"}
        if lieu:
            params["commune"] = lieu

        r = session.get(
            "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return [_normalize_ft(o) for o in r.json().get("resultats", [])]
    except Exception as e:
        log.debug(f"France Travail: {e}")
        return []


def _normalize_ft(o: dict) -> dict:
    sal = (o.get("salaire") or {}).get("libelle", "")
    return {
        "job_id"     : f"ft_{o.get('id', uuid.uuid4().hex[:8])}",
        "title"      : o.get("intitule", ""),
        "company"    : (o.get("entreprise") or {}).get("nom", ""),
        "location"   : (o.get("lieuTravail") or {}).get("libelle", ""),
        "salary"     : sal,
        "description": o.get("description", "")[:500],
        "url"        : (o.get("origineOffre") or {}).get("urlOrigine", ""),
        "plateforme" : "france_travail",
    }


# ════════════════════════════════════════════════════════════════════════════════
#  UTILITAIRES
# ════════════════════════════════════════════════════════════════════════════════

def _parse_rss_generic(xml_text: str, source: str, base_url: str) -> list[dict]:
    """Parse un flux RSS générique."""
    jobs = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.findall(".//item"):
            def tag(n):
                el = item.find(n)
                return (el.text or "").strip() if el is not None else ""
            desc = re.sub(r"<[^>]+>", " ", tag("description")).strip()[:400]
            jobs.append({
                "job_id"     : f"{source}_{uuid.uuid4().hex[:8]}",
                "title"      : tag("title"),
                "company"    : "",
                "location"   : "",
                "salary"     : "",
                "description": desc,
                "url"        : tag("link") or tag("guid"),
                "plateforme" : source,
            })
    except Exception:
        pass
    return jobs


def _extract_jsonld_jobs(html: str, source: str) -> list[dict]:
    """Extrait les offres depuis les blocs JSON-LD (schema.org/JobPosting)."""
    import json as _json
    jobs = []
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = _json.loads(block)
            items = data if isinstance(data, list) else [data]
            for it in items:
                if it.get("@type") == "JobPosting":
                    sal = ""
                    bs  = it.get("baseSalary", {})
                    if bs:
                        v   = bs.get("value", {})
                        sal = f"{v.get('minValue','')}-{v.get('maxValue','')} {v.get('unitText','')}"
                    loc = it.get("jobLocation", {})
                    if isinstance(loc, list): loc = loc[0]
                    city = (loc.get("address") or {}).get("addressLocality", "")
                    jobs.append({
                        "job_id"     : f"{source}_{uuid.uuid4().hex[:8]}",
                        "title"      : it.get("title", ""),
                        "company"    : (it.get("hiringOrganization") or {}).get("name", ""),
                        "location"   : city,
                        "salary"     : sal,
                        "description": re.sub(r"<[^>]+>", " ", it.get("description", ""))[:500],
                        "url"        : it.get("url", ""),
                        "plateforme" : source,
                    })
        except Exception:
            pass
    return jobs


# ════════════════════════════════════════════════════════════════════════════════
#  MOTEUR PRINCIPAL — Recherche multi-sources en parallèle
# ════════════════════════════════════════════════════════════════════════════════

SOURCES = {
    "indeed"             : fetch_indeed,
    "monster"            : fetch_monster,
    "welcome_to_the_jungle": fetch_wttj,
    "cadremploi"         : fetch_cadremploi,
    "apec"               : fetch_apec,
    "hellowork"          : fetch_hellowork,
}


def search_french_boards(
    postes      : list[str],
    villes      : list[str] = None,
    remote      : bool      = True,
    ft_client_id: str       = "",
    ft_secret   : str       = "",
) -> list[dict]:
    """
    Recherche en parallèle sur tous les job boards français.
    Retourne les offres dédupliquées toutes sources confondues.
    """
    villes  = villes or ["Paris"]
    results = []
    seen    = set()

    queries = postes if postes else ["emploi"]
    lieux   = list(villes[:3])
    if remote:
        lieux.append("télétravail")

    def run(source_name, fn, query, lieu):
        try:
            jobs = fn(query, lieu)
            log.info(f"  {source_name:25} '{query}' @ {lieu or 'France'} → {len(jobs)} offres")
            return jobs
        except Exception as e:
            log.debug(f"  {source_name} erreur: {e}")
            return []

    tasks = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for query in queries:
            for lieu in lieux:
                for name, fn in SOURCES.items():
                    tasks.append(pool.submit(run, name, fn, query, lieu))

        # France Travail (si clé configurée)
        if ft_client_id and ft_secret:
            for query in queries:
                for lieu in lieux:
                    tasks.append(pool.submit(
                        run, "france_travail",
                        lambda q, l: fetch_france_travail(q, l, ft_client_id, ft_secret),
                        query, lieu
                    ))

        for future in as_completed(tasks):
            for job in (future.result() or []):
                jid = job.get("job_id", "")
                if jid and jid not in seen:
                    seen.add(jid)
                    results.append(job)

    log.info(f"🇫🇷 Total FR boards : {len(results)} offres uniques")
    return results


# ── Test standalone ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "Product Manager"
    l = sys.argv[2] if len(sys.argv) > 2 else "Paris"

    print(f"\n🔍 Recherche : '{q}' @ {l}\n")
    jobs = search_french_boards(postes=[q], villes=[l], remote=True)

    by_source = {}
    for j in jobs:
        src = j["plateforme"]
        by_source.setdefault(src, []).append(j)

    print(f"{'Source':<25} {'Offres':>6}")
    print("─" * 33)
    for src, lst in sorted(by_source.items(), key=lambda x: -len(x[1])):
        print(f"{src:<25} {len(lst):>6}")
    print("─" * 33)
    print(f"{'TOTAL':<25} {len(jobs):>6}\n")

    for j in jobs[:10]:
        print(f"[{j['plateforme'].upper():<20}] {j['title'][:45]}")
        print(f"  {j['company'][:30]} | {j['location']} | {j['salary']}")
        print(f"  {j['url'][:70]}")
        print()
