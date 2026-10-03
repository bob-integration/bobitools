# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mise à jour entre instances Bobi.Tools (pull / push) sur le réseau local.

Modèle : une instance « serveur » expose son code (zip builder, déjà sans secret) + un
manifeste (versions + sha256). Une autre instance tire ce zip, vérifie le checksum,
sauvegarde son arbre code, applique par-dessus (sans toucher l'état local), puis relance
son service. Le « push » = appeler `apply_update` à distance sur le pair (auth par SON token).

Sécurité : token partagé obligatoire (vérifié côté routes) + sha256 du zip vérifié avant
extraction. L'extraction PRÉSERVE config_local.py, les bases *.db (et annexes) et
static/uploads/. Dérivé de l'updater de Bobi.Studio.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime

from . import builder

log = logging.getLogger(__name__)

ROOT = builder.ROOT
DIST_DIR = builder.DIST_DIR
# Zip dédié à la mise à jour inter-instances : copie FIDÈLE (tous plugins/services),
# distinct du zip de distribution sélectif servi par /install.
ZIP_PATH = os.path.join(DIST_DIR, "bobitools-update.zip")
SERVICE = "bobitools"
PENDING_PATH = os.path.join(ROOT, "UPDATE_PENDING")
# Horodatage du DERNIER déploiement appliqué SUR cette instance (≠ built_at, qui date la
# construction du zip). Écrit par apply_update, lu par ping/UI.
DEPLOY_INFO_PATH = os.path.join(ROOT, "deploy_info.json")

UPDATE_TOKEN_HEADER = "X-BT-Update-Token"


def _protected(arc):
    """Chemins jamais écrasés lors de l'extraction (état local de l'instance cible)."""
    a = arc.replace("\\", "/")
    # coffre.key : clé maître du coffre à identifiants. L'écraser rendrait TOUS les mots
    # de passe de l'instance illisibles — elle est locale, jamais distribuée.
    return (a == "config_local.py" or a == "coffre.key"
            or a.endswith((".db", ".db-wal", ".db-shm", ".db-journal"))
            or a.startswith("static/uploads/") or a.startswith("dist/")
            or a == "build_manifest.json" or a == "deploy_info.json")


# ─── Côté serveur : manifeste + zip à jour ───────────────────────────────────

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_mtime():
    """mtime le plus récent de l'arbre code (hors dist/venv/caches) → détecte un changement."""
    newest = 0.0
    for d in builder.CORE_DIRS + ["plugins", "services"]:
        ap = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(ap):
            dirnames[:] = [x for x in dirnames if x not in builder.EXCLUDE_DIRS]
            for fn in filenames:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
                except OSError:
                    pass
    return newest


def ensure_build():
    """Garantit un zip de mise à jour à jour : reconstruit (TOUS plugins/services,
    `stamp=False` → identité stable) seulement si le zip manque ou si du code a changé
    depuis sa dernière génération. L'identité (build_info.json) ne bouge qu'à un build
    « release » explicite (stamp=True) ou après un pull."""
    builder.identity()   # re-tamponne build_info.json si HEAD a bougé (dépôt git)
    if os.path.exists(ZIP_PATH):
        zt = os.path.getmtime(ZIP_PATH)
        try:
            bt = os.path.getmtime(builder.BUILD_INFO_PATH)
        except OSError:
            bt = 0.0
        # build_info.json compte aussi : le zip doit embarquer l'identité qu'on annonce.
        if zt >= _code_mtime() and zt >= bt:
            return None
    return builder.build(plugins=None, services=None, dest=ZIP_PATH, stamp=False)


def diff_manifests(old, new):
    """Compare deux manifestes (cible `old` → source `new`) et renvoie le détail des
    plugins/services qui changent. Fonction PURE (pas d'I/O) → testable.

    Renvoie {"components": [{kind, id, label, from, to, status}], "counts": {...}}
    avec status ∈ added | updated | removed | unchanged. Tolère des manifestes partiels."""
    old = old or {}
    new = new or {}
    components = []
    counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}

    def _index(manifest, list_key, id_key):
        out = {}
        for it in (manifest.get(list_key) or []):
            key = it.get(id_key)
            if key:
                out[key] = it
        return out

    for kind, list_key, id_key in (("plugin", "plugins", "type"),
                                   ("service", "services", "id")):
        o = _index(old, list_key, id_key)
        n = _index(new, list_key, id_key)
        for key in sorted(set(o) | set(n)):
            ov = o.get(key, {}).get("version") or ""
            nv = n.get(key, {}).get("version") or ""
            label = (n.get(key) or o.get(key) or {}).get("label") or key
            if key not in o:
                status = "added"
            elif key not in n:
                status = "removed"
            elif ov != nv:
                status = "updated"
            else:
                status = "unchanged"
            counts[status] += 1
            components.append({"kind": kind, "id": key, "label": label,
                               "from": ov, "to": nv, "status": status})
    return {"components": components, "counts": counts}


def current_manifest():
    """Manifeste servi à un pair : identité de build + sha256 du zip + versions.

    Recharge d'abord les registres plugins/services depuis le disque : sinon les numéros
    de version rapportés viendraient du cache mémoire figé au démarrage."""
    from . import plugins, core_plugins
    plugins.reload()
    # `restart=False` : on ne veut ici que des numéros de version à jour. Redémarrer les
    # services parce qu'un pair demande un manifeste couperait des connexions de contrôleur
    # pour une simple lecture.
    core_plugins.reload(restart=False)
    ensure_build()
    info = builder.identity()
    av = builder.available()
    return {
        "build_id": info.get("build_id"),
        "label":    info.get("label"),
        "built_at": info.get("built_at"),
        "git_hash": info.get("git_hash"),
        "sha256":   sha256_file(ZIP_PATH),
        "size":     os.path.getsize(ZIP_PATH),
        "plugins":  av.get("plugins", []),
        "services": av.get("services", []),
    }


# ─── Côté client : récupération + application ────────────────────────────────

def _http_json(url, token, timeout=15):
    req = urllib.request.Request(url, headers={UPDATE_TOKEN_HEADER: token or ""})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (réseau interne)
        return json.loads(r.read().decode())


def fetch_manifest(base_url, token):
    return _http_json(base_url.rstrip("/") + "/api/update/manifest", token)


def ping(base_url, token=None, timeout=4):
    return _http_json(base_url.rstrip("/") + "/api/update/ping", token, timeout=timeout)


def _download(url, token, dest, timeout=120):
    req = urllib.request.Request(url, headers={UPDATE_TOKEN_HEADER: token or ""})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f)


def backup_code():
    """Archive l'arbre code courant dans dist/backup-<ts>.tgz (pour rollback)."""
    os.makedirs(DIST_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(DIST_DIR, f"backup-{ts}.tgz")
    members = builder.CORE_DIRS + ["plugins", "services"] + [
        f for f in ("main.py", "requirements.txt", "build_info.json")
        if os.path.exists(os.path.join(ROOT, f))
    ]
    with tarfile.open(dest, "w:gz") as tar:
        for m in members:
            ap = os.path.join(ROOT, m)
            if os.path.exists(ap):
                tar.add(ap, arcname=m, filter=_tar_filter)
    return dest


def _tar_filter(ti):
    # Ne pas embarquer venv/caches/uploads/db dans le backup non plus.
    parts = ti.name.split("/")
    if any(p in builder.EXCLUDE_DIRS for p in parts) or "uploads" in parts:
        return None
    if ti.name.endswith((".db", ".db-wal", ".db-shm", ".db-journal", ".pyc", ".log")):
        return None
    return ti


def latest_backup():
    try:
        bks = sorted(b for b in os.listdir(DIST_DIR)
                     if b.startswith("backup-") and b.endswith(".tgz"))
        return os.path.join(DIST_DIR, bks[-1]) if bks else None
    except FileNotFoundError:
        return None


def _safe_target(name):
    """Chemin absolu de destination pour un membre d'archive, ou None si le membre
    tente de sortir de ROOT (zip-slip : nom absolu ou « .. »)."""
    root = os.path.realpath(ROOT)
    target = os.path.realpath(os.path.join(root, name))
    if target == root or target.startswith(root + os.sep):
        return target
    return None


def local_component_ids():
    """Composants INSTALLÉS ici, lus sur le DISQUE (pas le registre en mémoire : un plugin
    cassé — manifeste invalide… — est absent du registre alors qu'il est bien installé, et
    c'est souvent la mise à jour qui le répare). Un plugin = plugins/<type>/plugin.json ; un
    service = services/<id>/. Les dossiers `_…`/`.…` ne sont pas des composants."""
    plugs, servs = set(), set()
    for sub, out, marker in (("plugins", plugs, "plugin.json"), ("services", servs, None)):
        d = os.path.join(ROOT, sub)
        try:
            for name in os.listdir(d):
                if name.startswith(("_", ".")):
                    continue
                full = os.path.join(d, name)
                if os.path.isdir(full) and (marker is None
                                            or os.path.exists(os.path.join(full, marker))):
                    out.add(name)
        except OSError:
            pass
    return plugs, servs


def _extract_over(zip_path, skip_plugins=None, skip_services=None):
    """Extrait le zip par-dessus ROOT en sautant les chemins protégés (état local) et les
    composants exclus (composition par instance : plugins/services non installés ici).
    Refuse tout membre qui tenterait d'écrire hors de ROOT (zip-slip)."""
    skip_plugins = skip_plugins or set()
    skip_services = skip_services or set()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if _protected(name):
                continue
            parts = name.split("/", 2)
            if len(parts) >= 2 and ((parts[0] == "plugins" and parts[1] in skip_plugins)
                                    or (parts[0] == "services" and parts[1] in skip_services)):
                continue
            target = _safe_target(name)
            if target is None:
                raise ValueError(f"membre d'archive hors périmètre (zip-slip) : {name!r}")
            os.makedirs(os.path.dirname(target) or ROOT, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _missing_requirements(req_text):
    """Lignes de requirements.txt dont le paquet n'est PAS installé dans ce venv. Présence
    seulement, pas la version exacte : une autre version déjà installée fait l'affaire, on
    ne remue pas les paquets d'un site hors ligne."""
    import importlib.metadata as md
    import re
    missing = []
    for line in (req_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0].strip()
        if not name:
            continue
        try:
            md.version(name)
        except md.PackageNotFoundError:
            missing.append(line)
    return missing


def _ensure_requirements(zpath):
    """Garde-fou dépendances (repris de Bobi.Studio) : une version qui AJOUTE une dépendance
    Python casserait le démarrage au redémarrage (import au chargement). On lit le
    requirements.txt DU ZIP et on installe ce qui manque AVANT de toucher au code ; en cas
    d'échec (site hors ligne, pip cassé), la mise à jour est REFUSÉE et l'instance reste sur
    son code actuel. → (ok, msg)"""
    try:
        with zipfile.ZipFile(zpath) as zf:
            if "requirements.txt" not in zf.namelist():
                return True, ""
            req_text = zf.read("requirements.txt").decode("utf-8", "replace")
    except Exception as e:
        return False, f"requirements.txt illisible dans l'archive : {e}"
    missing = _missing_requirements(req_text)
    if not missing:
        return True, ""
    pip = os.path.join(ROOT, "venv", "bin", "pip")
    if not os.path.exists(pip):
        return False, ("dépendances manquantes ({}) et venv/bin/pip introuvable — les installer "
                       "à la main puis relancer la mise à jour".format(", ".join(missing)))
    log.info("mise à jour : installation des dépendances manquantes : %s", ", ".join(missing))
    try:
        r = subprocess.run([pip, "install", "--no-input", *missing],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, f"pip install {' '.join(missing)} : {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return False, ("dépendances manquantes non installables ({}) — accès Internet ou miroir "
                       "pip requis. {}".format(", ".join(missing), " | ".join(tail)))
    still = _missing_requirements(req_text)
    if still:
        return False, "dépendances toujours manquantes après installation : " + ", ".join(still)
    return True, ""


def restart_service():
    """Relance le service hors de notre cgroup (sinon le restart nous tue avant l'heure)."""
    try:
        subprocess.Popen(["systemd-run", "--on-active=2s", "--quiet",
                          "systemctl", "restart", SERVICE])
        return
    except FileNotFoundError:
        pass
    subprocess.Popen(["bash", "-c", f"sleep 2; systemctl restart {SERVICE}"],
                     start_new_session=True)


def apply_update(source_url, token, install_new=None):
    """Tire le code depuis `source_url`, vérifie le checksum et les dépendances, sauvegarde,
    applique, relance. Renvoie (ok, msg). Le restart est asynchrone.

    COMPOSITION PAR INSTANCE (comme Bobi.Studio) : le zip de mise à jour contient TOUS les
    plugins/services de la source, mais on n'applique que le cœur + les composants DÉJÀ
    INSTALLÉS ici — un outil n'apparaît jamais tout seul sur un site. `install_new` =
    opt-in explicite ({"plugins": [ids], "services": [ids]}, coché dans l'aperçu)."""
    inew = install_new or {}
    new_p = {str(x) for x in (inew.get("plugins") or [])}
    new_s = {str(x) for x in (inew.get("services") or [])}
    base = source_url.rstrip("/")
    try:
        man = fetch_manifest(base, token)
    except Exception as e:
        return False, f"manifeste injoignable : {e}"
    expected = man.get("sha256")
    if not expected:
        return False, "manifeste sans sha256"

    tmpdir = tempfile.mkdtemp(prefix="btupd-")
    try:
        zpath = os.path.join(tmpdir, "bobitools.zip")
        try:
            _download(base + "/api/update/download", token, zpath)
        except Exception as e:
            return False, f"téléchargement échoué : {e}"
        got = sha256_file(zpath)
        if got != expected:
            return False, f"checksum invalide (attendu {expected[:12]}…, reçu {got[:12]}…)"
        with zipfile.ZipFile(zpath) as zf:   # sanity : le zip doit contenir main.py
            if "main.py" not in zf.namelist():
                return False, "archive invalide (main.py absent)"

        ok_req, req_msg = _ensure_requirements(zpath)
        if not ok_req:
            return False, req_msg

        loc_p, loc_s = local_component_ids()
        src_p = {p.get("type") for p in (man.get("plugins") or []) if p.get("type")}
        src_s = {x.get("id") for x in (man.get("services") or []) if x.get("id")}
        skip_p = {p for p in src_p if p not in loc_p and p not in new_p}
        skip_s = {x for x in src_s if x not in loc_s and x not in new_s}

        backup_code()
        _extract_over(zpath, skip_plugins=skip_p, skip_services=skip_s)
        with open(PENDING_PATH, "w") as f:
            f.write(man.get("build_id") or man.get("label") or "?")
        record_deploy(man.get("build_id"), man.get("label"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    restart_service()
    extra = ""
    added = sorted((new_p & src_p) | (new_s & src_s))
    if added:
        extra += f" · nouveaux composants installés : {', '.join(added)}"
    if skip_p or skip_s:
        extra += f" · {len(skip_p) + len(skip_s)} composant(s) non installé(s) ici ignoré(s)"
    return True, f"mise à jour appliquée (v {man.get('label') or '?'}) — redémarrage en cours{extra}"


def apply_zip(zpath):
    """Applique un paquet .zip LOCAL (import hors-ligne depuis une autre instance, sans
    réseau). Même effet qu'`apply_update` mais sans pair HTTP : sanity main.py, sauvegarde,
    extraction préservant l'état local, marqueur PENDING, relance. Renvoie (ok, msg).

    Cas d'usage broadcast : une instance sans Internet reçoit un zip construit ailleurs
    (`/api/deploy/build` → `/download`) et l'importe via l'UI / clé USB."""
    try:
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
            if "main.py" not in names:                 # sanity : vrai paquet Bobi.Tools
                return False, "archive invalide (main.py absent)"
            label = build_id = None
            for cand in ("build_info.json", "build_manifest.json"):
                if cand in names:
                    try:
                        info = json.loads(zf.read(cand).decode())
                        label, build_id = info.get("label"), info.get("build_id")
                    except Exception:
                        pass
                    break
    except zipfile.BadZipFile:
        return False, "fichier .zip illisible"

    ok_req, req_msg = _ensure_requirements(zpath)
    if not ok_req:
        return False, req_msg

    backup_code()
    _extract_over(zpath)
    with open(PENDING_PATH, "w") as f:
        f.write(build_id or label or "?")
    record_deploy(build_id, label)
    restart_service()
    return True, f"paquet importé{(' (v ' + label + ')') if label else ''} — redémarrage en cours"


def rollback():
    """Restaure le dernier backup puis relance le service."""
    bk = latest_backup()
    if not bk:
        return False, "aucun backup disponible"
    with tarfile.open(bk, "r:gz") as tar:
        tar.extractall(ROOT, filter="data")   # filter='data' : refuse chemins hors ROOT
    clear_pending()
    restart_service()
    return True, f"rollback depuis {os.path.basename(bk)} — redémarrage en cours"


def record_deploy(build_id=None, label=None):
    """Mémorise la date/heure du déploiement appliqué sur CETTE instance."""
    try:
        with open(DEPLOY_INFO_PATH, "w") as f:
            json.dump({"deployed_at": datetime.now().isoformat(timespec="seconds"),
                       "build_id": build_id, "label": label}, f, indent=2)
    except Exception as e:
        log.debug("record_deploy: %s", e)


def deploy_info():
    """Infos du dernier déploiement appliqué localement, ou {} si jamais déployé (dev)."""
    try:
        with open(DEPLOY_INFO_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def clear_pending():
    try:
        os.remove(PENDING_PATH)
    except FileNotFoundError:
        pass


def pending_build_id():
    """Renvoie le build_id ciblé par une mise à jour en attente de validation, ou None."""
    try:
        with open(PENDING_PATH) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def confirm_boot_ok():
    """Appelée au démarrage : si l'app remonte, la mise à jour en attente est validée."""
    if os.path.exists(PENDING_PATH):
        log.info("Mise à jour confirmée au boot (build %s)", pending_build_id())
        clear_pending()
