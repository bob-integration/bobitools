# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Génération du paquet de distribution (`dist/bobitools.zip`).

Source unique de vérité du build : appelée par l'UI (Réglages → Déploiement), par le
wrapper CLI `tools/build_dist.py`, et par l'updater inter-instances (app/updater.py).
Produit un zip ne contenant QUE le code (cœur + plugins/services sélectionnés), à
l'exclusion stricte de tout secret ou état local (`config_local.py`, base `*.db` et ses
annexes -wal/-shm/-journal, `backups/`, venv, caches…).

Garde-fou : après écriture, le zip est relu et le build échoue si un motif sensible y est
détecté — on n'expose jamais une base de données ni la config locale.

Dérivé du builder de Bobi.Studio ; métier broadcast (images runtime, node-agent) retiré.
"""
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime

# Racine du dépôt = parent de app/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(ROOT, "dist")
DEFAULT_DEST = os.path.join(DIST_DIR, "bobitools.zip")
MANIFEST_PATH = os.path.join(DIST_DIR, "build_manifest.json")
# Identité de build embarquée dans le zip (lue par une instance pour rapporter sa version).
BUILD_INFO_PATH = os.path.join(ROOT, "build_info.json")

# ── Cœur : toujours inclus ───────────────────────────────────
# `tools` embarque create_admin.py (référencé par install.sh) et build_dist.py.
CORE_DIRS = ["app", "templates", "static", "i18n", "tools"]
CORE_FILES = [
    "main.py", "requirements.txt", "bobitools.service", "config_local.example.py",
    "install.sh", "release.sh", "build_info.json",
    "LICENSE", "CHANGELOG.md", "CLAUDE.md", "README.md", "DESIGN.md", "DEPLOY.md",
    "EMBERPLUS-IPG.md", "PASSATION-TSL-CDE-NEWT.md",
]

# Sélection par défaut : None → TOUS les plugins/services installés (cf. _resolve_*).
DEFAULT_PLUGINS = None
DEFAULT_SERVICES = None

# ── Exclusions (n'importe où dans le chemin relatif) ─────────
# Secrets / état local : JAMAIS dans le zip.
# + toute base SQLite (cf. _is_secret). `coffre.key` est la clé maître du coffre à
# identifiants : la laisser partir dans un zip rendrait déchiffrable toute base copiée.
SECRET_PATTERNS = ["config_local.py", "backups/", "coffre.key"]

EXCLUDE_DIRS = {
    "venv", ".git", "__pycache__", ".claude", ".agents", ".impeccable",
    "old", "dist", "node_modules", ".pytest_cache", "backups",
    # Archives de rollback LOCALES (plugins/services) : hors distribution, comme l'export
    # plugin. Le zip ne porte que le code courant. Rollback global → backup updater.
    "versions",
}
EXCLUDE_FILES = {
    "config_local.py", "push_to_github.sh", "skills-lock.json", ".impeccable",
    "coffre.key",
}
EXCLUDE_EXT = {".db", ".log", ".pyc", ".pyo"}
# Annexes SQLite : db_bobitools.db-wal / -shm / -journal (ne finissent pas par .db).
_DB_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


def _is_db(name):
    low = name.lower()
    return low.endswith(".db") or low.endswith(_DB_SIDECAR_SUFFIXES)


def _is_secret(rel):
    """Vrai si le chemin relatif est un secret/état local interdit (garde-fou)."""
    low = rel.replace("\\", "/").lower()
    if _is_db(low):
        return True
    return any(p in low for p in (p_.lower() for p_ in SECRET_PATTERNS))


def _excluded(rel):
    """Vrai si le chemin relatif doit être écarté du zip."""
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    base = parts[-1]
    if base in EXCLUDE_FILES:
        return True
    if _is_db(base):
        return True
    if os.path.splitext(base)[1].lower() in EXCLUDE_EXT:
        return True
    return False


def _add_dir(zf, abs_dir, arc_prefix):
    """Ajoute récursivement un dossier au zip en filtrant les exclusions."""
    for dirpath, dirnames, filenames in os.walk(abs_dir):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            absf = os.path.join(dirpath, fn)
            rel = os.path.relpath(absf, abs_dir)
            arc = os.path.join(arc_prefix, rel)
            if _excluded(arc):
                continue
            zf.write(absf, arc)


def _git_hash():
    """Short hash git du dépôt, ou None hors dépôt git."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except Exception:
        return None


def current_build_info():
    """Identité de build de CETTE instance (build_info.json), ou un repère 'dev' si absent."""
    try:
        with open(BUILD_INFO_PATH) as f:
            return json.load(f)
    except Exception:
        gh = _git_hash()
        return {"build_id": None, "label": f"dev{(' ' + gh) if gh else ''}",
                "built_at": None, "git_hash": gh}


_identity_cache = {"at": 0.0, "info": None}


def identity():
    """Identité de build À JOUR de cette instance — celle qu'on affiche (menu « ? »), qu'on
    annonce aux pairs (ping, manifeste) et qu'on embarque dans le zip de mise à jour.

    Sur un dépôt git (mise à jour par `git pull`), build_info.json n'est réécrit qu'à un
    build explicite : il annonçait donc la date d'un zip déjà dépassé, et deux instances au
    même commit ne se reconnaissaient pas. Quand HEAD a bougé depuis, on le RE-TAMPONNE
    (nouvel id, libellé « date · hash »). Hors git (instance installée par zip), on le lit
    tel quel. Cache de 60 s : un `git rev-parse` par page rendue serait du gaspillage."""
    import time
    now = time.time()
    if _identity_cache["info"] is not None and now - _identity_cache["at"] < 60:
        return _identity_cache["info"]
    info = current_build_info()
    if os.path.exists(os.path.join(ROOT, ".git")):
        gh = _git_hash()
        if gh and gh != info.get("git_hash"):
            built_at = datetime.now().isoformat(timespec="seconds")
            info = {"build_id": uuid.uuid4().hex, "label": f"{built_at} · {gh}",
                    "built_at": built_at, "git_hash": gh}
            try:
                with open(BUILD_INFO_PATH, "w") as f:
                    json.dump(info, f, indent=2)
            except Exception:
                pass
    _identity_cache.update(at=now, info=info)
    return info


def available():
    """Liste les plugins et services installés, pour la sélection UI."""
    from . import plugins, core_plugins
    plugs = [{"type": m.get("type"), "label": m.get("label") or m.get("type"),
              "version": m.get("version", "")} for m in plugins.all()]
    plugs.sort(key=lambda p: p["type"] or "")
    servs = [{"id": m.get("id"), "label": m.get("label") or m.get("id"),
              "description": m.get("description", ""), "version": m.get("version", "")}
             for m in core_plugins.all()]
    servs.sort(key=lambda s: s["id"] or "")
    return {"plugins": plugs, "services": servs}


def _resolve_plugins(plugins):
    installed = [p["type"] for p in available()["plugins"] if p.get("type")]
    if plugins is not None:
        # Ne garder que des noms réellement installés : une sélection venue de l'API ne
        # doit pas pouvoir injecter un chemin arbitraire (« ../ ») dans le zip.
        wanted = set(plugins)
        return [t for t in installed if t in wanted]
    return installed


def _resolve_services(services):
    installed = [s["id"] for s in available()["services"] if s.get("id")]
    if services is not None:
        wanted = set(services)
        return [s for s in installed if s in wanted]
    return installed


def last_selection():
    """Dernière sélection utilisée (build_manifest.json) ou « tout l'installé »."""
    try:
        with open(MANIFEST_PATH) as f:
            m = json.load(f)
        return {"plugins": m.get("plugins") or _resolve_plugins(None),
                "services": m.get("services") or _resolve_services(None),
                "built_at": m.get("built_at")}
    except Exception:
        return {"plugins": _resolve_plugins(None),
                "services": _resolve_services(None), "built_at": None}


def build(plugins=None, services=None, dest=DEFAULT_DEST, stamp=True):
    """Construit le zip de distribution. Retourne un dict résumé.

    plugins/services : listes d'ids à inclure (None → TOUS les installés).
    stamp : si True (build explicite = nouvelle release), (re)génère l'identité de build
    (build_info.json). Si False (build paresseux pour servir un zip manquant), réutilise
    l'identité existante afin que la version de l'instance reste stable.
    Lève RuntimeError si le garde-fou anti-secret se déclenche.
    """
    plugins = _resolve_plugins(plugins)
    services = _resolve_services(services)

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    # Identité de build : écrite AVANT le zip pour être embarquée (build_info.json ∈ CORE_FILES).
    existing = current_build_info() if os.path.exists(BUILD_INFO_PATH) else None
    if stamp or not existing or not existing.get("build_id"):
        built_at = datetime.now().isoformat(timespec="seconds")
        git_hash = _git_hash()
        build_id = uuid.uuid4().hex
        label = built_at + (f" · {git_hash}" if git_hash else "")
        try:
            with open(BUILD_INFO_PATH, "w") as f:
                json.dump({"build_id": build_id, "label": label,
                           "built_at": built_at, "git_hash": git_hash}, f, indent=2)
        except Exception:
            pass
    else:
        build_id = existing["build_id"]
        label = existing.get("label")
        built_at = existing.get("built_at")
        git_hash = existing.get("git_hash")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in CORE_DIRS:
            absd = os.path.join(ROOT, d)
            if os.path.isdir(absd):
                _add_dir(zf, absd, d)
        for f in CORE_FILES:
            absf = os.path.join(ROOT, f)
            if os.path.isfile(absf) and not _excluded(f):
                zf.write(absf, f)
        for p in plugins:
            absd = os.path.join(ROOT, "plugins", p)
            if os.path.isdir(absd):
                _add_dir(zf, absd, os.path.join("plugins", p))
        for s in services:
            absd = os.path.join(ROOT, "services", s)
            if os.path.isdir(absd):
                _add_dir(zf, absd, os.path.join("services", s))
        # services/__init__.py est nécessaire pour que `services` soit un package.
        init_abs = os.path.join(ROOT, "services", "__init__.py")
        if os.path.isfile(init_abs):
            zf.write(init_abs, os.path.join("services", "__init__.py"))

    # ── Garde-fou : aucun secret ne doit avoir fui ───────────
    with zipfile.ZipFile(tmp) as zf:
        names = zf.namelist()
        leaked = [n for n in names if _is_secret(n)]
    if leaked:
        os.remove(tmp)
        raise RuntimeError("Build refusé — fichiers sensibles détectés dans le zip : "
                           + ", ".join(leaked[:10]))

    os.replace(tmp, dest)

    # Rafraîchir l'installeur servi à côté du zip (/install/install.sh).
    src_inst = os.path.join(ROOT, "install.sh")
    if os.path.isfile(src_inst):
        try:
            shutil.copy2(src_inst, os.path.join(DIST_DIR, "install.sh"))
        except Exception:
            pass

    size = os.path.getsize(dest)
    _identity_cache["info"] = None   # build_info.json a pu changer (stamp)
    # La « dernière sélection » est celle du zip de DISTRIBUTION choisi dans l'UI : le zip de
    # mise à jour (tout l'installé, construit à la volée pour un pair) ne doit pas l'écraser.
    if os.path.abspath(dest) != os.path.abspath(DEFAULT_DEST):
        return {"ok": True, "file": os.path.basename(dest), "path": dest,
                "size": size, "count": len(names),
                "plugins": plugins, "services": services, "built_at": built_at,
                "build_id": build_id, "label": label, "git_hash": git_hash}
    try:
        with open(MANIFEST_PATH, "w") as f:
            json.dump({"plugins": plugins, "services": services,
                       "built_at": built_at, "file": os.path.basename(dest),
                       "size": size, "count": len(names),
                       "build_id": build_id, "label": label, "git_hash": git_hash}, f, indent=2)
    except Exception:
        pass

    return {"ok": True, "file": os.path.basename(dest), "path": dest,
            "size": size, "count": len(names),
            "plugins": plugins, "services": services, "built_at": built_at,
            "build_id": build_id, "label": label, "git_hash": git_hash}
