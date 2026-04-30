#!/bin/bash
# Double-clique sur ce fichier pour lancer JobBot

cd "$(dirname "$0")"

echo ""
echo "================================================"
echo "  🤖  JobBot — Démarrage..."
echo "================================================"
echo ""

# Vérifie Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python3 introuvable. Installe Python sur python.org"
    read -p "Appuie sur Entrée pour fermer..."
    exit 1
fi

# Installe les dépendances si besoin
echo "📦 Vérification des dépendances..."
pip3 install -q -r requirements.txt

# Lance le serveur
echo ""
echo "✅ Démarrage du serveur..."
echo "📌 Ouvre ton navigateur sur : http://localhost:5000"
echo ""
echo "   (Appuie sur Ctrl+C pour arrêter le bot)"
echo ""

# Ouvre le navigateur automatiquement
sleep 2 && open "http://localhost:5000" &

python3 app.py
