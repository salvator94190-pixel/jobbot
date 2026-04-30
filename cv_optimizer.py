"""
JobBot — Optimiseur CV & Lettres (Anti-détection IA + ATS)
===========================================================

Ce module fait 4 choses :

  1. HUMANISATION      — Réécrit le texte pour casser les patterns IA
                         (phrases trop parfaites, vocabulaire générique,
                          structure trop symétrique, absence d'imperfections
                          stylistiques naturelles).

  2. OPTIMISATION ATS  — Injecte les mots-clés exacts de la fiche de poste
                         dans le CV/lettre pour passer les filtres automatiques.

  3. FORMAT ATS-SAFE   — Génère un fichier Word (.docx) sans tableaux, sans
                         colonnes, sans images — les parsers ATS lisent ça
                         parfaitement là où ils ratent les PDFs complexes.

  4. SCORE DÉTECTION   — Évalue le risque de détection IA avant/après
                         traitement (simulation locale, pas d'API tierce).
"""

import os
import re
import json
import logging
import anthropic
from pathlib import Path

log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ════════════════════════════════════════════════════════════════════════════════
#  1. DÉTECTEUR IA LOCAL (score heuristique)
#     Analyse les patterns statistiquement associés aux textes IA
# ════════════════════════════════════════════════════════════════════════════════

# Formules ultra-génériques que les LLMs sur-utilisent
AI_PATTERNS = [
    # Formules d'ouverture stéréotypées
    r"je suis (très |profondément |)convaincu",
    r"passionné par (le |la |les |)",
    r"fort(e?) d[e']une expérience",
    r"doté(e?) d[e']une (solide |grande |)",
    r"ma passion pour",
    r"je suis (particulièrement |)enthousiaste",
    r"c'est avec (grand |beaucoup d'|)enthousiasme",
    r"je me permets de",
    r"dans le cadre de",
    r"suite à votre annonce",
    r"je me tiens à votre disposition",
    r"dans l'attente de votre",
    r"veuillez agréer",
    r"en vous souhaitant bonne",
    r"je serais ravi(e?) de",
    r"n'hésitez pas à",

    # Adjectifs génériques sur-utilisés
    r"\bdynamique\b",
    r"\brigoureux\b|\brigoureus",
    r"\bpolyvalent\b|\bpolyvalente\b",
    r"\bproactif\b|\bproactive\b",
    r"\bsynergie\b",
    r"\bstratégique(ment)?\b",
    r"\boptimiser\b|\boptimisation\b",
    r"\brésilience\b|\bresilience\b",
    r"\bleadership\b.*\bleadership\b",   # répété 2x
    r"\bvaleur ajoutée\b",
    r"\bexcellence\b.*\bexcellence\b",

    # Structure trop parfaite
    r"(premièrement|deuxièmement|troisièmement)",
    r"(primo|secundo|tertio)",
    r"d'une part[,\.].*d'autre part",
    r"non seulement.*mais (aussi|également)",

    # Longueur de phrases uniforme (détecté par variance faible)
]

AI_PATTERN_RE = [re.compile(p, re.IGNORECASE) for p in AI_PATTERNS]

# Marqueurs de texte humain authentique
HUMAN_MARKERS = [
    r"\bj'ai\b",
    r"\bj'ai eu\b",
    r"\bon m'a\b",
    r"\bfranchement\b|\bhonnêtement\b",
    r"\ben fait\b",
    r"\bdu coup\b",
    r"\bconcrètement\b",
    r"\bpar exemple\b",
    r"\bnotamment\b.*\bnotamment\b",   # répétition naturelle
    r"\bun peu\b|\bassez\b|\bplutôt\b",
    r"\bje pense que\b|\bselon moi\b|\bà mon sens\b",
    r"\bj'ai appris\b|\bj'ai découvert\b|\bj'ai réalisé\b",
]

HUMAN_MARKER_RE = [re.compile(p, re.IGNORECASE) for p in HUMAN_MARKERS]


def score_ai_detection(text: str) -> dict:
    """
    Estime le risque de détection IA sur un texte.
    Retourne un dict avec score (0-100, 100 = clairement IA)
    et les patterns détectés.
    """
    if not text:
        return {"score": 0, "niveau": "N/A", "patterns": [], "human_markers": 0}

    words     = text.split()
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]

    # ── Détection des patterns IA ─────────────────────────────────────────────
    detected = []
    for i, regex in enumerate(AI_PATTERN_RE):
        matches = regex.findall(text)
        if matches:
            detected.append(AI_PATTERNS[i][:50])

    # ── Marqueurs humains ─────────────────────────────────────────────────────
    human_count = sum(1 for r in HUMAN_MARKER_RE if r.search(text))

    # ── Variance des longueurs de phrases (les IA ont tendance à uniformiser) ─
    if len(sentences) > 3:
        lengths = [len(s.split()) for s in sentences]
        avg = sum(lengths) / len(lengths)
        variance = sum((l - avg) ** 2 for l in lengths) / len(lengths)
        # Variance faible → texte IA (phrases régulières)
        variance_penalty = max(0, 30 - int(variance / 2))
    else:
        variance_penalty = 0

    # ── Densité de mots génériques ────────────────────────────────────────────
    generic_words = ["dynamique", "rigoureux", "polyvalent", "proactif",
                     "synergies", "optimisation", "excellence", "leadership",
                     "stratégique", "innovant", "performant", "efficace"]
    generic_count = sum(
        text.lower().count(w) for w in generic_words
    )
    generic_density = min(40, generic_count * 5)

    # ── Score final ───────────────────────────────────────────────────────────
    pattern_score  = min(50, len(detected) * 8)
    human_bonus    = min(20, human_count * 4)
    raw_score      = pattern_score + variance_penalty + generic_density - human_bonus
    score          = max(0, min(100, raw_score))

    if score >= 70:
        niveau = "🔴 Élevé — détectable"
    elif score >= 40:
        niveau = "🟡 Moyen — à humaniser"
    elif score >= 20:
        niveau = "🟢 Faible — acceptable"
    else:
        niveau = "✅ Très faible — naturel"

    return {
        "score"         : score,
        "niveau"        : niveau,
        "patterns"      : detected[:8],
        "human_markers" : human_count,
        "variance_phrases": round(variance if len(sentences) > 3 else 0, 1),
    }


# ════════════════════════════════════════════════════════════════════════════════
#  2. HUMANISEUR DE TEXTE
#     Réécrit avec Claude en simulant un style humain authentique
# ════════════════════════════════════════════════════════════════════════════════

HUMANIZE_SYSTEM = """Tu es un expert en rédaction humaine authentique.
Ta mission : réécrire un texte professionnel pour qu'il semble écrit par un humain,
pas par une IA. Il FAUT impérativement :

STYLISTIQUE :
- Varier TRÈS fortement la longueur des phrases (alterner courtes/longues/moyennes)
- Utiliser des constructions grammaticales variées et parfois inattendues
- Intégrer des connecteurs naturels : "du coup", "en fait", "concrètement", "d'ailleurs"
- Ajouter des nuances et légères imperfections stylistiques (comme un humain)
- Éviter toute symétrie rhétorique parfaite (pas de "d'une part / d'autre part")
- Proscrire les listes trop parfaites ou trop symétriques
- Mélanger parfois le formel et le légèrement familier (selon le contexte)

VOCABULAIRE :
- Bannir totalement : "dynamique", "rigoureux", "proactif", "synergies",
  "valeur ajoutée", "passionné par", "je me permets", "dans l'attente de",
  "veuillez agréer", "doté d'une solide expérience", "fort d'une expérience"
- Remplacer par des formulations concrètes et personnelles
- Utiliser des verbes d'action précis plutôt que des adjectifs génériques
- Parler de faits concrets, de chiffres, d'expériences réelles

STRUCTURE :
- Pas de structure trop parfaite en 3 paragraphes égaux
- Varier le rythme : parfois une phrase courte seule pour l'impact
- Commencer parfois par une observation ou un fait plutôt que "je"
- Terminer de façon naturelle, pas avec une formule rituelle

AUTHENTICITÉ :
- Laisser transparaître une vraie personnalité
- Inclure une ou deux références à des expériences spécifiques
- Exprimer une opinion ou un point de vue personnel sur le secteur
- Le texte doit sonner comme une vraie personne qui parle de son parcours

Retourne UNIQUEMENT le texte réécrit, sans commentaires."""


def humanize_text(text: str, context: str = "lettre de motivation") -> str:
    """
    Réécrit un texte pour le rendre indétectable par les outils de détection IA.
    context : "lettre de motivation" | "cv_section" | "cv_resume"
    """
    if not ANTHROPIC_KEY or not text:
        return text

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=HUMANIZE_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"""Contexte : {context}

Texte à humaniser :
---
{text}
---

Réécris ce texte en respectant toutes les consignes.
Conserve le sens et les informations, change le style pour qu'il soit
indiscernable d'un texte écrit par un humain.
Ne pas utiliser de balises markdown dans la réponse."""
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Humanisation: {e}")
        return text


# ════════════════════════════════════════════════════════════════════════════════
#  3. OPTIMISEUR ATS — injection de mots-clés
# ════════════════════════════════════════════════════════════════════════════════

def extract_ats_keywords(job_description: str) -> list[str]:
    """
    Extrait les mots-clés ATS importants d'une fiche de poste :
    compétences techniques, outils, certifications, soft skills demandés.
    """
    if not ANTHROPIC_KEY or not job_description:
        return []

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": f"""Extrait les mots-clés ATS de cette fiche de poste.
Retourne UNIQUEMENT un JSON : {{"keywords": ["mot1","mot2",...]}}
Inclure : compétences techniques, outils, logiciels, certifications, langues,
méthodes de travail, intitulés de poste mentionnés.
Maximum 25 mots-clés, par ordre d'importance.

Fiche de poste :
{job_description[:2000]}"""
            }]
        )
        raw = msg.content[0].text.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0:
            return json.loads(raw[start:end]).get("keywords", [])
    except Exception as e:
        log.debug(f"Extraction keywords: {e}")

    # Fallback heuristique
    return _extract_keywords_heuristic(job_description)


def _extract_keywords_heuristic(text: str) -> list[str]:
    """Extraction heuristique basique si pas de clé API."""
    # Mots courants à ignorer
    stopwords = {
        "le","la","les","un","une","des","de","du","et","ou","en","dans","pour",
        "avec","sur","par","que","qui","est","sont","nous","vous","ils","leur",
        "notre","votre","être","avoir","faire","plus","tout","cette","votre"
    }
    words     = re.findall(r'\b[a-zA-ZÀ-ÿ]{3,}\b', text)
    freq      = {}
    for w in words:
        w_lower = w.lower()
        if w_lower not in stopwords:
            freq[w_lower] = freq.get(w_lower, 0) + 1

    # Garde les plus fréquents + noms propres (outils, frameworks)
    keywords = sorted(freq.items(), key=lambda x: -x[1])
    return [k for k, _ in keywords[:20]]


def optimize_for_ats(text: str, keywords: list[str], cv_text: str = "") -> str:
    """
    Réécrit le texte en intégrant naturellement les mots-clés ATS manquants.
    Les mots-clés sont intégrés de façon fluide, pas mécaniquement.
    """
    if not ANTHROPIC_KEY or not text or not keywords:
        return text

    # Trouve quels mots-clés sont déjà présents
    present = [kw for kw in keywords if kw.lower() in text.lower()]
    missing = [kw for kw in keywords if kw.lower() not in text.lower()]

    if not missing:
        return text  # Tous les mots-clés sont déjà là

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{
                "role": "user",
                "content": f"""Tu es un expert en optimisation de CV pour les ATS (Applicant Tracking Systems).

Réécris ce texte en intégrant NATURELLEMENT les mots-clés manquants.
Les mots-clés doivent apparaître de façon fluide dans le texte, jamais comme une liste.
Conserve le sens et le ton. Ne force pas les mots-clés — adapte les phrases pour
qu'ils s'intègrent organiquement.

TEXTE ORIGINAL :
{text}

MOTS-CLÉS DÉJÀ PRÉSENTS (ne pas dupliquer) :
{', '.join(present[:10])}

MOTS-CLÉS À INTÉGRER (par ordre de priorité) :
{', '.join(missing[:12])}

Retourne UNIQUEMENT le texte optimisé, sans commentaires."""
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Optimisation ATS: {e}")
        return text


# ════════════════════════════════════════════════════════════════════════════════
#  4. PIPELINE COMPLET : Humanise + Optimise ATS
# ════════════════════════════════════════════════════════════════════════════════

def process_cover_letter(
    lettre      : str,
    job_description: str = "",
    cv_text     : str = "",
    user_name   : str = "",
) -> dict:
    """
    Pipeline complet pour une lettre de motivation :
      1. Score IA initial
      2. Humanisation
      3. Injection mots-clés ATS
      4. Score IA final
    Retourne le texte optimisé + les métriques.
    """
    if not lettre:
        return {"text": "", "score_avant": 0, "score_apres": 0, "keywords": []}

    # Score initial
    score_avant = score_ai_detection(lettre)

    # Étape 1 : Humanisation
    log.info("  Humanisation de la lettre...")
    lettre_humanisee = humanize_text(lettre, context="lettre de motivation")

    # Étape 2 : Optimisation ATS
    keywords = []
    if job_description:
        log.info("  Extraction mots-clés ATS...")
        keywords = extract_ats_keywords(job_description)
        if keywords:
            log.info(f"  Injection de {len(keywords)} mots-clés...")
            lettre_humanisee = optimize_for_ats(lettre_humanisee, keywords, cv_text)

    # Score final
    score_apres = score_ai_detection(lettre_humanisee)

    return {
        "text"          : lettre_humanisee,
        "score_avant"   : score_avant,
        "score_apres"   : score_apres,
        "keywords_ats"  : keywords,
        "gain"          : score_avant["score"] - score_apres["score"],
    }


def process_cv_section(section_text: str, section_name: str,
                       job_description: str = "") -> dict:
    """
    Optimise une section du CV (accroche, expériences, compétences).
    """
    if not section_text:
        return {"text": section_text, "score_avant": 0, "score_apres": 0}

    score_avant = score_ai_detection(section_text)

    # Humanisation adaptée au type de section
    context_map = {
        "accroche"    : "résumé de profil sur un CV",
        "expériences" : "description d'expérience professionnelle sur un CV",
        "compétences" : "liste de compétences sur un CV",
        "formation"   : "description de formation sur un CV",
    }
    context = context_map.get(section_name.lower(), "section de CV")
    optimized = humanize_text(section_text, context=context)

    # ATS si description fournie
    keywords = []
    if job_description:
        keywords = extract_ats_keywords(job_description)
        if keywords:
            optimized = optimize_for_ats(optimized, keywords)

    score_apres = score_ai_detection(optimized)

    return {
        "text"       : optimized,
        "score_avant": score_avant,
        "score_apres": score_apres,
        "keywords"   : keywords,
    }


# ════════════════════════════════════════════════════════════════════════════════
#  5. ADAPTATEUR CV PAR OFFRE
#     Réécrit les sections du CV pour coller exactement à l'offre
# ════════════════════════════════════════════════════════════════════════════════

CV_ADAPT_SYSTEM = """Tu es un expert en rédaction de CV et en stratégie de candidature.
Ta mission : adapter un CV existant pour maximiser ses chances sur une offre précise,
tout en restant 100% fidèle aux expériences réelles du candidat.

RÈGLES ABSOLUES :
- Ne jamais inventer d'expériences, de compétences ou de diplômes qui n'existent pas
- Adapter = reformuler, réordonner, mettre en valeur — jamais mentir
- Chaque affirmation doit être vérifiable et cohérente avec le CV original
- Conserver toutes les dates, entreprises et intitulés de poste réels

CE QUE TU PEUX FAIRE :
- Reformuler les bullet points pour utiliser les mots-clés de l'offre
- Mettre en avant les expériences les plus pertinentes pour ce poste
- Réécrire l'accroche/résumé pour coller au profil recherché
- Quantifier les résultats là où c'est possible (si des chiffres existent dans le CV)
- Réordonner les compétences par ordre de pertinence pour l'offre
- Adapter le vocabulaire technique (ex: "gestion de projet" → "Project Management" si l'offre est en anglais)

STYLE :
- Bullet points courts et percutants (verbe d'action + résultat)
- Verbes forts : "piloté", "déployé", "optimisé", "généré", "réduit", "augmenté"
- Éviter les adjectifs génériques (dynamique, rigoureux, etc.)
- Chiffres et métriques quand disponibles"""


def adapt_cv_for_job(
    cv_text       : str,
    job_title     : str,
    job_description: str,
    user_name     : str = "",
) -> dict:
    """
    Adapte le CV complet pour une offre spécifique.
    Retourne :
      - cv_adapte      : texte du CV adapté (markdown structuré)
      - sections       : dict des sections adaptées
      - score_avant    : score IA du CV original
      - score_apres    : score IA du CV adapté
      - keywords_injectes : liste des mots-clés ajoutés
      - resume_adapte  : nouvelle accroche personnalisée
    """
    if not ANTHROPIC_KEY or not cv_text:
        return {"cv_adapte": cv_text, "resume_adapte": "", "keywords_injectes": []}

    # Score IA initial du CV
    score_avant = score_ai_detection(cv_text)

    # Extraction des mots-clés ATS de l'offre
    keywords = extract_ats_keywords(job_description) if job_description else []

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=CV_ADAPT_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"""Adapte ce CV pour l'offre suivante.

POSTE VISÉ : {job_title}

DESCRIPTION DE L'OFFRE :
{job_description[:2000]}

CV ORIGINAL DU CANDIDAT ({user_name}) :
{cv_text[:4000]}

MOTS-CLÉS ATS PRIORITAIRES À INTÉGRER :
{', '.join(keywords[:15])}

Retourne UNIQUEMENT un JSON valide avec cette structure :
{{
  "resume_adapte": "<accroche personnalisée de 3-4 lignes pour ce poste>",
  "experience_adaptee": "<section expériences reformulée avec bullet points percutants>",
  "competences_adaptees": "<compétences réorganisées par pertinence pour ce poste>",
  "cv_complet_adapte": "<CV complet adapté en markdown structuré>",
  "keywords_injectes": ["<mot-clé1>", "<mot-clé2>"],
  "modifications": ["<modification1 faite>", "<modification2 faite>"]
}}"""
            }]
        )

        raw = msg.content[0].text.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
        else:
            data = {}

        cv_adapte = data.get("cv_complet_adapte", cv_text)

        # Humanisation du CV adapté
        if cv_adapte and cv_adapte != cv_text:
            cv_adapte = humanize_text(cv_adapte, context="cv_section")

        score_apres = score_ai_detection(cv_adapte)

        return {
            "cv_adapte"        : cv_adapte,
            "resume_adapte"    : data.get("resume_adapte", ""),
            "experience"       : data.get("experience_adaptee", ""),
            "competences"      : data.get("competences_adaptees", ""),
            "keywords_injectes": data.get("keywords_injectes", keywords[:8]),
            "modifications"    : data.get("modifications", []),
            "score_avant"      : score_avant,
            "score_apres"      : score_apres,
            "gain"             : score_avant["score"] - score_apres["score"],
        }

    except Exception as e:
        log.error(f"Adaptation CV: {e}")
        return {"cv_adapte": cv_text, "resume_adapte": "", "keywords_injectes": keywords}


# ════════════════════════════════════════════════════════════════════════════════
#  6. GÉNÉRATEUR DOCX ATS-SAFE
#     Produit un fichier Word lisible par tous les ATS (pas de tableaux,
#     pas de colonnes, pas d'images, structure linéaire)
# ════════════════════════════════════════════════════════════════════════════════

def generate_cv_docx(
    cv_text  : str,
    user_name: str,
    job_title: str,
    output_path: Path,
) -> bool:
    """
    Génère un DOCX ATS-safe à partir du texte du CV adapté.
    Utilise docx.js via Node.js.
    Retourne True si succès.
    """
    import subprocess
    import tempfile

    # Prépare le script Node.js
    script = _build_docx_script(cv_text, user_name, job_title, str(output_path))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs",
                                     delete=False, encoding="utf-8") as f:
        f.write(script)
        script_path = f.name

    try:
        # Installe docx si nécessaire
        subprocess.run(["npm", "install", "-g", "docx"],
                       capture_output=True, timeout=60)

        result = subprocess.run(
            ["node", script_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info(f"DOCX généré : {output_path}")
            return True
        else:
            log.error(f"DOCX erreur: {result.stderr[:200]}")
            return False
    except Exception as e:
        log.error(f"DOCX génération: {e}")
        return False
    finally:
        Path(script_path).unlink(missing_ok=True)


def _build_docx_script(cv_text: str, user_name: str,
                        job_title: str, output_path: str) -> str:
    """Génère le script Node.js pour créer le DOCX."""

    # Parse les sections markdown du CV
    lines = cv_text.split("\n")

    # Échappe le JSON pour l'injection dans JS
    escaped_text = json.dumps(cv_text)
    escaped_name = json.dumps(user_name)
    escaped_job  = json.dumps(job_title)
    escaped_out  = json.dumps(output_path)

    return f"""
import {{ Document, Packer, Paragraph, TextRun, HeadingLevel,
          AlignmentType, LevelFormat, BorderStyle }} from 'docx';
import fs from 'fs';

const cvText = {escaped_text};
const userName = {escaped_name};
const jobTitle = {escaped_job};
const outputPath = {escaped_out};

// Parse le CV en sections
function parseCV(text) {{
  const lines = text.split('\\n');
  const sections = [];
  let current = null;

  for (const raw of lines) {{
    const line = raw.trim();
    if (!line) {{ continue; }}

    if (line.startsWith('# ') || line.startsWith('## ')) {{
      current = {{ heading: line.replace(/^#+\\s*/, ''), items: [] }};
      sections.push(current);
    }} else if (line.startsWith('- ') || line.startsWith('• ')) {{
      if (current) current.items.push({{ type: 'bullet', text: line.replace(/^[-•]\\s*/, '') }});
    }} else {{
      if (current) current.items.push({{ type: 'text', text: line }});
      else sections.push({{ heading: null, items: [{{ type: 'text', text: line }}] }});
    }}
  }}
  return sections;
}}

const sections = parseCV(cvText);
const children = [];

// En-tête nom + poste
children.push(new Paragraph({{
  alignment: AlignmentType.CENTER,
  spacing: {{ after: 120 }},
  children: [new TextRun({{ text: userName, bold: true, size: 36, font: 'Arial' }})]
}}));

children.push(new Paragraph({{
  alignment: AlignmentType.CENTER,
  spacing: {{ after: 240 }},
  border: {{ bottom: {{ style: BorderStyle.SINGLE, size: 6, color: '2E74B5', space: 4 }} }},
  children: [new TextRun({{ text: jobTitle, size: 24, color: '2E74B5', font: 'Arial' }})]
}}));

// Sections du CV
const numbering = {{
  config: [{{
    reference: 'bullets',
    levels: [{{
      level: 0,
      format: LevelFormat.BULLET,
      text: '•',
      alignment: AlignmentType.LEFT,
      style: {{ paragraph: {{ indent: {{ left: 720, hanging: 360 }} }} }}
    }}]
  }}]
}};

for (const section of sections) {{
  if (section.heading) {{
    children.push(new Paragraph({{
      heading: HeadingLevel.HEADING_2,
      spacing: {{ before: 240, after: 120 }},
      children: [new TextRun({{ text: section.heading.toUpperCase(), bold: true, size: 24, font: 'Arial' }})]
    }}));
  }}
  for (const item of section.items) {{
    if (item.type === 'bullet') {{
      children.push(new Paragraph({{
        numbering: {{ reference: 'bullets', level: 0 }},
        spacing: {{ after: 60 }},
        children: [new TextRun({{ text: item.text, size: 20, font: 'Arial' }})]
      }}));
    }} else {{
      children.push(new Paragraph({{
        spacing: {{ after: 80 }},
        children: [new TextRun({{ text: item.text, size: 20, font: 'Arial' }})]
      }}));
    }}
  }}
}}

const doc = new Document({{
  numbering,
  styles: {{
    default: {{ document: {{ run: {{ font: 'Arial', size: 20 }} }} }},
    paragraphStyles: [{{
      id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal',
      run: {{ size: 24, bold: true, font: 'Arial', color: '2E74B5' }},
      paragraph: {{ spacing: {{ before: 240, after: 120 }}, outlineLevel: 1 }}
    }}]
  }},
  sections: [{{
    properties: {{
      page: {{
        size: {{ width: 11906, height: 16838 }},
        margin: {{ top: 1134, right: 1134, bottom: 1134, left: 1134 }}
      }}
    }},
    children
  }}]
}});

Packer.toBuffer(doc).then(buf => {{
  fs.writeFileSync(outputPath, buf);
  console.log('OK: ' + outputPath);
}}).catch(e => {{
  console.error('FAIL: ' + e.message);
  process.exit(1);
}});
"""


# ════════════════════════════════════════════════════════════════════════════════
#  7. PIPELINE COMPLET CV + LETTRE
# ════════════════════════════════════════════════════════════════════════════════

def process_application(
    cv_text       : str,
    job           : dict,
    user_name     : str,
    cv_docx_dir   : Path = None,
) -> dict:
    """
    Pipeline complet pour une candidature :
      1. Adaptation du CV à l'offre
      2. Génération + humanisation de la lettre
      3. Optimisation ATS des deux
      4. Génération du DOCX ATS-safe
    Retourne tout pour la validation utilisateur.
    """
    job_title = job.get("title", "")
    job_desc  = job.get("description", "")

    result = {
        "cv_adapte"         : cv_text,
        "cv_resume"         : "",
        "cv_keywords"       : [],
        "cv_modifications"  : [],
        "cv_score_avant"    : 0,
        "cv_score_apres"    : 0,
        "cv_docx_path"      : "",
        "lettre_brute"      : "",
        "lettre_optimisee"  : "",
        "lettre_score_avant": 0,
        "lettre_score_apres": 0,
        "lettre_keywords"   : [],
    }

    # ── Adaptation CV ─────────────────────────────────────────────────────────
    if cv_text:
        log.info(f"  📄 Adaptation CV pour '{job_title}'...")
        cv_result = adapt_cv_for_job(cv_text, job_title, job_desc, user_name)
        result["cv_adapte"]       = cv_result.get("cv_adapte", cv_text)
        result["cv_resume"]       = cv_result.get("resume_adapte", "")
        result["cv_keywords"]     = cv_result.get("keywords_injectes", [])
        result["cv_modifications"]= cv_result.get("modifications", [])
        result["cv_score_avant"]  = cv_result.get("score_avant", {}).get("score", 0)
        result["cv_score_apres"]  = cv_result.get("score_apres", {}).get("score", 0)

        # Génération DOCX
        if cv_docx_dir:
            safe = re.sub(r'[^\w]', '_', f"{user_name}_{job_title}")[:50]
            docx_path = cv_docx_dir / f"CV_{safe}.docx"
            ok = generate_cv_docx(
                result["cv_adapte"], user_name, job_title, docx_path
            )
            if ok:
                result["cv_docx_path"] = str(docx_path)

    return result


# ── Test standalone ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    TEST_LETTRE = """
    Je me permets de vous adresser ma candidature pour le poste de Product Manager
    au sein de votre entreprise. Passionné par l'innovation et doté d'une solide
    expérience dans la gestion de produits digitaux, je suis convaincu que mon profil
    correspond parfaitement à vos attentes.

    Fort de 5 ans d'expérience dans le domaine, je suis rigoureux, dynamique et
    proactif. J'ai développé de solides compétences en leadership et en gestion
    d'équipes pluridisciplinaires. Ma passion pour les nouvelles technologies et
    mon esprit d'analyse me permettent d'apporter une réelle valeur ajoutée.

    Dans l'attente de votre retour, je me tiens à votre disposition pour tout
    entretien. Veuillez agréer, Madame, Monsieur, l'expression de mes salutations
    distinguées.
    """

    TEST_JOB = """
    Product Manager - SaaS B2B
    Nous cherchons un PM expérimenté maîtrisant : Agile/Scrum, Jira, roadmap produit,
    OKR, analytics (Amplitude, Mixpanel), A/B testing, SQL basique, stakeholder management.
    """

    print("=" * 60)
    print("TEST PIPELINE COMPLET")
    print("=" * 60)

    result = process_cover_letter(
        lettre=TEST_LETTRE,
        job_description=TEST_JOB,
    )

    print(f"\n📊 Score IA AVANT : {result['score_avant']['score']}/100 — {result['score_avant']['niveau']}")
    print(f"📊 Score IA APRÈS : {result['score_apres']['score']}/100 — {result['score_apres']['niveau']}")
    print(f"📈 Amélioration   : -{result['gain']} points")
    print(f"🔑 Mots-clés ATS  : {', '.join(result['keywords_ats'][:8])}")
    print(f"\n{'─'*60}\nLETTRE OPTIMISÉE :\n{'─'*60}")
    print(result["text"])
