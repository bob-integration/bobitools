#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# get.sh — installation de Bobi.Tools sur une machine Debian/Ubuntu VIERGE, en une ligne :
#
#     bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh)
#
# ★ UN VRAI CLONE GIT, PAS UNE ARCHIVE (différence assumée avec le get.sh de Bobi.Studio).
# Bobi.Tools se met à jour depuis GitHub par un fast-forward (Réglages → Déploiement → GitHub) :
# une instance installée depuis une archive n'aurait pas de dépôt git et ne pourrait jamais se
# mettre à jour par ce chemin. On installe donc git s'il manque, et l'on clone AVEC les submodules
# (chaque plugin et service est un dépôt à part).
#
# Options :
#   --dir <chemin>   dossier d'installation (défaut : /opt/bobitools)
#   --ref <branche>  branche ou tag à installer (défaut : main)
#   --docker         installer Docker sans demander     --no-docker   ne pas l'installer
#   -y               répondre oui à tout (installation non interactive)
#
# Variables : BOBI_DIR, BOBI_REF, BOBI_REPO (défaut https://github.com/bob-integration/bobitools),
#             BOBI_LANG=fr|en.
set -euo pipefail

INSTALLEUR_VERSION="2026.10.06"   # ⚠ à changer à chaque modification de ce fichier
REPO="${BOBI_REPO:-https://github.com/bob-integration/bobitools}"
DIR="${BOBI_DIR:-/opt/bobitools}"
REF="${BOBI_REF:-main}"
DOCKER=""          # vide = demander ; oui | non
OUI=0

c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_b=$'\033[34m'; c_0=$'\033[0m'
log(){ echo "  ${c_b}·${c_0} $*"; }; ok(){ echo "  ${c_g}✓${c_0} $*"; }
warn(){ echo "  ${c_y}!${c_0} $*"; }; die(){ echo "  ${c_r}✗${c_0} $*" >&2; exit 1; }

UI="${BOBI_LANG:-}"
if [ -z "$UI" ]; then
  case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in fr*|FR*|"") UI=fr ;; *) UI=en ;; esac
fi
# Une clé, deux langues ; printf pour les valeurs (l'ordre des mots change d'une langue à l'autre).
_t() {
  local k="$1"; shift; local fr="" en=""
  case "$k" in
    titre)      fr="Installation de Bobi.Tools";            en="Installing Bobi.Tools" ;;
    root)       fr="à lancer en root (l'installation pose un service systemd)."
                en="must be run as root (the installation sets up a systemd service)." ;;
    os)         fr="apt-get introuvable : ce script vise Debian / Ubuntu."
                en="apt-get not found: this script targets Debian / Ubuntu." ;;
    paquets)    fr="paquets à installer : %s";              en="packages to install: %s" ;;
    paquets_ok) fr="prérequis présents (git, python3, venv)"; en="prerequisites present (git, python3, venv)" ;;
    docker_q)   fr="  ${c_y}?${c_0} Installer Docker (outils en conteneur : switchs, NMOS, caméras…) ? [O/n] "
                en="  ${c_y}?${c_0} Install Docker (containerised tools: switches, NMOS, cameras…)? [Y/n] " ;;
    docker_ok)  fr="Docker présent";                        en="Docker present" ;;
    docker_non) fr="sans Docker : seuls les outils in-process seront disponibles (Docker s'ajoute plus tard)."
                en="without Docker: only in-process tools will be available (Docker can be added later)." ;;
    existe)     fr="%s existe déjà. Pour une instance en place, mettez-la à jour depuis Réglages → Déploiement."
                en="%s already exists. For an existing instance, update it from Settings → Deployment." ;;
    clone)      fr="clonage de %s (%s), avec les plugins…"; en="cloning %s (%s), with plugins…" ;;
    clone_ko)   fr="clonage impossible — réseau, ou dépôt inaccessible ?"
                en="clone failed — network, or repository unreachable?" ;;
    install)    fr="installation (venv, dépendances, service)…"; en="installing (venv, dependencies, service)…" ;;
    demarre)    fr="service démarré";                        en="service started" ;;
    fini)       fr="Bobi.Tools est installé.";               en="Bobi.Tools is installed." ;;
    ouvrir)     fr="Ouvrir %s pour créer le premier administrateur."
                en="Open %s to create the first administrator." ;;
    sans_sysd)  fr="pas de systemd ici : démarrer à la main avec  cd %s && ./venv/bin/python main.py"
                en="no systemd here: start manually with  cd %s && ./venv/bin/python main.py" ;;
  esac
  local s="$fr"; [ "$UI" = en ] && [ -n "$en" ] && s="$en"
  # shellcheck disable=SC2059
  printf "$s" "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --docker) DOCKER=oui; shift ;;
    --no-docker) DOCKER=non; shift ;;
    -y) OUI=1; shift ;;
    -h|--help) sed -n '4,22p' "$0"; exit 0 ;;
    *) die "option inconnue / unknown option: $1" ;;
  esac
done

echo ""
echo "  ${c_b}$(_t titre)${c_0}  (get.sh $INSTALLEUR_VERSION)"
echo ""

[ "$(id -u)" = 0 ] || die "$(_t root)"
command -v apt-get >/dev/null || die "$(_t os)"
# Hors terminal (curl | bash sans tty, provisionnement), on ne peut rien demander : oui par défaut.
[ -t 0 ] || OUI=1
export DEBIAN_FRONTEND=noninteractive

# ── Prérequis ────────────────────────────────────────────────
manque=()
command -v git >/dev/null || manque+=(git)
command -v python3 >/dev/null || manque+=(python3)
python3 -c "import ensurepip, venv" 2>/dev/null || manque+=(python3-venv)
[ -e /etc/ssl/certs/ca-certificates.crt ] || manque+=(ca-certificates)
if [ ${#manque[@]} -gt 0 ]; then
  log "$(_t paquets "${manque[*]}")"
  apt-get update -qq && apt-get install -y -qq "${manque[@]}" >/dev/null
fi
ok "$(_t paquets_ok)"

# ── Docker (facultatif) ──────────────────────────────────────
if command -v docker >/dev/null; then
  ok "$(_t docker_ok)"
else
  if [ -z "$DOCKER" ]; then
    if [ "$OUI" = 1 ]; then DOCKER=oui
    else read -r -p "$(_t docker_q)" r; case "${r:-o}" in [nN]*) DOCKER=non ;; *) DOCKER=oui ;; esac
    fi
  fi
  if [ "$DOCKER" = oui ]; then
    apt-get install -y -qq docker.io >/dev/null
    command -v systemctl >/dev/null && [ -d /run/systemd/system ] && systemctl enable --now docker >/dev/null 2>&1 || true
    ok "$(_t docker_ok)"
  else
    warn "$(_t docker_non)"
  fi
fi

# ── Source ───────────────────────────────────────────────────
[ -e "$DIR" ] && die "$(_t existe "$DIR")"
log "$(_t clone "$REPO" "$REF")"
git clone -q --branch "$REF" --recurse-submodules "$REPO" "$DIR" || die "$(_t clone_ko)"
ok "$DIR"

# ── Installation ─────────────────────────────────────────────
log "$(_t install)"
bash "$DIR/install.sh"

if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  systemctl start bobitools && ok "$(_t demarre)"
else
  warn "$(_t sans_sysd "$DIR")"
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
ok "$(_t fini)"
echo "    $(_t ouvrir "http://${ip:-<hôte>}:5000/setup")"
echo ""
