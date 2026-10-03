# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des instances Bobi.Tools du réseau (la « flotte ») + découverte par scan.

Carnet d'adresses de la mise à jour pull/push (cf. `updater.py`). Chaque pair =
{name, url, token}. `discover(cidr)` sonde le port HTTP d'une plage IP à la recherche de
l'endpoint public `/api/update/ping` d'autres instances. Repris de Bobi.Studio.
"""
import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .config import HTTP_PORT
from .database import get_db
from . import updater

log = logging.getLogger(__name__)

_COLS = "id, name, url, token, version, last_seen, deployed_at"
# Au-delà, un scan sonderait des milliers d'adresses avec 64 fils pour un résultat illisible :
# une flotte Bobi.Tools vit sur un réseau de régie, pas sur un /16.
_MAX_SCAN = 1024


def _row(r):
    return dict(r) if r else None


def list_peers():
    with get_db() as db:
        return [dict(r) for r in db.execute(f"SELECT {_COLS} FROM peers ORDER BY name")]


def get_peer(pid):
    with get_db() as db:
        return _row(db.execute(f"SELECT {_COLS} FROM peers WHERE id=?", (pid,)).fetchone())


def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def add_peer(name, url, token=""):
    url = normalize_url(url)
    if not url:
        raise ValueError("url requise")
    with get_db() as db:
        db.execute("INSERT INTO peers (name, url, token, created_at) VALUES (?,?,?,?) "
                   "ON CONFLICT(url) DO UPDATE SET name=excluded.name, token=excluded.token",
                   ((name or "").strip() or url, url, (token or "").strip(),
                    datetime.now().isoformat(timespec="seconds")))
    return refresh_url(url)


def update_peer(pid, name=None, token=None):
    sets, args = [], []
    if name is not None:
        sets.append("name=?"); args.append(name.strip())
    if token is not None:
        sets.append("token=?"); args.append(token.strip())
    if sets:
        args.append(pid)
        with get_db() as db:
            db.execute(f"UPDATE peers SET {', '.join(sets)} WHERE id=?", args)
    return get_peer(pid)


def delete_peer(pid):
    with get_db() as db:
        db.execute("DELETE FROM peers WHERE id=?", (pid,))


def refresh_url(url):
    """Ping un pair et mémorise sa version / last_seen / date de déploiement. Un pair muet
    garde ses dernières valeurs connues : `last_seen` dit depuis quand il ne répond plus."""
    url = normalize_url(url)
    try:
        info = updater.ping(url)
        with get_db() as db:
            db.execute("UPDATE peers SET version=?, last_seen=?, deployed_at=? WHERE url=?",
                       (info.get("label") or info.get("build_id") or "?",
                        datetime.now().isoformat(timespec="seconds"),
                        info.get("deployed_at"), url))
    except Exception as e:
        log.debug("ping %s: %s", url, e)
    with get_db() as db:
        return _row(db.execute(f"SELECT {_COLS} FROM peers WHERE url=?", (url,)).fetchone())


def refresh_all():
    peers = list_peers()
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda p: refresh_url(p["url"]), peers))
    return list_peers()


def default_cidr():
    """Réseau de l'interface qui porte la route par défaut — là où vivent, le plus souvent,
    les autres instances. Lu sur la machine, jamais codé en dur : une adresse de site
    serait juste ici et fausse partout ailleurs. "" si indéterminable."""
    try:
        from . import system_net
        stt = system_net.state()
        gw_if = (stt.get("gateway") or {}).get("interface")
        for itf in stt.get("interfaces") or []:
            if itf.get("name") == gw_if and itf.get("addresses"):
                return str(ipaddress.ip_interface(itf["addresses"][0]).network)
    except Exception as e:
        log.debug("default_cidr: %s", e)
    return ""


def discover(cidr):
    """Scan threadé d'une plage IP → instances trouvées [{url, ip, label, name}].
    Ne modifie pas le registre : l'utilisateur ajoute ensuite ce qu'il veut."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise ValueError(f"CIDR invalide : {e}")
    if net.num_addresses > _MAX_SCAN:
        raise ValueError(f"plage trop large ({net.num_addresses} adresses, {_MAX_SCAN} au plus)")
    hosts = list(net.hosts()) if net.num_addresses > 2 else list(net)

    def _probe(ip):
        url = f"http://{ip}:{HTTP_PORT}"
        try:
            info = updater.ping(url, timeout=2)
            return {"url": url, "ip": str(ip), "label": info.get("label"),
                    "build_id": info.get("build_id"), "name": info.get("name")}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=64) as ex:
        return [r for r in ex.map(_probe, hosts) if r]
