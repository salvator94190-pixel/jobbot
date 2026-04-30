# 🚀 Guide de déploiement — JobBot en ligne

## Option A — Tester en local (maintenant, gratuit)

1. Double-clique sur **`LANCER.command`**
2. Le navigateur s'ouvre sur `http://localhost:5000`
3. Crée ton compte, upload ton CV, configure tes critères
4. Lance une recherche !

> Tes amis ne peuvent pas encore y accéder en dehors de ton réseau.

---

## Option B — Mettre en ligne sur Railway (lien public, gratuit)

Railway est une plateforme qui héberge ton app et te donne une vraie URL publique.

### Étapes

1. **Crée un compte** sur [railway.app](https://railway.app) (gratuit)

2. **Installe Railway CLI** :
   ```
   npm install -g @railway/cli
   ```

3. **Dans le terminal, va dans ce dossier** :
   ```
   cd "/Users/salvy/Desktop/bot/bot candidature"
   ```

4. **Connecte-toi et déploie** :
   ```
   railway login
   railway init
   railway up
   ```

5. **Configure les variables d'environnement** dans Railway :
   - `ANTHROPIC_API_KEY` → ta clé API Anthropic (pour la génération de lettres)
   - `SECRET_KEY` → n'importe quelle chaîne aléatoire longue

6. **Obtiens ton URL** depuis le dashboard Railway → tu peux la partager !

---

## Option C — Render.com (alternative gratuite)

1. Crée un compte sur [render.com](https://render.com)
2. New → Web Service → "Deploy from existing code"
3. Upload le dossier ou connecte un repo GitHub
4. Build command : `pip install -r requirements.txt`
5. Start command : `python app.py`
6. Ajoute les variables d'environnement (ANTHROPIC_API_KEY, SECRET_KEY)

---

## Clé Anthropic (pour les lettres de motivation IA)

1. Va sur [console.anthropic.com](https://console.anthropic.com)
2. API Keys → Create Key
3. Copie la clé et ajoute-la dans Railway/Render comme `ANTHROPIC_API_KEY`

Sans cette clé, le bot fonctionne quand même (recherche + filtrage) mais ne génère pas de lettres.

---

## Architecture du projet

```
bot candidature/
├── app.py              ← Serveur principal (Flask)
├── requirements.txt    ← Dépendances Python
├── LANCER.command      ← Démarrage local (double-clic)
├── database.db         ← Base de données (créée automatiquement)
└── uploads/            ← CVs des utilisateurs (créé automatiquement)
```

## Fonctionnalités incluses

- ✅ Comptes utilisateurs (inscription / connexion)
- ✅ Upload et analyse du CV
- ✅ Critères personnalisés par utilisateur (poste, salaire, localisation, contrat)
- ✅ Scoring automatique CV ↔ offre
- ✅ Génération de lettre de motivation IA (Anthropic)
- ✅ Tableau de bord de suivi des candidatures
- ✅ Changement de statut (À postuler / Envoyé / En attente / Refus)
- ✅ Visualisation des lettres générées
