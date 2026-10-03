#!/usr/bin/env bash
# Installation de Bobi.Tools sur un nouveau serveur.
# À lancer depuis /opt/bobitools après avoir copié les fichiers.

set -e
cd "$(dirname "$0")"

echo ""
echo "=== Installation Bobi.Tools ==="
echo ""

# ── 0. Submodules (plugins/* et services/* = dépôts séparés) ──────────────────
# Chaque plugin/service est un dépôt à part agrégé en submodule (cf. .gitmodules).
# Sans cette étape, plugins/ et services/ seraient vides après un clone simple.
if [ -f .gitmodules ] && command -v git &>/dev/null && [ -d .git ]; then
    echo "→ Initialisation des submodules (plugins/services)..."
    git submodule update --init --recursive
    echo "  Submodules à jour."
fi

# ── 1. Python ─────────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "❌  python3 introuvable. Installer avec : apt install python3 python3-venv"
    exit 1
fi

echo "→ Création du venv..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install -r requirements.txt --quiet
echo "  Dépendances installées."

# ── 2. Docker (optionnel mais recommandé : outils runtime=docker) ─────────────
if command -v docker &>/dev/null; then
    echo "  Docker détecté : les outils runtime=docker seront disponibles."
else
    echo "⚠  Docker introuvable. Les outils 'inprocess' fonctionnent ; les outils"
    echo "   'docker' seront indisponibles tant que Docker n'est pas installé."
fi

# ── 3. Configuration locale (optionnelle) ─────────────────────────────────────
if [ ! -f config_local.py ]; then
    cp config_local.example.py config_local.py
    echo "  config_local.py créé depuis le modèle (les défauts conviennent en standard)."
fi

# ── 4. Service systemd ────────────────────────────────────────────────────────
SERVICE_SRC="bobitools.service"
SERVICE_DST="/etc/systemd/system/bobitools.service"
if [ -w /etc/systemd/system ] || [ "$(id -u)" = "0" ]; then
    cp "$SERVICE_SRC" "$SERVICE_DST"
    systemctl daemon-reload
    systemctl enable bobitools
    echo "  Service systemd installé et activé."
else
    echo "⚠  Pas les droits pour installer le service systemd (relancer en root si voulu)."
fi

# ── 5. Base de données + compte admin ─────────────────────────────────────────
if [ ! -f db_bobitools.db ]; then
    echo "  Initialisation de la base de données..."
    ./venv/bin/python -c "from app.database import init_db; init_db()"
    echo "  Base créée."
fi

echo ""
echo "=== Installation terminée ==="
echo ""
echo "  Créer un administrateur :  ./venv/bin/python tools/create_admin.py"
echo "  (ou laisser l'assistant /setup le faire au premier accès web)"
echo ""
echo "  Démarrer :  systemctl start bobitools   (ou ./venv/bin/python main.py)"
echo "  Accès    :  http://<hôte>:5000"
echo ""
