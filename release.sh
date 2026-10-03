#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Helper de release d'un plugin OU d'un service Bobi.Tools : archive la version COURANTE
# sous <dir>/versions/<ver>/ puis bumpe le manifeste (version + tag d'image docker pour les
# plugins). Détecte automatiquement plugins/<type>/ ou services/<id>/.
#
# À lancer AU DÉBUT d'un nouveau cycle de version, AVANT d'éditer les fichiers :
#   ./release.sh <type|id> <nouvelle_version>
#   ex. ./release.sh switch_ports 0.46.0      (plugin)
#   ex. ./release.sh emberplus 0.2.0          (service)
#
# Effet :
#   1) snapshot des fichiers à plat actuels (la version déployée) → versions/<courante>/
#   2) manifeste : version -> <nouvelle> (+ docker.image tag -> <nouvelle> si plugin docker)
# Ensuite : éditer le code, ajouter l'entrée changelog dans meta.json, rebuild l'image
# (plugin docker), puis recréer le conteneur / redémarrer l'app (service).
set -euo pipefail

TYPE="${1:-}"; NEW="${2:-}"
if [[ -z "$TYPE" || -z "$NEW" ]]; then
    echo "usage: $0 <type|id> <nouvelle_version>" >&2; exit 2
fi
ROOT="$(cd "$(dirname "$0")" && pwd)"
# Détection plugin vs service.
if [[ -f "$ROOT/plugins/$TYPE/plugin.json" ]]; then
    DIR="$ROOT/plugins/$TYPE"; MAN="$DIR/plugin.json"
elif [[ -f "$ROOT/services/$TYPE/manifest.json" ]]; then
    DIR="$ROOT/services/$TYPE"; MAN="$DIR/manifest.json"
else
    echo "plugin/service introuvable : plugins/$TYPE ou services/$TYPE" >&2; exit 1
fi

CUR="$(python3 -c "import json;print(json.load(open('$MAN'))['version'])")"
if [[ "$CUR" == "$NEW" ]]; then
    echo "plugin.json est déjà en $NEW — rien à faire." >&2; exit 1
fi

# 1) Archive la version courante (fichiers à plat, hors versions/ et __pycache__).
ARCH="$DIR/versions/$CUR"
if [[ -d "$ARCH" ]]; then
    echo "versions/$CUR existe déjà — archive conservée."
else
    mkdir -p "$ARCH"
    find "$DIR" -maxdepth 1 -type f -exec cp -p {} "$ARCH/" \;
    echo "archivé : $TYPE $CUR -> versions/$CUR/"
fi

# 2) Bump plugin.json (version + tag d'image docker s'il y en a un).
python3 - "$MAN" "$NEW" <<'PY'
import json, re, sys
path, new = sys.argv[1], sys.argv[2]
m = json.load(open(path, encoding="utf-8"))
m["version"] = new
dk = m.get("docker")
if isinstance(dk, dict) and dk.get("image"):
    dk["image"] = re.sub(r":[^:]+$", ":" + new, dk["image"])
with open(path, "w", encoding="utf-8") as f:
    json.dump(m, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
echo "bumpé : $TYPE $CUR -> $NEW"
echo
echo "À FAIRE ENSUITE :"
echo "  1) éditer le code de la nouvelle version"
echo "  2) ajouter l'entrée changelog en tête de $DIR/meta.json"
echo "  3) si runtime=docker : docker build -t bobitool-$TYPE:$NEW -f $DIR/Dockerfile $DIR"
echo "  4) recréer le conteneur (UI : barre Docker → Stop puis Start)"
