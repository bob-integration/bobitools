# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Configuration réseau de la MACHINE (pas des outils) — lecture et modification.

Sert l'onglet Réglages → Réseau : adresses en notation CIDR, passerelle, DNS, et surtout
QUI possède réellement cette configuration. Ce dernier point n'est pas cosmétique : sur une
instance en conteneur LXC, c'est l'hôte Proxmox qui écrit `/etc/network/interfaces` et
`/etc/resolv.conf` au démarrage du conteneur. Une modification faite de l'intérieur y survit
jusqu'au prochain redémarrage, puis disparaît — sans que rien ne l'annonce. L'écran le dit
donc franchement plutôt que de laisser croire à un réglage durable.

La modification est protégée par une CONFIRMATION avec retour arrière automatique : voir la
section « ÉCRITURE » en bas de ce module, qui explique pourquoi c'est indispensable ici.
"""
import ipaddress
import logging
import os
import re
import subprocess
import threading
import time

log = logging.getLogger(__name__)

_TIMEOUT = 5


def _run(args):
    """Exécute une commande de lecture et rend sa sortie, ou None. Ne lève jamais : un outil
    absent (machine minimale, image sans iproute2) ne doit pas casser la page."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("system_net: %s indisponible (%s)", args[0], e)
        return None


def _interfaces():
    """Interfaces IPv4 avec leur préfixe, hors boucle locale."""
    out = _run(["ip", "-o", "-4", "addr", "show"])
    ifs = {}
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] == "lo":
            continue
        name, cidr = parts[1], parts[3]
        entry = ifs.setdefault(name, {"name": name, "addresses": [], "up": False})
        entry["addresses"].append(cidr)
    # État administratif : une interface configurée mais non montée est un cas courant et
    # déroutant (elle figure dans le fichier de conf mais pas dans `ip addr`).
    link = _run(["ip", "-o", "link", "show"]) or ""
    for line in link.splitlines():
        m = re.match(r"\d+:\s+([^:@]+)[:@]", line)
        if m and m.group(1) in ifs:
            ifs[m.group(1)]["up"] = "state UP" in line or "UP" in line.split("<")[1].split(">")[0]
    return list(ifs.values())


def _gateway():
    out = _run(["ip", "-4", "route", "show", "default"]) or ""
    m = re.search(r"default via (\S+)(?: dev (\S+))?", out)
    return {"address": m.group(1), "interface": m.group(2)} if m else None


def _dns():
    """Serveurs DNS et domaine de recherche, plus l'indice de qui écrit resolv.conf."""
    servers, search, managed_by = [], [], None
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {"servers": [], "search": [], "managed_by": None}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            servers.append(line.split()[1] if len(line.split()) > 1 else "")
        elif line.startswith(("search", "domain")):
            search += line.split()[1:]
    if "BEGIN PVE" in content:
        managed_by = "pve"          # Proxmox réécrit ce fichier au démarrage du conteneur
    elif "systemd-resolved" in content or "127.0.0.53" in servers:
        managed_by = "systemd-resolved"
    elif "NetworkManager" in content:
        managed_by = "NetworkManager"
    return {"servers": [s for s in servers if s], "search": search, "managed_by": managed_by}


def _virt():
    """`lxc`, `kvm`, `none`… — détermine si la configuration appartient à un hôte."""
    return (_run(["systemd-detect-virt"]) or "").strip() or None


def _manager():
    """Qui gère les interfaces : ifupdown, netplan, systemd-networkd, NetworkManager."""
    if os.path.exists("/etc/netplan") and any(
            f.endswith((".yaml", ".yml")) for f in os.listdir("/etc/netplan")):
        return "netplan"
    if os.path.exists("/etc/network/interfaces"):
        return "ifupdown"
    if os.path.isdir("/etc/systemd/network"):
        return "systemd-networkd"
    return None


def _declared():
    """Ce que le FICHIER de configuration déclare, qui peut différer de l'état courant —
    une interface déclarée mais non montée, typiquement."""
    path = "/etc/network/interfaces"
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            cur = None
            for raw in f:
                line = raw.strip()
                m = re.match(r"iface\s+(\S+)\s+inet\s+(\S+)", line)
                if m:
                    cur = {"name": m.group(1), "method": m.group(2),
                           "address": None, "gateway": None}
                    if cur["name"] != "lo":
                        out.append(cur)
                    continue
                if cur is None:
                    continue
                if line.startswith("address "):
                    cur["address"] = line.split(None, 1)[1].strip()
                elif line.startswith("gateway "):
                    cur["gateway"] = line.split(None, 1)[1].strip()
    except OSError:
        return []
    return out


def state():
    """Instantané complet, prêt pour l'UI."""
    virt = _virt()
    manager = _manager()
    dns = _dns()
    # Le point qui décide de ce que l'écran doit AFFICHER comme avertissement : dans un
    # conteneur, l'hôte réécrit les fichiers au démarrage, donc une modification faite ici
    # n'est pas durable. Le dire est plus utile que de l'apprendre après un redémarrage.
    host_owned = virt in ("lxc", "lxc-libvirt", "openvz") or dns.get("managed_by") == "pve"
    return {
        "interfaces": _interfaces(),
        "declared": _declared(),
        "gateway": _gateway(),
        "dns": dns,
        "virt": virt,
        "manager": manager,
        "host_owned": host_owned,
        "hostname": (_run(["hostname"]) or "").strip() or None,
    }


def validate_cidr(value):
    """(ok, message) — valide une adresse en notation CIDR. Utilisé par l'UI avant tout envoi ;
    la validation vit ici pour rester la même partout."""
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError as e:
        return False, str(e)
    if iface.version != 4:
        return False, "IPv4 attendue"
    if iface.network.prefixlen > 30:
        return False, "préfixe trop étroit (/30 au maximum utile)"
    return True, None


# ═════════════════════════════════════════════════════════════════════
# ÉCRITURE — avec confirmation et retour arrière automatique
#
# Changer l'adresse d'une machine depuis sa propre interface web, c'est scier la branche :
# si l'adresse est fausse, plus personne ne l'atteint — y compris l'application qui vient de
# la changer, et donc y compris pour réparer. D'où le mécanisme du « commit confirmed » des
# routeurs : on applique, puis l'opérateur DOIT se reconnecter sur la NOUVELLE adresse et
# confirmer dans le délai imparti. Sans confirmation, la machine revient seule à l'ancienne
# configuration. L'échec redevient une attente d'une minute au lieu d'un déplacement.
# ═════════════════════════════════════════════════════════════════════

CONFIRM_DELAY_S = 90          # délai de confirmation avant retour arrière automatique
_IFACE_FILE = "/etc/network/interfaces"

_pending = None               # {token, iface, before, after, deadline, timer}
_pending_lock = threading.Lock()


def pending():
    """Changement en attente de confirmation, pour l'UI (sans le timer)."""
    with _pending_lock:
        if not _pending:
            return None
        return {k: v for k, v in _pending.items() if k != "timer"}


def _snapshot(iface):
    """État actuel de l'interface, tel qu'il faudra le rétablir."""
    addrs = [i["addresses"] for i in _interfaces() if i["name"] == iface]
    gw = _gateway() or {}
    return {"iface": iface,
            "addresses": addrs[0] if addrs else [],
            "gateway": gw.get("address") if gw.get("interface") == iface else None}


def _apply_live(iface, cidr, gateway):
    """Applique à chaud. Renvoie (ok, message). On ne passe PAS par ifdown/ifup : couper
    l'interface avant de la reconfigurer laisse une fenêtre où la machine est injoignable même
    si la suite réussit. `ip addr` remplace en place."""
    if _run(["ip", "addr", "flush", "dev", iface]) is None:
        return False, "flush de %s impossible" % iface
    if _run(["ip", "addr", "add", cidr, "dev", iface]) is None:
        return False, "adresse %s refusée sur %s" % (cidr, iface)
    _run(["ip", "link", "set", iface, "up"])
    if gateway:
        # `replace` et non `add` : la route par défaut existe déjà dans le cas nominal.
        if _run(["ip", "route", "replace", "default", "via", gateway, "dev", iface]) is None:
            return False, "passerelle %s inatteignable depuis %s" % (gateway, iface)
    return True, None


def _write_iface_file(iface, cidr, gateway):
    """Réécrit la strophe de `iface` dans /etc/network/interfaces, en préservant le reste.
    Une sauvegarde horodatée est laissée à côté — c'est le filet du filet."""
    try:
        with open(_IFACE_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return False, str(e)
    try:
        with open(_IFACE_FILE + ".bak", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass
    out, i, done = [], 0, False
    while i < len(lines):
        m = re.match(r"iface\s+(\S+)\s+inet\s+", lines[i].strip())
        if m and m.group(1) == iface:
            out.append("iface %s inet static" % iface)
            out.append("\taddress %s" % cidr)
            if gateway:
                out.append("\tgateway %s" % gateway)
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                if not lines[i].strip():          # ligne vide = fin de strophe
                    break
                i += 1
            done = True
            continue
        out.append(lines[i])
        i += 1
    if not done:                                   # interface absente du fichier : on l'ajoute
        out += ["", "auto %s" % iface, "iface %s inet static" % iface, "\taddress %s" % cidr]
        if gateway:
            out.append("\tgateway %s" % gateway)
    try:
        with open(_IFACE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError as e:
        return False, str(e)
    return True, None


def _rollback(reason, before=None):
    """Rétablit l'état d'avant. Appelé par le minuteur, à la demande, ou juste après un
    échec d'application.

    `before` explicite est INDISPENSABLE sur le chemin d'échec : à ce moment-là, rien n'a
    encore été posé dans `_pending` (il n'est renseigné qu'une fois l'application réussie),
    si bien que la version qui dépilait la globale sortait aussitôt sans rien rétablir. Or
    `_apply_live` commence par vider l'adresse de l'interface : un échec survenu après ce
    vidage laissait la machine sans adresse, et donc injoignable, alors même que l'écran
    promet un retour arrière automatique."""
    global _pending
    with _pending_lock:
        p = _pending
        _pending = None
    if not p and before is None:
        return
    before = before if before is not None else p["before"]
    log.warning("system_net: RETOUR ARRIÈRE réseau (%s) — %s revient à %s",
                reason, before["iface"], before["addresses"] or "aucune adresse")
    old = (before["addresses"] or [None])[0]
    if not old:
        # L'interface n'avait AUCUNE adresse avant : on la remet dans cet état plutôt que
        # d'écrire « address None » dans le fichier, ce qui le rendrait invalide au démarrage.
        _run(["ip", "addr", "flush", "dev", before["iface"]])
        return
    _apply_live(before["iface"], old, before.get("gateway"))
    _write_iface_file(before["iface"], old, before.get("gateway"))


def change(iface, cidr, gateway=None, persist=True, delay=None):
    """Applique un changement d'adresse SOUS CONDITION DE CONFIRMATION.
    Renvoie (ok, {token, deadline} | message d'erreur)."""
    global _pending
    ok, msg = validate_cidr(cidr)
    if not ok:
        return False, msg
    if gateway:
        try:
            ipaddress.ip_address(gateway)
        except ValueError:
            return False, "passerelle invalide"
        if ipaddress.ip_address(gateway) not in ipaddress.ip_interface(cidr).network:
            return False, "la passerelle %s n'est pas dans le réseau %s" % (
                gateway, ipaddress.ip_interface(cidr).network)
    if not any(i["name"] == iface for i in _interfaces()):
        return False, "interface %s inconnue" % iface
    with _pending_lock:
        if _pending:
            return False, "un changement est déjà en attente de confirmation"
    before = _snapshot(iface)
    ok, msg = _apply_live(iface, cidr, gateway)
    if not ok:
        _rollback("échec de l'application", before=before)
        return False, msg
    if persist:
        _write_iface_file(iface, cidr, gateway)
    delay = int(delay or CONFIRM_DELAY_S)
    token = os.urandom(8).hex()
    timer = threading.Timer(delay, _rollback, args=("non confirmé",))
    timer.daemon = True
    with _pending_lock:
        _pending = {"token": token, "iface": iface, "before": before,
                    "after": {"address": cidr, "gateway": gateway},
                    "deadline": time.time() + delay, "timer": timer}
    timer.start()
    log.warning("system_net: %s → %s appliqué, confirmation attendue sous %d s",
                iface, cidr, delay)
    return True, {"token": token, "deadline": time.time() + delay, "delay": delay}


def confirm(token):
    """Confirme le changement : le retour arrière est annulé."""
    global _pending
    with _pending_lock:
        if not _pending:
            return False, "aucun changement en attente"
        if token != _pending["token"]:
            return False, "jeton invalide"
        _pending["timer"].cancel()
        iface, after = _pending["iface"], _pending["after"]
        _pending = None
    log.warning("system_net: changement confirmé — %s reste en %s", iface, after["address"])
    return True, None


def cancel():
    """Annule tout de suite, sans attendre le délai."""
    with _pending_lock:
        p = _pending
    if not p:
        return False, "aucun changement en attente"
    p["timer"].cancel()
    _rollback("annulé par l'opérateur")
    return True, None
