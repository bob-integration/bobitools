#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# get.sh — installation de Bobi.Tools sur une machine Debian/Ubuntu VIERGE, en une ligne :
#
#     bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh)
#
# ★ UN VRAI CLONE GIT DU CŒUR, PAS UNE ARCHIVE (différence assumée avec le get.sh de Studio).
# Bobi.Tools se met à jour depuis GitHub par un fast-forward (Réglages → Déploiement → GitHub) :
# sans dépôt git, ce chemin serait fermé. Les OUTILS, eux, viennent du Catalogue (comme Studio) :
# le script propose de les installer tout de suite, et l'on en ajoute ensuite depuis
# Réglages → Outils → Catalogue.
#
# Options :
#   --dir <chemin>   dossier d'installation (défaut : /opt/bobitools)
#   --ref <branche>  branche ou tag à installer (défaut : main)
#   --docker         installer Docker sans demander     --no-docker   ne pas l'installer
#   --outils <choix> outils à installer : tous | aucun | liste « switch_ports,notes »
#                    (défaut : demander ; « tous » en non interactif)
#   -y               répondre oui à tout (installation non interactive)
#
# Variables : BOBI_DIR, BOBI_REF, BOBI_REPO (défaut https://github.com/bob-integration/bobitools),
#             BOBI_LANG=fr|en, BOBI_GH_TOKEN (jeton GitHub, facultatif : quota du Catalogue
#             porté de 60 à 5 000 requêtes/heure — utile derrière une IP partagée).
set -euo pipefail

INSTALLEUR_VERSION="2026.10.08"   # ⚠ à changer à chaque modification de ce fichier
REPO="${BOBI_REPO:-https://github.com/bob-integration/bobitools}"
DIR="${BOBI_DIR:-/opt/bobitools}"
REF="${BOBI_REF:-main}"
DOCKER=""          # vide = demander ; oui | non
OUTILS="${BOBI_OUTILS:-}"   # vide = demander ; tous | aucun | liste
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
    clone)      fr="clonage de %s (%s)…";                   en="cloning %s (%s)…" ;;
    clone_ko)   fr="clonage impossible — réseau, ou dépôt inaccessible ?"
                en="clone failed — network, or repository unreachable?" ;;
    install)    fr="installation (venv, dépendances, service)…"; en="installing (venv, dependencies, service)…" ;;
    demarre)    fr="service démarré";                        en="service started" ;;
    fini)       fr="Bobi.Tools est installé.";               en="Bobi.Tools is installed." ;;
    ouvrir)     fr="Ouvrir %s pour créer le premier administrateur."
                en="Open %s to create the first administrator." ;;
    outils_q)   fr="  ${c_y}?${c_0} Outils à installer depuis le Catalogue — [T]ous, [A]ucun, ou une liste (ex. switch_ports,notes) [T] : "
                en="  ${c_y}?${c_0} Tools to install from the Catalogue — [A]ll, [N]one, or a list (e.g. switch_ports,notes) [A]: " ;;
    outils)     fr="installation des outils (%s)…";          en="installing tools (%s)…" ;;
    outils_non) fr="aucun outil installé : Réglages → Outils → Catalogue pour en ajouter."
                en="no tool installed: Settings → Tools → Catalogue to add some." ;;
    outils_ko)  fr="certains outils n'ont pas pu être installés : réessayer depuis Réglages → Outils → Catalogue."
                en="some tools could not be installed: retry from Settings → Tools → Catalogue." ;;
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
    --outils) OUTILS="$2"; shift 2 ;;
    --no-docker) DOCKER=non; shift ;;
    -y) OUI=1; shift ;;
    -h|--help) sed -n '4,24p' "$0"; exit 0 ;;
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

# ── Outils (Catalogue) ───────────────────────────────────────
if [ -z "$OUTILS" ]; then
  if [ "$OUI" = 1 ]; then OUTILS=tous
  else read -r -p "$(_t outils_q)" r
    # « A » veut dire Aucun en français mais All en anglais : la réponse se lit dans SA langue.
    r="${r:-défaut}"
    if [ "$UI" = en ]; then
      case "$r" in défaut|[aA]|[aA]ll) OUTILS=tous ;; [nN]|[nN]one) OUTILS=aucun ;; *) OUTILS="$r" ;; esac
    else
      case "$r" in défaut|[tT]|[tT]ous) OUTILS=tous ;; [aA]|[aA]ucun) OUTILS=aucun ;; *) OUTILS="$r" ;; esac
    fi
  fi
fi
if [ "$OUTILS" = aucun ]; then
  warn "$(_t outils_non)"
else
  log "$(_t outils "$OUTILS")"
  liste=$( [ "$OUTILS" = tous ] && echo tous || echo "$OUTILS" | tr ',' ' ' )
  # shellcheck disable=SC2086
  ( cd "$DIR" && ./venv/bin/python tools/catalogue.py installe $liste ) || warn "$(_t outils_ko)"
fi

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
