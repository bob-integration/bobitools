#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# get.sh — installation de Bobi.Tools sur une machine Debian/Ubuntu VIERGE, en une ligne :
#
#     bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh)
#
# ★ UNE RELEASE, PAS UN CLONE GIT (comme le get.sh de Bobi.Studio, depuis 1.2.0).
# Par défaut, la DERNIÈRE VERSION PUBLIÉE : son paquet `bobitools.zip`, vérifié par le
# `SHA256SUMS` joint à la release — le même paquet que tire ensuite la mise à jour
# (Réglages → Système → Déploiement → « Mettre à jour vers … »). `--ref` installe une autre
# étiquette ou une branche, depuis l'archive que sert GitHub (codeload) : curl suffit, git n'est
# plus nécessaire. Avant 1.2.0, ce script clonait le dépôt : ces instances restent suivies par
# git et se mettent à jour par l'onglet GitHub.
# Les OUTILS viennent du Catalogue (comme Studio) : le script propose de les installer tout de
# suite, et l'on en ajoute ensuite depuis Réglages → Outils → Catalogue.
#
# Options :
#   --dir <chemin>   dossier d'installation (défaut : /opt/bobitools)
#   --ref <ref>      étiquette (v1.2.0) ou branche (main) à installer (défaut : dernière release)
#   --docker         installer Docker sans demander     --no-docker   ne pas l'installer
#   --outils <choix> outils à installer : tous | aucun | liste « switch_ports,notes »
#                    (défaut : demander ; « tous » en non interactif)
#   -y               répondre oui à tout (installation non interactive)
#
# Variables : BOBI_DIR, BOBI_REF, BOBI_REPO (défaut bob-integration/bobitools),
#             BOBI_LANG=fr|en, BOBI_GH_TOKEN (jeton GitHub, facultatif : quota du Catalogue
#             porté de 60 à 5 000 requêtes/heure — utile derrière une IP partagée).
set -euo pipefail

INSTALLEUR_VERSION="2026.11.22"   # ⚠ à changer à chaque modification de ce fichier
REPO="${BOBI_REPO:-bob-integration/bobitools}"
REPO="${REPO#https://github.com/}"           # ancienne forme (URL complète) encore acceptée
ORG="${REPO%%/*}"
DIR="${BOBI_DIR:-/opt/bobitools}"
REF="${BOBI_REF:-}"                          # vide = la dernière release publiée
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
    paquets_ok) fr="prérequis présents (curl, python3, venv)"; en="prerequisites present (curl, python3, venv)" ;;
    docker_q)   fr="  ${c_y}?${c_0} Installer Docker (outils en conteneur : switchs, NMOS, caméras…) ? [O/n] "
                en="  ${c_y}?${c_0} Install Docker (containerised tools: switches, NMOS, cameras…)? [Y/n] " ;;
    docker_ok)  fr="Docker présent";                        en="Docker present" ;;
    docker_non) fr="sans Docker : seuls les outils in-process seront disponibles (Docker s'ajoute plus tard)."
                en="without Docker: only in-process tools will be available (Docker can be added later)." ;;
    existe)     fr="%s existe déjà. Pour une instance en place, mettez-la à jour depuis Réglages → Déploiement."
                en="%s already exists. For an existing instance, update it from Settings → Deployment." ;;
    derniere)   fr="dernière version publiée : %s";         en="latest published version: %s" ;;
    sans_rel)   fr="aucune release lisible : installation depuis la branche main"
                en="no readable release: installing from the main branch" ;;
    paquet)     fr="paquet %s (%s), empreinte vérifiée";    en="package %s (%s), checksum verified" ;;
    empreinte)  fr="empreinte du paquet invalide — rien n'a été installé"
                en="package checksum mismatch — nothing was installed" ;;
    archive)    fr="archive de %s (%s)…";                   en="archive of %s (%s)…" ;;
    archive_ko) fr="téléchargement impossible — réseau, référence inexistante, ou dépôt inaccessible ?"
                en="download failed — network, unknown reference, or repository unreachable?" ;;
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
    -h|--help) sed -n '4,29p' "$0"; exit 0 ;;
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
command -v curl >/dev/null || manque+=(curl)
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
_curl=(curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# La dernière release : l'index du catalogue (CDN, hors quota d'API), sinon l'API.
if [ -z "$REF" ]; then
  REF="$("${_curl[@]}" "https://raw.githubusercontent.com/$ORG/bobitools-catalog/HEAD/catalog.json" 2>/dev/null \
         | python3 -c "import json,sys; print((json.load(sys.stdin).get('core') or {}).get('tag') or '')" 2>/dev/null || true)"
  [ -n "$REF" ] || REF="$("${_curl[@]}" "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
         | python3 -c "import json,sys; print(json.load(sys.stdin).get('tag_name') or '')" 2>/dev/null || true)"
  if [ -n "$REF" ]; then ok "$(_t derniere "$REF")"; else warn "$(_t sans_rel)"; REF=main; fi
fi
SOURCE=""
# Une ÉTIQUETTE porteuse du paquet de release : on le prend, et l'empreinte fait foi AVANT tout.
REL="https://github.com/$REPO/releases/download/$REF"
if "${_curl[@]}" -o "$TMP/bobitools.zip" "$REL/bobitools.zip" 2>/dev/null \
   && "${_curl[@]}" -o "$TMP/SHA256SUMS" "$REL/SHA256SUMS" 2>/dev/null; then
  ( cd "$TMP" && grep ' \*\?bobitools.zip$' SHA256SUMS | sha256sum -c --status - ) || die "$(_t empreinte)"
  mkdir -p "$DIR"
  python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$TMP/bobitools.zip" "$DIR"
  SOURCE=paquet
  ok "$(_t paquet "$REF" "$(cut -c1-12 "$TMP/SHA256SUMS")…")"
else
  # Sinon (branche, ou étiquette sans paquet) : l'archive de source servie par GitHub.
  log "$(_t archive "$REPO" "$REF")"
  case "$REF" in v[0-9]*) kind=tags ;; *) kind=heads ;; esac
  "${_curl[@]}" -o "$TMP/src.tgz" "https://codeload.github.com/$REPO/tar.gz/refs/$kind/$REF" 2>/dev/null \
    || "${_curl[@]}" -o "$TMP/src.tgz" "https://codeload.github.com/$REPO/tar.gz/$REF" 2>/dev/null \
    || die "$(_t archive_ko)"
  mkdir -p "$DIR"
  tar -xzf "$TMP/src.tgz" -C "$DIR" --strip-components=1 || die "$(_t archive_ko)"
  SOURCE=archive
fi
[ -f "$DIR/main.py" ] || die "$(_t archive_ko)"
ok "$DIR ($SOURCE)"

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
