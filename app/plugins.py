# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Tool plugin registry for Bobi.Tools.

A *tool* is a self-contained, versioned plugin living in `plugins/<type>/`:

    plugins/<type>/
      plugin.json     # manifest (see REQUIRED_KEYS below)
      page.html       # UI fragment mounted full-page by the shell / focus view
      page.js         # registers window.BTTools["<type>"] = { mount(el, ctx), unmount() }
      page.css        # optional
      backend.py      # optional (runtime=inprocess): def api(path, method, payload, ctx) -> (status, data)
      Dockerfile      # optional (runtime=docker): self-contained tool image
      i18n/<code>.json  # optional, keys prefixed plugin.<type>.*
      meta.json       # optional changelog/version metadata
      versions/<ver>/ # optional archived versions

Two runtimes, declared by the manifest `runtime` field:
  - "inprocess" (default): pure-frontend, or a backend.py loaded in-process and called
    via /api/tools/<type>/<path>.
  - "docker": the tool runs as a local Docker container; the orchestrator proxies the
    same /api/tools/<type>/<path> calls to the container's HTTP port.

This module is the descendant of Bobi.Studio's container-plugin registry: the scan,
versioning, package import/export and nav-section machinery are reused verbatim; the
ST 2110 wiring/Proxmox bits are dropped and a backend loader + runtime helpers are added.

New-code naming is English by project rule (the rest of the code is French).
"""
import importlib.util
import json
import logging
import os
import re
import shutil

log = logging.getLogger(__name__)

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")

REQUIRED_KEYS = ("type", "label", "version")

# Section de nav par défaut pour un outil qui ne déclare pas de `nav.section` — au
# contraire de Studio (où l'absence de nav masquait le plugin), tout outil de
# Bobi.Tools apparaît au lanceur, regroupé au minimum sous « Outils ».
DEFAULT_SECTION = {"section": "outils", "label": "Outils", "order": 100}

# type -> manifest dict (augmented with "_dir" = absolute plugin directory, "_backend"
# = loaded backend module or None).
REGISTRY = {}

# nom_dossier -> raison (plugins présents sur disque mais NON chargés au dernier scan).
SCAN_ERRORS = {}


class Ctx(dict):
    """Contexte passé au backend in-process d'un outil. Reste un dict (accès par clé
    `ctx["store"]`, `ctx.get(...)`) MAIS autorise aussi l'accès par attribut `ctx.store` —
    les backends mélangent les deux styles selon les plugins (cf. notes vs api_explorer)."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

_SAFE_TYPE = re.compile(r"^[A-Za-z0-9_-]+$")
# `version` sert à construire des chemins (versions/<ver>/) : on le borne comme `type`
# (pas de séparateur ni de « .. ») pour interdire toute traversée de répertoire.
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _scan():
    """(Re)scan PLUGINS_DIR. Returns {type: manifest}. Tolerant: a broken plugin is
    logged, recorded in SCAN_ERRORS, and skipped — never crashes startup."""
    found = {}
    SCAN_ERRORS.clear()
    if not os.path.isdir(PLUGINS_DIR):
        log.info("plugins: no plugins/ directory (%s)", PLUGINS_DIR)
        return found
    for name in sorted(os.listdir(PLUGINS_DIR)):
        pdir = os.path.join(PLUGINS_DIR, name)
        manifest_path = os.path.join(pdir, "plugin.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            log.error("plugins: bad manifest %s: %s", manifest_path, e)
            SCAN_ERRORS[name] = f"manifeste JSON invalide : {e}"
            continue
        missing = [k for k in REQUIRED_KEYS if k not in manifest]
        if missing:
            log.error("plugins: %s missing keys %s — skipped", manifest_path, missing)
            SCAN_ERRORS[name] = f"clés manquantes dans plugin.json : {', '.join(missing)}"
            continue
        if not _SAFE_TYPE.match(str(manifest.get("type", ""))):
            SCAN_ERRORS[name] = "type invalide (autorisé : lettres, chiffres, _ et -)"
            continue
        manifest["_dir"] = pdir
        manifest["_backend"] = _load_backend(pdir, manifest["type"]) \
            if _runtime_of(manifest) == "inprocess" else None
        found[manifest["type"]] = manifest
        log.info("plugins: loaded %s v%s (%s)", manifest["type"], manifest["version"],
                 _runtime_of(manifest))
    return found


def _runtime_of(manifest):
    r = str((manifest or {}).get("runtime") or "inprocess").lower()
    return r if r in ("inprocess", "docker") else "inprocess"


def _load_backend(plugin_dir, type_):
    """Charge backend.py (in-process). Retourne le module, ou None si absent/invalide.
    C'est l'UNIQUE code plugin exécuté dans le process de l'app : un backend.py expose
    `def api(path, method, payload, ctx)`. Les erreurs sont loguées sans bloquer le
    chargement de l'outil (il fonctionnera en front-pur)."""
    backend_path = os.path.join(plugin_dir, "backend.py")
    if not os.path.isfile(backend_path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"bt_tool_{type_}_backend", backend_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        log.info("plugins: backend loaded for %s", type_)
        return mod
    except Exception as e:
        log.warning("plugins: %s backend.py ignoré (%s)", type_, e)
        return None


def reload():
    global REGISTRY
    REGISTRY = _scan()
    # Un plugin ajouté/retiré change le CATALOGUE des permissions : on recharge les rôles
    # (distribution unique des permissions nouvelles à leurs default_roles).
    try:
        from .auth import recharger_roles
        recharger_roles()
    except Exception as e:
        log.warning("plugins: rechargement des rôles impossible : %s", e)
    return REGISTRY


def scan_errors():
    return dict(SCAN_ERRORS)


# ─── Accès registre ─────────────────────────────────────────

def get(type_):
    return REGISTRY.get(type_)


def all():
    return list(REGISTRY.values())


def is_tool(type_):
    return type_ in REGISTRY


# ─── Dépendances entre outils ───────────────────────────────
# Un outil peut consommer l'inventaire d'un autre via `docker.shared_volumes`. Ce montage
# est SOUPLE par nature : Docker crée un volume vide si l'outil propriétaire n'existe pas,
# le conteneur démarre sans broncher et le consommateur voit… rien. Indiscernable d'un
# inventaire réellement vide.
#
# `requires` déclare la dépendance DURE, celle sans laquelle l'outil n'a rien à faire :
#     "requires": [{ "type": "nmos_parc", "reason": "parc NMOS partagé" }]
# (la forme courte "requires": ["nmos_parc"] est acceptée)
#
# On avertit sans bloquer : empêcher le démarrage empêcherait aussi l'outil d'afficher sa
# propre explication, ce qui rend le diagnostic plus difficile, pas plus simple.

def requirements(type_):
    """Dépendances dures déclarées par l'outil : [{"type", "reason"}]."""
    out = []
    for req in (REGISTRY.get(type_) or {}).get("requires") or []:
        if isinstance(req, str):
            out.append({"type": req, "reason": ""})
        elif isinstance(req, dict) and req.get("type"):
            out.append({"type": req["type"], "reason": req.get("reason") or ""})
    return out


def missing_requirements(type_):
    """Dépendances déclarées mais absentes du registre (outil non installé)."""
    return [r for r in requirements(type_) if r["type"] not in REGISTRY]


def runtime(type_):
    """Runtime déclaré : 'inprocess' (défaut) ou 'docker'."""
    return _runtime_of(REGISTRY.get(type_) or {})


def backend(type_):
    """Module backend in-process de l'outil (ou None)."""
    return (REGISTRY.get(type_) or {}).get("_backend")


# ─── Désactivation (politique orchestrateur, en settings) ───

def _disabled():
    try:
        from . import settings
        raw = settings.get("tools_disabled")
        return set(raw) if isinstance(raw, list) else set()
    except Exception:
        return set()


def is_disabled(type_):
    return type_ in _disabled()


def set_disabled(type_, flag):
    from . import settings
    cur = _disabled()
    cur.add(type_) if flag else cur.discard(type_)
    settings.set("tools_disabled", sorted(cur))
    return sorted(cur)


def delete_plugin(type_):
    """Supprime le dossier du plugin sur disque + nettoie son état désactivé + reload."""
    m = REGISTRY.get(type_)
    if not m:
        raise ValueError("type inconnu")
    shutil.rmtree(m["_dir"])
    set_disabled(type_, False)
    reload()
    return type_


# ─── Nav / sections ─────────────────────────────────────────

def _nav_of(m):
    """Nav effective d'un outil : son `nav` complété par les défauts (section « outils »)."""
    nav = dict(DEFAULT_SECTION)
    nav.update(m.get("nav") or {})
    nav.setdefault("route", f"/tools/{nav['section']}")
    return nav


def sections(user=None):
    """Group enabled tools by nav.section, sorted by nav.order. Si `user` est fourni,
    ne garde que les outils auxquels il a accès (contrôle par outil).

    Returns {section_id: {label, route, order, plugins: [manifest, ...]}}."""
    out = {}
    dis = _disabled()
    can = None
    if user is not None:
        from .auth import can_access_tool
        can = lambda t: can_access_tool(t, user)
    for m in REGISTRY.values():
        t = m.get("type")
        if t in dis:
            continue
        if can and not can(t):
            continue
        nav = _nav_of(m)
        sec = nav["section"]
        entry = out.setdefault(sec, {
            "label": nav.get("label", sec.capitalize()),
            "route": nav.get("route", f"/tools/{sec}"),
            "order": nav.get("order", 100),
            "plugins": [],
        })
        entry["plugins"].append(m)
        entry["order"] = min(entry["order"], nav.get("order", 100))
    for entry in out.values():
        entry["plugins"].sort(key=lambda m: _nav_of(m).get("order", 100))
    return out


def config_schemas():
    """{type: config_schema} pour les outils déclarant un `config_schema`."""
    return {t: m["config_schema"] for t, m in REGISTRY.items() if m.get("config_schema")}


def manifest_summary_for_js():
    """Résumé compact des manifestes pour window.BT_TOOLS (métadonnées UI seules :
    label, badge, runtime, section). Aucun chemin ni code plugin inclus."""
    out = {}
    for t, m in REGISTRY.items():
        badge = m.get("badge") or {}
        nav = _nav_of(m)
        out[t] = {
            "label":       m.get("label", t),
            "runtime":     runtime(t),
            "badge_class": badge.get("class") or "",
            "badge_label": badge.get("label") or m.get("label", t),
            "badge_oklch": badge.get("oklch") or "",
            "nav_section": nav.get("section") or "",
        }
    return out


def badge_css_vars():
    """{badge_class: oklch} dédupliqué pour générer le bloc <style> badge dans layout.html."""
    seen = {}
    for m in REGISTRY.values():
        badge = m.get("badge") or {}
        cls = badge.get("class") or ""
        oklch = badge.get("oklch") or ""
        if cls and oklch and cls not in seen:
            seen[cls] = oklch
    return seen


def coerce_config(type_, params):
    """Coerce/borne `params` selon le `config_schema` du manifeste (optionnel).
    Types : number (min/max), checkbox (bool), select (option valide), text/textarea.
    Les clés hors schéma sont laissées telles quelles. Repris de Bobi.Studio."""
    m = REGISTRY.get(type_) or {}
    schema = m.get("config_schema") or []
    if not schema:
        return params
    out = dict(params or {})
    for f in schema:
        k = f.get("key")
        if not k or k not in out:
            continue
        t = f.get("type")
        v = out[k]
        if t == "number":
            try: v = float(v)
            except (TypeError, ValueError): v = float(f.get("default") or 0)
            if f.get("min") is not None: v = max(float(f["min"]), v)
            if f.get("max") is not None: v = min(float(f["max"]), v)
            out[k] = int(v) if float(v).is_integer() else v
        elif t in ("checkbox", "bool"):
            if isinstance(v, str):
                out[k] = v.strip().lower() in ("1", "true", "yes", "on")
            else:
                out[k] = bool(v)
        elif t == "select":
            opts = [o.get("value") for o in (f.get("options") or [])]
            if opts and v not in opts:
                out[k] = f.get("default") if f.get("default") in opts else opts[0]
        else:  # text / textarea
            out[k] = "" if v is None else str(v)
    return out


# ─── Versions (archives) ────────────────────────────────────

def versions(type_):
    """Versions disponibles, de la plus récente à la plus ancienne : la COURANTE
    (manifest.version) + les ARCHIVÉES sous `versions/<ver>/`. Repris de Bobi.Studio."""
    m = REGISTRY.get(type_)
    if not m:
        return []
    cur = m.get("version")
    out = [cur]
    vdir = os.path.join(m["_dir"], "versions")
    if os.path.isdir(vdir):
        archived = [d for d in os.listdir(vdir)
                    if os.path.isdir(os.path.join(vdir, d))]

        def _key(v):
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return (v,)
        for v in sorted(archived, key=_key, reverse=True):
            if v not in out:
                out.append(v)
    return out


_META_DEFAULT = {"published_at": "", "imported_at": "",
                 "changes": [], "fixes": [], "known_bugs": []}


def _meta_path(type_, version=None):
    m = REGISTRY.get(type_)
    if not m:
        return None
    if not version or version == m.get("version"):
        return os.path.join(m["_dir"], "meta.json")
    return os.path.join(m["_dir"], "versions", version, "meta.json")


def _coerce_meta_list(v):
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []


def read_version_meta(type_, version=None):
    """Métadonnées d'une version : dict complet (défauts vides si absent/illisible).
    Lecture JSON seule — aucun code plugin exécuté. Repris de Bobi.Studio."""
    out = dict(_META_DEFAULT)
    out["changes"], out["fixes"], out["known_bugs"] = [], [], []
    p = _meta_path(type_, version)
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
        except (ValueError, OSError):
            raw = None
        if isinstance(raw, dict):
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


def versions_meta(type_):
    cur = (REGISTRY.get(type_) or {}).get("version")
    return [{"version": v, "current": v == cur, **read_version_meta(type_, v)}
            for v in versions(type_)]


def ui_asset_path(type_, key, version=None):
    """Absolute path to a UI asset declared under manifest.ui[key], sanitized to stay
    inside the plugin directory. If version is given, looks in versions/<version>/ first.
    Returns None if absent/escaping. Repris de Bobi.Studio."""
    m = REGISTRY.get(type_)
    if not m:
        return None
    rel = (m.get("ui") or {}).get(key)
    if not rel:
        return None
    base = os.path.realpath(m["_dir"])
    if version:
        vpath = os.path.realpath(os.path.join(base, "versions", version, rel))
        if vpath.startswith(base + os.sep) and os.path.isfile(vpath):
            return vpath
    path = os.path.realpath(os.path.join(base, rel))
    if not (path == base or path.startswith(base + os.sep)):
        return None
    return path if os.path.isfile(path) else None


# ─── Import / export / activation de paquets .bobitool ──────

def _ver_key(v):
    try:
        return (0, tuple(int(x) for x in str(v).split(".")))
    except (ValueError, AttributeError):
        return (1, (str(v),))


def validate_package(d):
    """Valide un dossier de plugin extrait. Retourne (manifest, None) ou (None, raison).
    Ne fait que LIRE le manifeste (aucune exécution de code plugin)."""
    mp = os.path.join(d, "plugin.json")
    if not os.path.isfile(mp):
        return None, "plugin.json manquant"
    try:
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
    except Exception as e:
        return None, f"plugin.json invalide : {e}"
    missing = [k for k in REQUIRED_KEYS if k not in man]
    if missing:
        return None, f"clés manquantes : {', '.join(missing)}"
    if not _SAFE_TYPE.match(str(man.get("type", ""))):
        return None, "type invalide (autorisé : lettres, chiffres, _ et -)"
    if not _SAFE_VERSION.match(str(man.get("version", ""))):
        return None, "version invalide (autorisé : lettres, chiffres, ., _ et -)"
    return man, None


def export_dir(type_):
    m = REGISTRY.get(type_)
    return m["_dir"] if m else None


def export_version_dir(type_, version):
    """Dossier source pour zipper UNE version : dossier plat si version courante,
    sinon versions/<ver>/. Retourne (dir, version) ou (None, None)."""
    m = REGISTRY.get(type_)
    if not m or version not in versions(type_):
        return None, None
    if version == m.get("version"):
        return m["_dir"], version
    return os.path.join(m["_dir"], "versions", version), version


def stamp_imported_at(src_dir):
    """Tamponne imported_at (maintenant, ISO secondes) dans src_dir/meta.json."""
    import datetime
    p = os.path.join(src_dir, "meta.json")
    data = {}
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            data = {}
    data["imported_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _flat_names(d):
    return [n for n in os.listdir(d) if n != "versions"]


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
    """Installe un paquet validé (dossier `src_dir`) dans PLUGINS_DIR/<type>/.
    activate=True → devient la version COURANTE (archive l'ancienne d'abord).
    activate=False → rangé sous versions/<ver>/. Recharge le registre. Repris de Studio."""
    man, err = validate_package(src_dir)
    if err:
        raise ValueError(err)
    type_, ver = man["type"], man["version"]
    tdir = os.path.join(PLUGINS_DIR, type_)
    cur = (REGISTRY.get(type_) or {}).get("version") if os.path.isdir(tdir) else None
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
    return {"type": type_, "version": ver}


def activate_version(type_, version):
    """Promeut une version archivée en version COURANTE (archive la courante d'abord).
    Recharge le registre. Repris de Bobi.Studio."""
    m = REGISTRY.get(type_)
    if not m:
        raise ValueError("type inconnu")
    if not _SAFE_VERSION.match(str(version or "")):
        raise ValueError("version invalide")
    tdir, cur = m["_dir"], m.get("version")
    if version == cur:
        return {"type": type_, "version": version}
    vdir = os.path.join(tdir, "versions", version)
    if not os.path.isdir(vdir):
        raise ValueError(f"version {version} introuvable")
    _archive_current(tdir, cur)
    _clear_flat(tdir)
    _copy_into(vdir, tdir, os.listdir(vdir))
    mp = os.path.join(tdir, "plugin.json")
    if not os.path.isfile(mp):                 # archive sans manifeste → reconstruit
        man = {k: v for k, v in m.items() if not k.startswith("_")}
        man["version"] = version
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
    else:
        with open(mp, encoding="utf-8") as f:
            man = json.load(f)
        if man.get("version") != version:
            man["version"] = version
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(man, f, ensure_ascii=False, indent=2)
    reload()
    return {"type": type_, "version": version}


# Initial scan at import time.
REGISTRY = _scan()
