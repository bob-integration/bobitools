# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Droits fins des outils : permissions déclarées par les plugins + règles de PÉRIMÈTRE.

Un plugin déclare dans son plugin.json :

    "permissions": [{"id": "port.vlan", "label": "…", "default_roles": ["operator"]}, …],
    "acl": {
      "resources": [{"key": "switch", "label": "Switch", "choices": "switches"},
                    {"key": "port", "label": "Port", "attrs": [...]}],
      "resolve": "acl/resolve",            # facultatif : attributs d'une ressource
      "routes": [
        {"method": "POST", "path": "ember/set", "perm_by": {"field": "body.ref.field",
         "map": {"vlan": "port.vlan"}, "default": "switch.config"}, "resource": {…}},
        {"method": "POST", "path": "switches/{switch}/ports/vlan",
         "perm": "port.vlan", "resource": {"switch": "{switch}", "port": "body.port"}},
        {"method": "*", "path": "**", "perm": "manage"}          # filet : le reste
      ]
    }

Le CŒUR vérifie, AVANT de transmettre la requête à l'outil (donc aussi pour un conteneur
Docker, qui n'a pas à être cru sur parole) :
  1. la route d'écriture → une permission `<type>.<perm>` que le rôle doit porter ;
  2. si des règles de périmètre CITENT cette permission, l'une d'elles doit couvrir à la fois
     l'appelant (utilisateur, groupe, rôle) et la ressource visée.
Une route d'écriture qu'aucune entrée ne décrit — ou décrite avec `"perm": null` (aperçu,
diff, rafraîchissement : des POST qui n'écrivent rien) — garde le contrôle d'avant
(`tools.use`) : les plugins migrent à leur rythme.

Trois MODES de règle (cf. scope_allows) :
  · « deny » (interdire) : les appelants qu'elle nomme ne peuvent pas agir sur les ressources
    couvertes (périmètre vide = tout l'outil). Elle l'emporte sur les deux autres ; dans le
    doute, elle s'applique.
  · « reserve » : réserve les ressources couvertes à ses bénéficiaires ; ailleurs, rien ne
    change. Inconnu (ressource non désignée, attributs indisponibles) → traité comme réservé.
  · « allow » (autoriser seulement) : la permission devient une liste blanche sur TOUT
    l'outil. Inconnu → refus.
Il n'y a pas de « deny » libre. Les règles ne portent que sur l'ÉCRITURE : tout le monde
voit tout, ce qui aide au diagnostic.

Format d'un périmètre (`scope`) : {clé de ressource: sélecteur}. Clé absente ou « * » =
toutes. Un sélecteur est une alternative (OU) de :
  · "values":  [valeurs exactes]                    ex. ids de switch, noms de port ;
  · "<attr>":  [valeurs] ou "1-12,15" (plages)      comparé à l'attribut <attr> de la
                                                     ressource, fourni par `acl.resolve`.
Les clés se combinent en ET.
"""
import logging
import re
import threading
import time

log = logging.getLogger(__name__)

_RESOLVE_TTL = 30.0
_resolve_cache = {}
_resolve_lock = threading.Lock()


# ─── Déclaration du plugin ──────────────────────────────────

def tool_acl(type_):
    from . import plugins
    m = plugins.get(type_) or {}
    acl = m.get("acl")
    return acl if isinstance(acl, dict) else {}


def tool_permissions(type_):
    from . import plugins
    m = plugins.get(type_) or {}
    return [p for p in (m.get("permissions") or []) if isinstance(p, dict) and p.get("id")]


def _match_path(pattern, subpath):
    """Variables `{x}` d'un motif si `subpath` y correspond, sinon None. `**` = le reste."""
    pp = [x for x in (pattern or "").strip("/").split("/") if x]
    sp = [x for x in (subpath or "").strip("/").split("/") if x]
    out = {}
    for i, seg in enumerate(pp):
        if seg == "**":
            return out
        if i >= len(sp):
            return None
        if seg.startswith("{") and seg.endswith("}"):
            out[seg[1:-1]] = sp[i]
        elif seg != sp[i]:
            return None
    return out if len(pp) == len(sp) else None


def _dig(payload, dotted):
    cur = payload
    for k in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def match_route(type_, method, subpath, payload=None):
    """(perm courte, ressource {clé: valeur}) de la PREMIÈRE route qui correspond, ou None."""
    for r in tool_acl(type_).get("routes") or []:
        m = (r.get("method") or "*").upper()
        if m != "*" and m != method.upper():
            continue
        vars_ = _match_path(r.get("path"), subpath)
        if vars_ is None:
            continue
        res = {}
        for key, src in (r.get("resource") or {}).items():
            src = str(src)
            if src.startswith("{") and src.endswith("}"):
                val = vars_.get(src[1:-1])
            elif src.startswith("body."):
                val = _dig(payload or {}, src[5:])
            else:
                val = src
            if val is not None and val != "":
                res[key] = str(val)
        perm = r.get("perm")
        by = r.get("perm_by")
        if isinstance(by, dict):
            # La permission dépend d'une valeur du corps (ex. ember/set : le champ modifié).
            # Valeur non prévue → `default`, sinon la permission la plus large déclarée.
            v = _dig(payload or {}, str(by.get("field") or "").removeprefix("body."))
            perm = (by.get("map") or {}).get(str(v), by.get("default") or perm)
        if r.get("expand") and res:
            # La requête ne désigne la ressource qu'indirectement (ex. SW-P-08 : un numéro de
            # destination) : l'outil la traduit via `resolve` → clé `_resource`.
            res = {**res, **{k: str(v) for k, v in (_resolve(type_, res).get("_resource") or {}).items()}}
        return perm, res
    return None


# ─── Attributs de ressource (fournis par l'outil) ───────────

def _resolve(type_, resource):
    """Attributs {clé: {attr: valeur}} de la ressource, demandés à l'outil (cache 30 s).
    L'outil indisponible → {} : un sélecteur par attribut ne correspond alors à rien, ce qui
    REFUSE (on ne devine pas qu'un port est « patch » parce que l'outil ne répond pas)."""
    path = tool_acl(type_).get("resolve")
    if not path or not resource:
        return {}
    key = (type_, tuple(sorted(resource.items())))
    now = time.time()
    with _resolve_lock:
        hit = _resolve_cache.get(key)
        if hit and now - hit[0] < _RESOLVE_TTL:
            return hit[1]
    from . import tools
    try:
        status, data = tools.call(type_, path, "GET", dict(resource), actor="droits", timeout=10)
    except Exception as e:
        log.warning("droits: résolution %s %s : %s", type_, resource, e)
        status, data = 500, {}
    attrs = data if status == 200 and isinstance(data, dict) else {}
    with _resolve_lock:
        if len(_resolve_cache) > 4096:
            _resolve_cache.clear()
        _resolve_cache[key] = (now, attrs)
    return attrs


def forget_resolved(type_=None):
    with _resolve_lock:
        if type_ is None:
            _resolve_cache.clear()
        else:
            for k in [k for k in _resolve_cache if k[0] == type_]:
                del _resolve_cache[k]


# ─── Évaluation ─────────────────────────────────────────────

def parse_ranges(text):
    """« 1-12, 15 » → ensemble d'entiers. Ignore ce qui n'est pas une plage lisible."""
    out = set()
    for part in re.split(r"[,\s;]+", str(text or "")):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            continue
        a = int(m.group(1)); b = int(m.group(2) or a)
        if a <= b and b - a <= 10000:
            out.update(range(a, b + 1))
    return out


def _selector_matches(sel, value, attrs, need_attrs):
    """Le sélecteur `sel` couvre-t-il la ressource (valeur brute + attributs) ?
    → True, False, ou None quand on NE PEUT PAS savoir : la requête ne désigne pas la
    ressource (action groupée…), ou l'outil n'a pas fourni ses attributs. Chaque mode de
    règle tranche l'inconnu dans le sens prudent (cf. scope_allows)."""
    if sel in (None, "*", "", {}):
        return True
    if not isinstance(sel, dict):
        return False
    if value is None:
        return None
    vals = sel.get("values")
    if isinstance(vals, list) and value in [str(v) for v in vals]:
        return True
    inconnu = False
    for attr, cond in sel.items():
        if attr == "values" or cond in (None, "", []):
            continue
        got = need_attrs() if attrs is None else attrs
        if not got:
            # Aucun attribut pour cette ressource (outil muet, port hors cache) : inconnu.
            # Un port CONNU qui n'a simplement pas cet attribut (hors plan) est un vrai « non ».
            inconnu = True
            continue
        a = got.get(attr)
        if a is None:
            continue
        if isinstance(cond, list):
            if str(a) in [str(c) for c in cond]:
                return True
        elif str(a).isdigit() and int(a) in parse_ranges(cond):
            return True
    return None if inconnu else False


def _scope_matches(scope, resource, attrs_of):
    """Toutes les clés du périmètre (ET) → True / False / None (inconnu)."""
    res = True
    for k, sel in (scope or {}).items():
        m = _selector_matches(sel, resource.get(k), None, attrs_of(k))
        if m is False:
            return False
        if m is None:
            res = None
    return res


RULE_MODES = ("reserve", "allow", "deny")


def rule_mode(r):
    m = (r or {}).get("mode")
    return m if m in RULE_MODES else "allow"


def _principal_matches(principals, who):
    p = principals or {}
    if who.get("kind") == "controller":
        # Une règle cite un contrôleur par son id déclaré (« ctl:<id> ») ou par une adresse
        # brute (règles d'avant le registre, ou équipement non déclaré).
        cites = [str(x) for x in (p.get("controllers") or [])]
        if who.get("id") in cites:
            return True
        return any(f"ctl:{cid}" in cites for cid in who.get("controller_ids") or [])
    u = who.get("user") or {}
    if u.get("id") in [int(x) for x in (p.get("users") or []) if str(x).lstrip("-").isdigit()]:
        return True
    if u.get("role") and u.get("role") in (p.get("roles") or []):
        return True
    gids = {int(x) for x in (p.get("groups") or []) if str(x).lstrip("-").isdigit()}
    return bool(gids & set(who.get("groups") or ()))


def rules_for(type_, full_perm):
    from .database import db_rules_list
    short = full_perm.split(".", 1)[1] if full_perm.startswith(type_ + ".") else full_perm
    return [r for r in db_rules_list(type_) if short in (r.get("perms") or [])]


def scope_verdict(type_, full_perm, resource, who):
    """(permis ?, mode de la règle qui bloque, règle) — cf. scope_allows pour la logique.
    Le mode et la règle servent à EXPLIQUER le refus à l'utilisateur."""
    rules = rules_for(type_, full_perm)
    if not rules:
        return True, None, None
    cache = {}

    def attrs_of(key):
        def _get():
            if "all" not in cache:
                cache["all"] = _resolve(type_, resource)
            return cache["all"].get(key) or {}
        return _get

    # « Interdire » d'abord : elle l'emporte sur tout. Elle ne vise que les appelants qu'elle
    # nomme ; dans le doute (ressource non désignée, attributs indisponibles), elle s'applique.
    for r in rules:
        if (rule_mode(r) == "deny" and _principal_matches(r.get("principals"), who)
                and _scope_matches(r.get("scope"), resource, attrs_of) is not False):
            return False, "deny", r
    allows = [r for r in rules if rule_mode(r) == "allow"]
    if allows and not any(_principal_matches(r.get("principals"), who)
                          and _scope_matches(r.get("scope"), resource, attrs_of) is True
                          for r in allows):
        return False, "allow", allows[0]
    for r in rules:
        if rule_mode(r) != "reserve":
            continue
        if _scope_matches(r.get("scope"), resource, attrs_of) is False:
            continue                     # ressource hors de cette réservation
        # Réservée (ou peut-être) : il faut être bénéficiaire d'UNE réservation qui la couvre.
        covering = [x for x in rules if rule_mode(x) == "reserve"
                    and _scope_matches(x.get("scope"), resource, attrs_of) is not False]
        if any(_principal_matches(x.get("principals"), who) for x in covering):
            return True, None, None
        return False, "reserve", r
    return True, None, None


def scope_allows(type_, full_perm, resource, who):
    """Trois modes de règles, qui se cumulent :
    · « Interdire » (deny) : bloque les appelants qu'elle nomme ; l'emporte sur tout.
    · « Autoriser seulement » (allow) : dès qu'une telle règle cite la permission, celle-ci
      devient une LISTE BLANCHE sur tout l'outil — il faut une règle qui couvre l'appelant
      ET la ressource. Inconnu → refus.
    · « Réserver » (reserve) : sur les ressources qu'elle couvre, seuls ses bénéficiaires
      agissent ; AILLEURS, rien ne change. Si l'on ne peut pas savoir si la ressource est
      réservée (action groupée, attributs indisponibles), on la traite comme réservée :
      sinon une action groupée suffirait à contourner la réservation."""
    return scope_verdict(type_, full_perm, resource, who)[0]


def _perm_label(type_, perm):
    return next((p.get("label") or perm for p in tool_permissions(type_) if p.get("id") == perm), perm)


_names_cache = {}
_NAMES_TTL = 60.0


def resource_names(type_, key):
    """{id: nom} d'une clé de ressource, lu chez l'outil via son `choices` (cache 60 s).
    Ne sert qu'à RÉDIGER un refus : jamais sur le chemin d'une écriture permise."""
    res = next((r for r in tool_acl(type_).get("resources") or [] if r.get("key") == key), None)
    if not res or not res.get("choices"):
        return {}
    now = time.time()
    hit = _names_cache.get((type_, key))
    if hit and now - hit[0] < _NAMES_TTL:
        return hit[1]
    from . import tools
    try:
        status, data = tools.call(type_, res["choices"], "GET", actor="droits", timeout=5)
    except Exception:
        status, data = 500, None
    lk, fid, fname = res.get("choices_list"), res.get("choices_id") or "id", res.get("choices_name")
    items = data if isinstance(data, list) else ((data or {}).get(lk) if lk and isinstance(data, dict)
                                                  else (data or {}).get("items") if isinstance(data, dict) else [])
    out = {}
    if status == 200 and isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get(fid) is not None:
                out[str(it[fid])] = str((it.get(fname) if fname else (it.get("name") or it.get("label"))) or it[fid])
    _names_cache[(type_, key)] = (now, out)
    return out


def _resource_text(resource, type_=None):
    """« switch Peli-03, port Ethernet1/5 » : le NOM quand l'outil sait le donner."""
    parts = []
    for k, v in (resource or {}).items():
        label = k
        if type_:
            res = next((r for r in tool_acl(type_).get("resources") or [] if r.get("key") == k), None)
            if res and res.get("label"):
                label = res["label"].lower().rstrip("s") if len(res["label"]) > 3 else res["label"].lower()
            try:
                v = resource_names(type_, k).get(str(v), v)
            except Exception:
                pass
        parts.append(f"{label} {v}")
    return ", ".join(parts)


def refusal_message(type_, perm, resource, mode, rule):
    """Phrase lisible par l'exploitant : QUEL geste, OÙ, et POURQUOI (type de règle + note)."""
    label = f"« {_perm_label(type_, perm)} »"
    where = _resource_text(resource, type_)
    where = f" ({where})" if where else ""
    if mode == "missing":
        msg = f"Action refusée : votre rôle ne permet pas {label}."
    elif mode == "deny":
        msg = f"Action refusée : {label}{where} vous est interdit par une règle de périmètre."
    elif mode == "allow":
        msg = (f"Action refusée : {label} est limité à certains comptes (règle « Autoriser "
               f"seulement »), et cette ressource{where} n'en fait pas partie pour vous.")
    else:
        msg = f"Action refusée : {label}{where} est réservé à d'autres (règle « Réserver »)."
    note = ((rule or {}).get("note") or "").strip()
    if note:
        msg += f" Note : {note.rstrip('.')}."
    return msg + " Voir un administrateur (Réglages → Outils → Périmètres)."


def who_user(user):
    from .auth import user_group_ids
    return {"kind": "user", "user": user or {}, "groups": user_group_ids(user) if user else set()}


def controllers_for_ip(ip):
    """Contrôleurs DÉCLARÉS (Réglages → Protocoles → Contrôleurs) dont une adresse ou une
    plage contient `ip`. → [{id, name}]"""
    import ipaddress
    from .database import db_controllers_list
    try:
        a = ipaddress.ip_address(str(ip))
    except ValueError:
        return []
    out = []
    for c in db_controllers_list():
        for ad in c.get("addresses") or []:
            try:
                if a in ipaddress.ip_network(str(ad), strict=False):
                    out.append({"id": c["id"], "name": c["name"]})
                    break
            except ValueError:
                continue
    return out


def who_controller(addr, service=""):
    """Un contrôleur broadcast (Ember+, SW-P-08) vu comme un appelant : identifié par son
    adresse IP, et par les contrôleurs DÉCLARÉS qui la portent. Il n'a pas de rôle — il porte
    donc toutes les permissions — mais il est soumis aux règles de PÉRIMÈTRE comme un
    utilisateur : dès qu'une règle cite une permission, il lui faut une règle qui le nomme
    (par son nom déclaré, ou par une adresse saisie telle quelle) et couvre la ressource."""
    ip = addr[0] if isinstance(addr, (tuple, list)) else str(addr or "")
    try:
        decl = controllers_for_ip(ip)
    except Exception as e:
        log.warning("droits: contrôleurs déclarés illisibles : %s", e)
        decl = []
    return {"kind": "controller", "id": ip, "service": service,
            "controller_ids": [c["id"] for c in decl],
            "name": ", ".join(c["name"] for c in decl) or ip}


def check_write(type_, method, subpath, payload, who, has_perm):
    """Contrôle d'une écriture. → None si permise, sinon (status, corps d'erreur JSON).
    `has_perm(full_perm)` dit si l'appelant porte la permission (rôle)."""
    u = who.get("user") or {}
    if who.get("kind") == "user" and u.get("role") == "admin":
        return None
    hit = match_route(type_, method, subpath, payload)
    if not hit or not hit[0]:
        return None                    # route non décrite : contrôle d'avant (tools.use)
    perm, resource = hit
    full = f"{type_}.{perm}"
    # Le corps porte un message LISIBLE (`error`) et de quoi le traiter (`rights`, `code`) :
    # le SDK des outils l'affiche tel quel, quel que soit le plugin.
    if not has_perm(full):
        return 403, {"error": refusal_message(type_, perm, resource, "missing", None),
                     "code": "forbidden", "rights": True, "missing_permission": full}
    ok, mode, rule = scope_verdict(type_, full, resource, who)
    if not ok:
        return 403, {"error": refusal_message(type_, perm, resource, mode, rule),
                     "code": "hors_perimetre", "rights": True, "rule_mode": mode,
                     "permission": full, "resource": resource}
    return None


def rights_summary(type_, user, has_perm):
    """Pour l'UI d'un outil : permissions accordées (ids courts), et celles que des règles
    de périmètre restreignent (l'UI doit alors interroger /rights/check par ressource)."""
    admin = (user or {}).get("role") == "admin"
    perms = [p["id"] for p in tool_permissions(type_)]
    granted = [p for p in perms if admin or has_perm(f"{type_}.{p}")]
    restricted = [] if admin else sorted({p for r in rules_for_tool(type_) for p in r.get("perms") or []})
    return {"admin": admin, "granted": granted, "restricted": restricted,
            "declared": bool(tool_acl(type_).get("routes"))}


def rules_for_tool(type_):
    from .database import db_rules_list
    return db_rules_list(type_)


def check_resource(type_, perm, resource, who, has_perm):
    """Une permission courte sur une ressource donnée (pour griser l'UI)."""
    u = who.get("user") or {}
    if u.get("role") == "admin":
        return True
    full = f"{type_}.{perm}"
    return bool(has_perm(full)) and scope_allows(type_, full, resource or {}, who)
