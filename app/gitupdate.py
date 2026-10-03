# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mise à jour de CETTE instance depuis GitHub (source unique).

Le déploiement est un checkout git avec submodules (URLs HTTPS). On tire
`origin/<branche>` en FAST-FORWARD, on resynchronise les submodules, puis on relance
le service. L'état local (`config_local.py`, `*.db`, `static/uploads/`) est gitignoré
→ jamais touché par un pull, donc préservé gratuitement.

Auth : on réutilise les credentials git déjà configurés sur la machine (aucun secret
stocké par l'app). `GIT_TERMINAL_PROMPT=0` garantit qu'un accès non autorisé échoue
immédiatement au lieu de figer l'appel sur une invite de mot de passe.

Rollback : le commit courant est mémorisé AVANT le pull (`git_rollback.json`) ; le
rollback fait un `git reset --hard` dessus + resync submodules + redémarrage.

Réutilise `updater` pour le redémarrage (systemd), l'horodatage de déploiement et le
marqueur PENDING confirmé au boot (`updater.confirm_boot_ok`).
"""
import json
import logging
import os
import subprocess
from datetime import datetime

from . import updater

log = logging.getLogger(__name__)

ROOT = updater.ROOT
ROLLBACK_PATH = os.path.join(ROOT, "git_rollback.json")

# git NON interactif : jamais d'invite de credentials qui figerait la requête HTTP.
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}


def _git(*args, timeout=120):
    """Lance git dans ROOT. Renvoie (rc, stdout, stderr) élagués."""
    try:
        p = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True,
                           env=_ENV, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "délai dépassé"
    except Exception as e:                                  # git absent, etc.
        return 1, "", str(e)


def is_git():
    rc, out, _ = _git("rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def _branch():
    rc, out, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 else ""


def _rev(ref):
    rc, out, _ = _git("rev-parse", ref)
    return out if rc == 0 else ""


def _desc(ref):
    """« <hash court> <sujet> » d'un commit, ou '' si inconnu."""
    rc, out, _ = _git("show", "-s", "--format=%h %s", ref)
    return out if rc == 0 else ""


def _dirty():
    """Modifications locales non commitées du dépôt PARENT (on ignore la dérive de
    pointeur de submodule, résorbée par `submodule update`)."""
    rc, out, _ = _git("status", "--porcelain", "--untracked-files=no",
                      "--ignore-submodules=all")
    return bool(out.strip())


def _upstream(br):
    return f"origin/{br}" if br and br != "HEAD" else "origin/HEAD"


def status(fetch=False):
    """État git pour l'UI : branche, HEAD local/distant, avance/retard, propreté,
    submodules désynchronisés. `fetch=True` interroge d'abord origin (réseau)."""
    if not is_git():
        return {"is_git": False}
    fetch_error = None
    if fetch:
        rc, _, err = _git("fetch", "origin", timeout=90)
        if rc != 0:
            fetch_error = err or "git fetch a échoué"
    br = _branch()
    up = _upstream(br)
    behind = ahead = 0
    rc, out, _ = _git("rev-list", "--left-right", "--count", f"{up}...HEAD")
    if rc == 0 and out:
        try:
            b, a = out.split()
            behind, ahead = int(b), int(a)     # gauche=upstream (retard), droite=HEAD (avance)
        except ValueError:
            pass
    rc, out, _ = _git("submodule", "status", "--recursive")
    subs = len([l for l in out.splitlines() if l[:1] in "+-"]) if rc == 0 else 0
    return {
        "is_git": True,
        "branch": br,
        "head": _desc("HEAD"),
        "remote": _desc(up),
        "behind": behind,
        "ahead": ahead,
        "dirty": _dirty(),
        "submodules_out_of_sync": subs,
        "fetch_error": fetch_error,
    }


def _save_rollback(sha):
    try:
        with open(ROLLBACK_PATH, "w") as f:
            json.dump({"prev": sha, "at": datetime.now().isoformat(timespec="seconds")}, f)
    except Exception as e:
        log.debug("save_rollback: %s", e)


def _load_rollback():
    try:
        with open(ROLLBACK_PATH) as f:
            return (json.load(f) or {}).get("prev")
    except Exception:
        return None


def _prepare_new_submodules():
    """Un dossier de plugin qui DEVIENT submodule (ex. coffre, 2026-10) garde après le
    fast-forward son `__pycache__` — ignoré par git, donc jamais supprimé. Le dossier n'est
    alors pas vide, et `submodule update --init` refuse d'y cloner : la mise à jour échoue,
    et le plugin disparaît au redémarrage suivant. On retire ce cache — et SEULEMENT lui :
    s'il reste autre chose dans le dossier, on n'y touche pas (ce serait du travail local)."""
    import shutil
    rc, out, _ = _git("config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.path")
    if rc != 0:
        return
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        path = os.path.join(ROOT, parts[1].strip())
        if not os.path.isdir(path) or os.path.exists(os.path.join(path, ".git")):
            continue
        contenu = [x for x in os.listdir(path)]
        if contenu and all(x == "__pycache__" for x in contenu):
            shutil.rmtree(os.path.join(path, "__pycache__"), ignore_errors=True)
            log.info("gitupdate: cache Python retiré de %s (devient submodule)", parts[1])


def update():
    """Fast-forward depuis origin/<branche> + submodules, puis redémarrage.
    Renvoie (ok, msg). Refuse si dépôt sale, HEAD détachée ou branche divergente."""
    if not is_git():
        return False, "cette instance n'est pas un checkout git"
    br = _branch()
    if not br or br == "HEAD":
        return False, "HEAD détachée : impossible de déterminer la branche à suivre"
    if _dirty():
        return False, "modifications locales non commitées : commit/stash avant de mettre à jour"

    rc, _, err = _git("fetch", "origin", timeout=120)
    if rc != 0:
        return False, f"git fetch a échoué : {err or 'erreur (accès GitHub ?)'}"

    prev = _rev("HEAD")
    rc, out, err = _git("merge", "--ff-only", _upstream(br), timeout=60)
    if rc != 0:
        return False, f"fast-forward impossible (branche divergente / commits locaux ?) : {err or out}"
    _prepare_new_submodules()
    rc, out, err = _git("submodule", "update", "--init", "--recursive", timeout=300)
    if rc != 0:
        return False, f"submodule update a échoué : {err or out}"

    _save_rollback(prev)
    now = _rev("HEAD")
    updater.record_deploy(now[:12], _desc("HEAD"))
    try:                                       # marqueur confirmé au boot (updater.confirm_boot_ok)
        with open(updater.PENDING_PATH, "w") as f:
            f.write(now[:12])
    except Exception as e:
        log.debug("pending: %s", e)
    updater.restart_service()
    return True, f"mise à jour appliquée : {_desc('HEAD')} — redémarrage en cours"


def rollback():
    """Restaure le commit d'avant la dernière mise à jour git + submodules, puis relance."""
    prev = _load_rollback()
    if not prev:
        return False, "aucun point de rollback enregistré"
    if not is_git():
        return False, "cette instance n'est pas un checkout git"
    rc, out, err = _git("reset", "--hard", prev, timeout=60)
    if rc != 0:
        return False, f"git reset a échoué : {err or out}"
    _prepare_new_submodules()
    _git("submodule", "update", "--init", "--recursive", timeout=300)
    updater.clear_pending()
    updater.record_deploy(prev[:12], _desc("HEAD"))
    updater.restart_service()
    return True, f"rollback vers {prev[:12]} — redémarrage en cours"
