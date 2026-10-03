# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des services globaux de Bobi.Tools — jumeau de app/plugins.py.

Scanne `services/` à la RACINE du dépôt : chaque sous-dossier contenant un
`manifest.json` valide est un service. Même mécanique que app/plugins.py pour les
outils (scan tolérant, versions/, import/export/activation de paquets .bobitool),
mais un service est un module Python chargé EN PROCESS (≠ proxy docker) qui peut :
  - démarrer au boot                 → def boot()
  - exposer ses propres routes API   → def register_routes(bp)
  - déclarer des réglages            → manifest.settings_keys (pilotent les defaults)

Contrat manifeste (REPRIS du registre d'outils, par cohérence Tools) : seuls
`id`, `label`, `version` sont requis. `settings_keys`, `nav_tab`, `tab_template`,
`order` sont optionnels (héritage Studio, enrichissent le rendu Réglages si présents).

New-code naming is English by project rule (cf. plugins.py).
"""
import datetime
import importlib
import json
import logging
import os
import re
import shutil
import sys

log = logging.getLogger(__name__)

SERVICES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services")

REQUIRED_KEYS = ("id", "label", "version")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# `version` sert à construire des chemins (versions/<ver>/) : borné comme `id` (+ « . »)
# pour interdire toute traversée de répertoire.
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# id -> {"manifest": dict, "module": module, "dir": abs path}
_registry = None
# nom_dossier -> raison (services présents sur disque mais NON chargés au dernier scan).
SCAN_ERRORS = {}


# ─── Scan & reload ──────────────────────────────────────────

def scan() -> dict:
    """(Re)scanne SERVICES_DIR. Tolérant : un service cassé est logué, rangé dans
    SCAN_ERRORS, et sauté — ne fait jamais planter le démarrage."""
    global _registry
    if _registry is not None:
        return _registry
    _registry = {}
    SCAN_ERRORS.clear()
    if not os.path.isdir(SERVICES_DIR):
        log.info("core_plugins: pas de dossier services/ (%s)", SERVICES_DIR)
        return _registry
    for name in sorted(os.listdir(SERVICES_DIR)):
        svc_dir = os.path.join(SERVICES_DIR, name)
        if not os.path.isdir(svc_dir):
            continue
        manifest_path = os.path.join(svc_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            log.error("core_plugins: manifeste illisible %s : %s", manifest_path, e)
            SCAN_ERRORS[name] = f"manifest.json invalide : {e}"
            continue
        missing = [k for k in REQUIRED_KEYS if k not in manifest]
        if missing:
            SCAN_ERRORS[name] = f"clés manquantes : {', '.join(missing)}"
            continue
        if not _SAFE_ID.match(str(manifest.get("id", ""))):
            SCAN_ERRORS[name] = "id invalide (autorisé : lettres, chiffres, _ et -)"
            continue
        try:
            mod = importlib.import_module(f"services.{name}")
        except Exception as e:
            log.error("core_plugins: import services.%s échoué : %s", name, e)
            SCAN_ERRORS[name] = f"import du module échoué : {e}"
            continue
        manifest["_dir"] = svc_dir
        _registry[manifest["id"]] = {"manifest": manifest, "module": mod, "dir": svc_dir}
        log.info("core_plugins: chargé service %s v%s", manifest["id"], manifest["version"])
    return _registry


def reload(restart=True):
    """Invalide le cache et recharge tous les modules services.

    Un service n'est pas qu'un module : c'est un SINGLETON VIVANT (socket d'écoute, threads,
    état d'abonnés) tenu dans les globales de son module. Purger `sys.modules` sans plus de
    précaution laissait donc l'ancien module tourner — socket ouvert, threads actifs — pendant
    que le registre pointait sur un module neuf et inerte. Le contrôleur restait connecté à
    l'ancien provider, sans qu'aucune erreur ne le signale ; pire, un service comme SW-P-08,
    qui ne diffuse que sur événement et n'a aucun pousseur périodique, ne recevait alors plus
    jamais de mise à jour — divergence définitive du pupitre jusqu'au redémarrage de l'app.

    On arrête donc ce qui tournait AVANT la purge, et on ne redémarre QUE ce qui tournait,
    sur les modules neufs. `restart=False` sert aux appelants qui ne veulent qu'un état de
    version à jour et n'ont aucune raison de toucher au service en fonctionnement."""
    global _registry
    running = []
    if _registry:
        for sid, entry in _registry.items():
            mod = entry.get("module")
            if not mod:
                continue
            try:
                if getattr(mod, "is_running", lambda: False)():
                    running.append(sid)
            except Exception as e:                      # noqa: BLE001
                log.warning("core_plugins: %s.is_running() a levé : %s", sid, e)
            if hasattr(mod, "stop"):
                try:
                    mod.stop()
                except Exception as e:                  # noqa: BLE001
                    log.warning("core_plugins: %s.stop() a levé : %s", sid, e)
    _registry = None
    for key in list(sys.modules.keys()):
        if key.startswith("services.") and key != "services":
            del sys.modules[key]
    reg = scan()
    if restart:
        for sid in running:
            mod = (reg.get(sid) or {}).get("module")
            if mod and hasattr(mod, "boot"):
                try:
                    mod.boot()
                    log.info("core_plugins: service %s redémarré après rechargement", sid)
                except Exception as e:                  # noqa: BLE001
                    log.error("core_plugins: %s.boot() a échoué après rechargement : %s", sid, e)
    return reg


def scan_errors():
    return dict(SCAN_ERRORS)


# ─── Accès registre ─────────────────────────────────────────

def _entry(service_id):
    return scan().get(service_id)


def get(service_id):
    e = _entry(service_id)
    return e["manifest"] if e else None


def module(service_id):
    e = _entry(service_id)
    return e["module"] if e else None


def all():
    return [e["manifest"] for e in scan().values()]


def is_service(service_id) -> bool:
    return _entry(service_id) is not None


def boot_all():
    """Démarre chaque service exposant un boot() (appelé par main.py au lancement)."""
    for e in scan().values():
        fn = getattr(e["module"], "boot", None)
        if callable(fn):
            try:
                fn()
            except Exception as ex:
                log.error("core_plugins: boot(%s) : %s", e["manifest"]["id"], ex)


def register_all_routes(bp):
    """Monte les routes de chaque service exposant un register_routes(bp)."""
    for e in scan().values():
        fn = getattr(e["module"], "register_routes", None)
        if callable(fn):
            try:
                fn(bp)
            except Exception as ex:
                log.error("core_plugins: register_routes(%s) : %s",
                          e["manifest"]["id"], ex)


def all_settings_defaults() -> dict:
    """Defaults de tous les réglages déclarés par les services (manifest.settings_keys).
    Fusionnés dans app.settings.DEFAULTS → un service est autoritaire pour SES réglages."""
    defaults = {}
    for e in scan().values():
        for key, spec in (e["manifest"].get("settings_keys") or {}).items():
            defaults[key] = (spec or {}).get("default")
    return defaults


# ─── Désactivation (politique orchestrateur, en settings) ───

def _disabled():
    try:
        from . import settings
        raw = settings.get("services_disabled")
        return set(raw) if isinstance(raw, list) else set()
    except Exception:
        return set()


def is_disabled(service_id) -> bool:
    return service_id in _disabled()


def set_disabled(service_id, flag):
    from . import settings
    cur = _disabled()
    cur.add(service_id) if flag else cur.discard(service_id)
    settings.set("services_disabled", sorted(cur))
    return sorted(cur)


def delete_service(service_id):
    """Supprime le dossier du service sur disque + nettoie son état + reload."""
    e = _entry(service_id)
    if not e:
        raise ValueError("service inconnu")
    # Arrêt propre si le service expose un stop().
    fn = getattr(e["module"], "stop", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass
    shutil.rmtree(e["dir"])
    set_disabled(service_id, False)
    reload()
    return service_id


# ─── Versions (archives) ────────────────────────────────────

def _ver_key(v):
    try:
        return (0, tuple(int(x) for x in str(v).split(".")))
    except (ValueError, AttributeError):
        return (1, (str(v),))


def versions(service_id) -> list:
    """Versions disponibles : courante en tête, archivées décroissantes."""
    e = _entry(service_id)
    if not e:
        return []
    cur = e["manifest"].get("version")
    out = [cur]
    vdir = os.path.join(e["dir"], "versions")
    if os.path.isdir(vdir):
        archived = [d for d in os.listdir(vdir)
                    if os.path.isdir(os.path.join(vdir, d)) and
                    os.path.isfile(os.path.join(vdir, d, "manifest.json"))]
        for v in sorted(archived, key=_ver_key, reverse=True):
            if v not in out:
                out.append(v)
    return out


_META_DEFAULT = {"published_at": "", "imported_at": "",
                 "changes": [], "fixes": [], "known_bugs": []}


def _read_meta(path) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _coerce_meta_list(v):
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []


def read_version_meta(service_id, version=None):
    """Métadonnées d'une version : dict complet (défauts vides si absent/illisible).
    Lecture JSON seule. Même format que app/plugins.read_version_meta."""
    out = dict(_META_DEFAULT)
    out["changes"], out["fixes"], out["known_bugs"] = [], [], []
    e = _entry(service_id)
    if not e:
        return out
    cur = e["manifest"].get("version")
    if not version or version == cur:
        p = os.path.join(e["dir"], "meta.json")
    else:
        p = os.path.join(e["dir"], "versions", version, "meta.json")
    raw = _read_meta(p)
    if raw:
        out["published_at"] = raw.get("published_at") or raw.get("date") or ""
        out["imported_at"] = raw.get("imported_at", "")
        ch = raw.get("changes")
        if isinstance(ch, dict):
            out["changes"]    = _coerce_meta_list(ch.get("Nouveautés"))
            out["fixes"]      = _coerce_meta_list(ch.get("Corrections"))
            out["known_bugs"] = _coerce_meta_list(ch.get("Bugs connus"))
        else:
            out["changes"]    = _coerce_meta_list(ch)
            out["fixes"]      = _coerce_meta_list(raw.get("fixes"))
            out["known_bugs"] = _coerce_meta_list(raw.get("known_bugs"))
    return out


def versions_meta(service_id):
    cur = (get(service_id) or {}).get("version")
    return [{"version": v, "current": v == cur, **read_version_meta(service_id, v)}
            for v in versions(service_id)]


# ─── Import / export / activation de paquets .bobitool ──────

def validate_package(d):
    """Valide un dossier service extrait. Retourne (manifest, None) ou (None, raison).
    Ne fait que LIRE le manifeste (aucune exécution de code)."""
    mp = os.path.join(d, "manifest.json")
    if not os.path.isfile(mp):
        return None, "manifest.json manquant"
    try:
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
    except Exception as e:
        return None, f"manifest.json invalide : {e}"
    missing = [k for k in REQUIRED_KEYS if k not in man]
    if missing:
        return None, f"clés manquantes : {', '.join(missing)}"
    if not _SAFE_ID.match(str(man.get("id", ""))):
        return None, "id invalide (autorisé : lettres, chiffres, _ et -)"
    if not _SAFE_VERSION.match(str(man.get("version", ""))):
        return None, "version invalide (autorisé : lettres, chiffres, ., _ et -)"
    if not os.path.isfile(os.path.join(d, "__init__.py")):
        return None, "__init__.py manquant"
    return man, None


def export_dir(service_id):
    e = _entry(service_id)
    return e["dir"] if e else None


def export_version_dir(service_id, version):
    """Dossier source pour zipper UNE version : dossier plat si version courante,
    sinon versions/<ver>/. Retourne (dir, version) ou (None, None)."""
    e = _entry(service_id)
    if not e or version not in versions(service_id):
        return None, None
    if version == e["manifest"].get("version"):
        return e["dir"], version
    return os.path.join(e["dir"], "versions", version), version


def stamp_imported_at(src_dir):
    """Tamponne imported_at (maintenant, ISO secondes) dans src_dir/meta.json."""
    p = os.path.join(src_dir, "meta.json")
    data = _read_meta(p)
    data["imported_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _flat_names(d):
    return [n for n in os.listdir(d) if n != "versions" and n != "__pycache__"]


def _copy_into(src_dir, dst_dir, names):
    os.makedirs(dst_dir, exist_ok=True)
    for n in names:
        s = os.path.join(src_dir, n)
        dst = os.path.join(dst_dir, n)
        if os.path.isdir(s):
            shutil.copytree(s, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(s, dst)


def _clear_flat(d):
    for n in _flat_names(d):
        p = os.path.join(d, n)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


def _archive_current(tdir, cur_ver):
    if not cur_ver:
        return
    dest = os.path.join(tdir, "versions", cur_ver)
    if os.path.isdir(dest):
        return
    _copy_into(tdir, dest, _flat_names(tdir))


def _merge_versions(src_dir, tdir):
    vsrc = os.path.join(src_dir, "versions")
    if not os.path.isdir(vsrc):
        return
    for v in os.listdir(vsrc):
        s = os.path.join(vsrc, v)
        dst = os.path.join(tdir, "versions", v)
        if os.path.isdir(s) and not os.path.isdir(dst):
            shutil.copytree(s, dst)


def install_package(src_dir, *, activate):
    """Installe un paquet validé (dossier `src_dir`) dans SERVICES_DIR/<id>/.
    activate=True → devient la version COURANTE (archive l'ancienne d'abord).
    activate=False → rangé sous versions/<ver>/. Recharge le registre."""
    man, err = validate_package(src_dir)
    if err:
        raise ValueError(err)
    sid, ver = man["id"], man["version"]
    tdir = os.path.join(SERVICES_DIR, sid)
    cur = (get(sid) or {}).get("version") if os.path.isdir(tdir) else None
    if activate:
        os.makedirs(tdir, exist_ok=True)
        if cur and cur != ver:
            _archive_current(tdir, cur)
        _clear_flat(tdir)
        _copy_into(src_dir, tdir, _flat_names(src_dir))
        _merge_versions(src_dir, tdir)
    else:
        dest = os.path.join(tdir, "versions", ver)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        _copy_into(src_dir, dest, _flat_names(src_dir))
        _merge_versions(src_dir, tdir)
    reload()
    return {"id": sid, "version": ver}


def activate_version(service_id, version):
    """Promeut une version archivée en version COURANTE (archive la courante d'abord).
    Recharge le registre. Redémarrage de l'app requis pour recharger le code Python."""
    e = _entry(service_id)
    if not e:
        raise ValueError("service inconnu")
    if not _SAFE_VERSION.match(str(version or "")):
        raise ValueError("version invalide")
    tdir, cur = e["dir"], e["manifest"].get("version")
    if version == cur:
        return {"id": service_id, "version": version}
    vdir = os.path.join(tdir, "versions", version)
    if not os.path.isdir(vdir):
        raise ValueError(f"version {version} introuvable")
    _archive_current(tdir, cur)
    _clear_flat(tdir)
    _copy_into(vdir, tdir, os.listdir(vdir))
    mp = os.path.join(tdir, "manifest.json")
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
        if man.get("version") != version:
            man["version"] = version
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(man, f, ensure_ascii=False, indent=2)
    reload()
    return {"id": service_id, "version": version}


# Scan initial à l'import (cohérent avec app/plugins.py).
scan()
