# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Veille des mises à jour : dire aux administrateurs qu'une nouvelle version est publiée.
Reprise de Bobi.Studio (`app/update_watch.py`, 2026-11-08), sans son système d'alertes que
Bobi.Tools n'a pas : ici le signal est un BANDEAU pour qui peut agir (`settings.edit`) et une
ligne au journal d'audit.

★ POURQUOI. La page Mises à jour sait comparer l'instance à ce qui est publié — mais seulement
quand quelqu'un l'ouvre. Une instance installée dans une régie reste des mois sur sa version, ses
administrateurs n'ayant aucune raison d'ouvrir cette page.

Ce que fait ce module, et ce qu'il s'interdit :
  • il VÉRIFIE toutes les `update_check_interval_h` heures (24 par défaut) ;
  • il PRÉVIENT : bandeau (chaque administrateur peut l'écarter pour CETTE version ; une version
    plus récente revient) et journal d'audit, une fois par version ;
  • il N'INSTALLE JAMAIS. Sur une installation d'antenne, le moment d'une mise à jour se choisit.

Deux interrupteurs, tous deux respectés : `update_check_enabled` (cette veille) et
`catalogue_actif` (aucune requête ne doit partir vers Internet — site isolé). Le second l'emporte.
Le quota GitHub n'est pas un souci : une lecture de l'index statique par passage (CDN, hors quota).
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

STATE_KEY = "update_watch_state"
DISMISSED_KEY = "update_watch_dismissed"
INTERVALS_H = (6, 12, 24, 72, 168)
SCOPES = ("core", "all")
_TICK_S = 300                           # fréquence à laquelle la boucle regarde l'horloge
_FIRST_DELAY_S = 120                    # laisser le réseau s'établir après un démarrage
_lock = threading.Lock()


def _flag(v):
    return str(v).strip().lower() not in ("0", "false", "off", "", "none")


def _settings():
    from . import settings as st
    on = _flag(st.get("update_check_enabled") if st.get("update_check_enabled") is not None else "1")
    try:
        h = int(st.get("update_check_interval_h") or 24)
    except (TypeError, ValueError):
        h = 24
    scope = st.get("update_check_scope") or "core"
    return {"enabled": on, "interval_h": h if h in INTERVALS_H else 24,
            "scope": scope if scope in SCOPES else "core"}


def state():
    from .database import db_get_setting
    return db_get_setting(STATE_KEY, {}) or {}


def _collect(scope):
    """→ (éléments disponibles, erreur ou None). Élément : {key, kind, name, label, version, installed, url}."""
    from . import catalogue
    items, error = [], None
    dispo, info = catalogue.maj_core_disponible(force=True)
    if info is None:
        error = "core"
    elif dispo:
        items.append({"key": "core", "kind": "core", "name": "bobitools", "label": "Bobi.Tools",
                      "version": info["version"], "installed": info.get("version_installee"),
                      "url": info.get("url") or ""})
    if scope == "all":
        cat = catalogue.lister(force=True)
        if cat.get("erreur"):
            error = error or "catalogue"
        for e in cat.get("entrees") or []:
            if e.get("etat") != "maj":
                continue
            items.append({"key": "%s:%s" % (e["genre"], e["type"]), "kind": e["genre"],
                          "name": e["type"], "label": e.get("label") or e["type"],
                          "version": e.get("version_dispo"), "installed": e.get("version_installee"),
                          "url": e.get("url") or ""})
    return items, error


def _journal(item):
    from .database import audit_log
    audit_log("system", "update_available", "%s %s disponible (installé : %s)"
              % (item["label"], item["version"], item.get("installed") or "?"),
              username="veille")


def check(now=None):
    """Un passage. Journalise UNE fois par nouvelle version publiée. → l'état écrit.
    Appelable à tout moment (bouton « Vérifier maintenant »), interrupteur ou pas."""
    from . import catalogue
    from .database import db_set_setting
    cfg = _settings()
    now = time.time() if now is None else now
    with _lock:
        prev = state()
        notified = dict(prev.get("notified") or {})
        if not catalogue._reglages()[2]:
            # Internet interdit sur ce site : le dire plutôt que montrer un ancien « disponible ».
            new = {"checked_at": now, "error": "offline", "available": [], "notified": notified}
            db_set_setting(STATE_KEY, new)
            return new
        try:
            items, error = _collect(cfg["scope"])
        except Exception as e:
            log.warning("veille des mises à jour : échec (%s)", e)
            items, error = list(prev.get("available") or []), "exception"
        for it in items:
            if notified.get(it["key"]) == it["version"]:
                continue
            try:
                _journal(it)
            except Exception as e:
                log.error("veille : journal non écrit pour %s (%s)", it["key"], e)
            notified[it["key"]] = it["version"]
        new = {"checked_at": now, "error": error, "available": items, "notified": notified}
        db_set_setting(STATE_KEY, new)
        return new


def notice_for(uid):
    """Ce que le bandeau montre à CET administrateur : le disponible, moins ce qu'il a écarté à
    cette version. Une version plus récente revient, c'est voulu."""
    from .database import db_get_setting
    cfg = _settings()
    st = state()
    gone = ((db_get_setting(DISMISSED_KEY, {}) or {}).get(str(uid)) or {})
    items = [i for i in (st.get("available") or []) if gone.get(i["key"]) != i["version"]]
    return {"enabled": cfg["enabled"], "items": items if cfg["enabled"] else [],
            "checked_at": st.get("checked_at")}


def dismiss(uid):
    """« Ignorer cette version » : pour cet administrateur, tout ce qui est montré, à sa version."""
    from .database import db_get_setting, db_set_setting
    with _lock:
        allx = db_get_setting(DISMISSED_KEY, {}) or {}
        mine = dict(allx.get(str(uid)) or {})
        for i in state().get("available") or []:
            mine[i["key"]] = i["version"]
        allx[str(uid)] = mine
        db_set_setting(DISMISSED_KEY, allx)
    return mine


def _due(now):
    cfg = _settings()
    if not cfg["enabled"]:
        return False
    last = float(state().get("checked_at") or 0)
    return now - last >= cfg["interval_h"] * 3600


def _loop():
    time.sleep(_FIRST_DELAY_S)
    while True:
        try:
            if _due(time.time()):
                check()
        except Exception as e:
            log.error("veille des mises à jour : erreur de boucle (%s)", e)
        time.sleep(_TICK_S)


def start():
    """Lancé par main.py au démarrage."""
    threading.Thread(target=_loop, daemon=True, name="update-watch").start()
