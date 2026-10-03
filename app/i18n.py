# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Internationalisation (i18n) légère, sans gettext/babel (le repo n'a aucun build).

Catalogues JSON symétriques keyés par clés stables (une langue par fichier) :
  i18n/fr.json  → { "nav.home": "Accueil", … }   (source)
  i18n/en.json  → { "nav.home": "Home", … }       (traduction)
Plugins et services (sous-modules) embarquent les leurs dans <dir>/i18n/<code>.json
(clés préfixées plugin.<type>.* / service.<name>.*).

Le code référence des CLÉS, jamais le texte : `t("nav.home")`.

Couches de résolution (priorité décroissante) :
  1. surcouche DB (i18n_overrides)  ← éditée via l'UI, survit aux sync git
  2. fichier de la langue           ← cœur + plugins/services
  3. surcouche DB FR puis fichier FR ← repli langue source
  4. la clé brute                   ← jamais d'erreur d'affichage

Langues : built-in (fichiers : fr, en) + langues personnalisées (setting
`ui_custom_languages`, sans fichier → uniquement surcouche DB).

Cohérent sur les trois couches :
  - Python  : `from app.i18n import t ; t("alert.foo", vmid=42)`
  - Jinja2  : globals `t()` / `_()` (enregistrés dans main.py)
  - JS      : `window.I18N` (sous-ensemble `js.*`/`plugin.*` injecté par layout.html)
"""
import json
import logging
import os
import re

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_I18N_DIR = os.path.join(_ROOT, "i18n")
_CONTRIB_DIRS = (os.path.join(_ROOT, "plugins"), os.path.join(_ROOT, "services"))

DEFAULT_LANG = "fr"
LANG_CODE_RE = re.compile(r"^[a-z]{2}(-[a-z0-9]+)?$")

# Langues fournies (portées par des fichiers catalogue).
_BUILTIN = [
    {"code": "fr", "label": "Français", "builtin": True},
    {"code": "en", "label": "English",  "builtin": True},
]

# Recalculés à chaque _load().
LANGUAGES = list(_BUILTIN)
LANG_CODES = {l["code"] for l in _BUILTIN}

# Catalogues fichiers (cœur+contrib) : { code: {clé: texte} } — sans overrides.
_FILE_CATALOGS = {}
# Catalogues effectifs (fichiers + overrides DB appliqués) : { code: {clé: texte} }.
_CATALOGS = {}


def _read_catalog(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error(f"i18n: catalogue {path} illisible : {e}")
        return {}


def _custom_languages():
    """Langues personnalisées (setting). Tolérant si la DB n'est pas prête."""
    try:
        from . import settings as st
        raw = st.get("ui_custom_languages") or []
        out = []
        for it in raw:
            code = (it.get("code") or "").strip()
            if code and LANG_CODE_RE.match(code) and code not in {"fr", "en"}:
                out.append({"code": code,
                            "label": (it.get("label") or code).strip(),
                            "builtin": False})
        return out
    except Exception:
        return []


def _file_catalog_for(code):
    """Catalogue fichier d'une langue : cœur (autoritaire) + contributeurs."""
    core = _read_catalog(os.path.join(_I18N_DIR, f"{code}.json"))
    merged = {}
    for base in _CONTRIB_DIRS:
        try:
            names = sorted(os.listdir(base))
        except FileNotFoundError:
            continue
        for name in names:
            cdir = os.path.join(base, name, "i18n")
            if os.path.isdir(cdir):
                merged.update(_read_catalog(os.path.join(cdir, f"{code}.json")))
    merged.update(core)   # le cœur prime sur les contributeurs
    return merged


def _load():
    """(Re)charge langues, catalogues fichiers et surcouche DB. Idempotent."""
    global LANGUAGES, LANG_CODES, _FILE_CATALOGS, _CATALOGS

    LANGUAGES = list(_BUILTIN) + _custom_languages()
    LANG_CODES = {l["code"] for l in LANGUAGES}

    # Catalogues fichiers (uniquement pour les langues à fichier = built-in).
    _FILE_CATALOGS = {l["code"]: _file_catalog_for(l["code"])
                      for l in _BUILTIN}

    # Surcouche DB.
    try:
        from .database import db_i18n_overrides
        overrides = db_i18n_overrides()
    except Exception:
        overrides = {}

    # Catalogues effectifs = fichier + overrides (override prime).
    cats = {}
    for code in LANG_CODES:
        eff = dict(_FILE_CATALOGS.get(code, {}))
        for k, v in (overrides.get(code) or {}).items():
            if v is None:
                eff.pop(k, None)
            else:
                eff[k] = v
        cats[code] = eff
    _CATALOGS = cats
    return _CATALOGS


def reload():
    return _load()


def current_lang():
    """Langue effective : préférence utilisateur → setting global → défaut."""
    try:
        from .auth import current_user
        u = current_user()
        if u and u.get("lang") in LANG_CODES:
            return u["lang"]
    except Exception:
        pass
    try:
        from . import settings as st
        g = st.get("ui_lang_default")
        if g in LANG_CODES:
            return g
    except Exception:
        pass
    return DEFAULT_LANG


def t(key, lang=None, **kw):
    """Traduit `key` dans `lang` (ou la langue courante). Repli : lang → fr → key.
    `kw` interpole via str.format (`t("alert.destroyed", vmid=42)`)."""
    if not _CATALOGS:
        _load()
    lang = lang or current_lang()
    val = (_CATALOGS.get(lang, {}).get(key)
           or _CATALOGS.get(DEFAULT_LANG, {}).get(key)
           or key)
    if kw:
        try:
            return val.format(**kw)
        except Exception:
            return val
    return val


def js_catalog(lang=None, prefixes=("js.", "plugin.", "home.", "cables.", "settings.", "containers.", "section.", "projects.", "monitoring.", "wiz.")):
    """Sous-ensemble destiné au JS (clés `js.*`/`plugin.*`/`home.*`/`cables.*`/`settings.*`/`containers.*`/`section.*`/`projects.*`/`monitoring.*`/`wiz.*`),
    résolu dans la langue courante avec repli FR → injecté dans window.I18N par layout.html (et le wizard de setup)."""
    if not _CATALOGS:
        _load()
    lang = lang or current_lang()
    keys = _all_keys()
    return {k: t(k, lang) for k in keys
            if any(k.startswith(p) for p in prefixes)}


# ─── Support de l'éditeur de traductions ─────────────────────

def _all_keys():
    """Union de toutes les clés connues (tous fichiers + toutes surcharges)."""
    keys = set()
    for c in _FILE_CATALOGS.values():
        keys |= set(c)
    for c in _CATALOGS.values():
        keys |= set(c)
    return keys


def namespaces():
    """Préfixes d'espace de noms présents (ex. nav, js, plugin.receiver_2110)."""
    ns = set()
    for k in _all_keys():
        parts = k.split(".")
        ns.add(parts[0] if len(parts) < 3 else f"{parts[0]}.{parts[1]}")
    return sorted(ns)


def editor_rows(target_lang):
    """Lignes pour l'éditeur : pour chaque clé connue, la source FR (fichier) et la
    valeur courante dans `target_lang` avec son origine (override/fichier/manquant)."""
    if not _CATALOGS:
        _load()
    file_tgt = _FILE_CATALOGS.get(target_lang, {})
    over_tgt = {}
    try:
        from .database import db_i18n_overrides_for_lang
        over_tgt = db_i18n_overrides_for_lang(target_lang)
    except Exception:
        pass
    fr_file = _FILE_CATALOGS.get(DEFAULT_LANG, {})
    rows = []
    for key in sorted(_all_keys()):
        if key in over_tgt and over_tgt[key] is not None:
            value, origin = over_tgt[key], "override"
        elif key in file_tgt:
            value, origin = file_tgt[key], "file"
        else:
            value, origin = "", "missing"
        rows.append({"key": key,
                     "source": fr_file.get(key, ""),
                     "value": value,
                     "origin": origin})
    return rows


def set_override(lang, key, value):
    """Pose/efface une surcharge. value vide/None → suppression (retour au fichier)."""
    if lang not in LANG_CODES:
        raise ValueError(f"langue inconnue: {lang}")
    from .database import db_i18n_set_override, db_i18n_delete_override
    if value is None or value == "":
        db_i18n_delete_override(lang, key)
    else:
        db_i18n_set_override(lang, key, value)
    _load()


def add_language(code, label):
    code = (code or "").strip().lower()
    label = (label or "").strip() or code
    if not LANG_CODE_RE.match(code):
        raise ValueError(f"code de langue invalide: {code!r} (ex. 'nl', 'fr-studio')")
    if code in LANG_CODES:
        raise ValueError(f"langue déjà présente: {code}")
    from . import settings as st
    custom = list(st.get("ui_custom_languages") or [])
    custom.append({"code": code, "label": label})
    st.set("ui_custom_languages", custom)
    _load()
    return {"code": code, "label": label}


def remove_language(code):
    if code in {"fr", "en"}:
        raise ValueError("impossible de supprimer une langue fournie")
    from . import settings as st
    from .database import db_i18n_delete_lang
    custom = [l for l in (st.get("ui_custom_languages") or []) if l.get("code") != code]
    st.set("ui_custom_languages", custom)
    db_i18n_delete_lang(code)
    _load()


def export_catalog(lang):
    """Catalogue complet résolu d'une langue (toutes les clés), pour téléchargement."""
    if not _CATALOGS:
        _load()
    return {k: t(k, lang) for k in sorted(_all_keys())}


def import_overrides(lang, strings):
    """Applique en masse un dict {clé: valeur} comme surcharges de `lang`."""
    if lang not in LANG_CODES:
        raise ValueError(f"langue inconnue: {lang}")
    if not isinstance(strings, dict):
        raise ValueError("payload invalide (dict attendu)")
    from .database import db_i18n_set_override
    n = 0
    for k, v in strings.items():
        if isinstance(k, str) and isinstance(v, str):
            db_i18n_set_override(lang, k, v)
            n += 1
    _load()
    return n


def write_catalog_file(lang):
    """Écrit i18n/<lang>.json (clés du CŒUR uniquement : ni plugin.*, ni service.*).
    Action explicite/optionnelle — ⚠️ fichier versionné, une sync git peut l'écraser."""
    if not _CATALOGS:
        _load()
    core_keys = [k for k in sorted(_all_keys())
                 if not (k.startswith("plugin.") or k.startswith("service."))]
    data = {k: t(k, lang) for k in core_keys}
    path = os.path.join(_I18N_DIR, f"{lang}.json")
    os.makedirs(_I18N_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return path


# Chargement au premier import.
_load()
