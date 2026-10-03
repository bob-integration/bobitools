# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Auth & permissions de Bobi.Tools.

Modèle : rôle par utilisateur. Chaque rôle porte un ensemble de permissions, MODIFIABLE
(table `roles`) ; les plugins peuvent déclarer les leurs. Au-dessous, des règles de
PÉRIMÈTRE restreignent une permission d'outil à certaines ressources (cf. app/droits.py).
Sessions Flask (cookie signé) avec durée par défaut de 30 jours.

En plus des rôles, un **contrôle d'accès par outil** (cf. can_access_tool) restreint
qui peut ouvrir/utiliser un outil donné — la politique est stockée en settings
(`tool_access`), pas dans les fichiers du plugin. Dérivé du socle d'auth de Bobi.Studio.
"""
from functools import wraps
from flask import session, request, redirect, url_for, jsonify, abort, g
from werkzeug.security import generate_password_hash, check_password_hash

from .database import db_get_user_by_id, db_user_group_ids

# ─── Permissions & rôles ─────────────────────────────────────
# Deux sources de permissions :
#   · le CŒUR (ci-dessous), nommées `domaine.verbe` ;
#   · les PLUGINS, déclarées dans plugin.json (`permissions`) et nommées
#     `<type>.<id>` (ex. `switch_ports.port.vlan`) — cf. plugin_permissions().
# Les rôles sont MODIFIABLES (table `roles`, Réglages → Général → Rôles), comme les
# habilitations de Bobi.Studio. ROLES / ROLE_LABELS sont des dicts du module rechargés
# EN PLACE (clear + update) : les modules qui les ont importés voient les changements à
# chaud, sans redémarrage.

PERMISSIONS = [
    "tools.use",        # ouvrir et piloter les outils (selon l'accès par outil)
    "tools.manage",     # installer/désactiver/supprimer des outils, gérer les accès
    "settings.edit",    # réglages, utilisateurs, marque, thème, sauvegarde
    "settings.theme",   # changer UNIQUEMENT le thème (style) — sous-ensemble de settings.edit
]

# Rôles du premier démarrage (et cible du « Réinitialiser » d'un rôle intégré).
ROLES_DEFAUT = {
    "admin":    ("Administrateur", set(PERMISSIONS)),
    "operator": ("Opérateur", {"tools.use", "settings.theme"}),
    "viewer":   ("Lecteur (lecture seule)", set()),
}

# L'admin a TOUT, toujours, y compris les permissions déclarées plus tard par un plugin :
# on ne peut ni le modifier ni le supprimer. Sans ce verrou, un clic malheureux dans la
# matrice retirait à la seule personne capable de réparer le droit de le faire.
ROLE_INTOUCHABLE = "admin"

ROLES = {rid: set(perms) for rid, (_l, perms) in ROLES_DEFAUT.items()}
ROLE_LABELS = {rid: label for rid, (label, _p) in ROLES_DEFAUT.items()}


def plugin_permissions():
    """Catalogue des permissions déclarées par les plugins installés :
    [{id: "<type>.<pid>", tool, label, description, default_roles}]."""
    try:
        from . import plugins
    except Exception:
        return []
    out = []
    for m in plugins.all():
        t = m.get("type")
        for p in m.get("permissions") or []:
            pid = (p.get("id") or "").strip() if isinstance(p, dict) else ""
            if not t or not pid:
                continue
            out.append({"id": f"{t}.{pid}", "tool": t, "tool_label": m.get("label") or t,
                        "label": p.get("label") or pid, "description": p.get("description") or "",
                        "default_roles": [r for r in (p.get("default_roles") or []) if isinstance(r, str)]})
    return out


def all_permissions():
    """Toutes les permissions connues (cœur + plugins installés)."""
    return list(PERMISSIONS) + [p["id"] for p in plugin_permissions()]


def recharger_roles():
    """Relit la table `roles` dans ROLES / ROLE_LABELS (en place). Sème la table au premier
    démarrage, et distribue UNE FOIS les permissions d'un plugin nouvellement vu à ses
    `default_roles` (réglage `perms_seen`) : ensuite, c'est la matrice qui fait foi — une
    permission retirée à la main ne revient pas au redémarrage. Base indisponible → défauts."""
    from .database import db_roles_seed, db_roles_list, db_role_upsert
    try:
        db_roles_seed({rid: (label, perms) for rid, (label, perms) in ROLES_DEFAUT.items()})
        from . import settings as st
        seen = set(st.get("perms_seen") or [])
        nouvelles = [p for p in plugin_permissions() if p["id"] not in seen]
        if nouvelles:
            existants = {r["id"]: r for r in db_roles_list()}
            for p in nouvelles:
                for rid in p["default_roles"]:
                    r = existants.get(rid)
                    if r and p["id"] not in r["permissions"]:
                        r["permissions"].append(p["id"])
                        db_role_upsert(rid, permissions=r["permissions"])
            st.set("perms_seen", sorted(seen | {p["id"] for p in nouvelles}))
        rows = db_roles_list()
    except Exception:
        return
    known = set(all_permissions())
    roles = {r["id"]: {p for p in r["permissions"] if p in known} for r in rows}
    labels = {r["id"]: r["label"] or r["id"] for r in rows}
    roles[ROLE_INTOUCHABLE] = known
    labels.setdefault(ROLE_INTOUCHABLE, ROLES_DEFAUT[ROLE_INTOUCHABLE][0])
    ROLES.clear(); ROLES.update(roles)
    ROLE_LABELS.clear(); ROLE_LABELS.update(labels)


# ─── Helpers password ────────────────────────────────────────

# pbkdf2 plutôt que le scrypt par défaut de werkzeug 3.x : scrypt réclame ~32 Mo
# par vérification et lève « memory limit exceeded » selon le build OpenSSL / les
# limites mémoire d'un conteneur, ce qui fait échouer le login silencieusement.
_HASH_METHOD = "pbkdf2:sha256"

# Longueur minimale d'un mot de passe (création ET mise à jour).
MIN_PASSWORD_LEN = 10


def hash_password(plain):
    return generate_password_hash(plain, method=_HASH_METHOD)


def verify_password(plain, hashed):
    try:
        return check_password_hash(hashed, plain)
    except Exception:
        return False

# ─── User session ────────────────────────────────────────────

def current_user():
    """Renvoie le user courant (dict) ou None. Caché dans g pour économiser la DB."""
    if hasattr(g, "_user"):
        return g._user
    uid = session.get("user_id")
    g._user = db_get_user_by_id(uid) if uid else None
    return g._user


def current_permissions():
    u = current_user()
    if not u:
        return set()
    return ROLES.get(u.get("role"), set())


def has_perm(perm):
    u = current_user()
    if u and u.get("role") == ROLE_INTOUCHABLE:
        return True
    return perm in current_permissions()


def user_group_ids(user=None):
    """Groupes (ids) d'un utilisateur — celui de la session par défaut. Le résultat du
    user courant est mémorisé dans `g` : les politiques de partage l'interrogent
    plusieurs fois par requête (une fois par outil du lanceur, p. ex.)."""
    u = user if user is not None else current_user()
    if not u:
        return set()
    cur = current_user()
    if cur and u.get("id") == cur.get("id"):
        if not hasattr(g, "_group_ids"):
            g._group_ids = db_user_group_ids(u.get("id"))
        return g._group_ids
    return db_user_group_ids(u.get("id"))


def login_user(user):
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True   # cookie persistant (~30 jours selon config app)


def logout_user():
    session.clear()

# ─── Accès par outil ─────────────────────────────────────────

def _tool_access_policy():
    """Politique d'accès par outil : { "<type>": {"roles": [...], "users": [uid,...]} }.
    Stockée en settings (éditée via Réglages → Plugins). Import paresseux pour éviter
    une boucle d'import avec settings → database."""
    try:
        from . import settings as st
        pol = st.get("tool_access")
        return pol if isinstance(pol, dict) else {}
    except Exception:
        return {}


# Défaut quand un outil n'a PAS de politique explicite : accessible à ces rôles.
DEFAULT_TOOL_ROLES = ("admin", "operator")


def can_access_tool(type_, user=None):
    """True si `user` (ou le user courant) peut ouvrir/utiliser l'outil `type_`.
    admin → toujours. Sinon : politique explicite (rôle OU user listé OU groupe dont il
    est membre) ; à défaut, les rôles par défaut. Un user non connecté n'a accès à rien."""
    u = user if user is not None else current_user()
    if not u:
        return False
    if u.get("role") == "admin":
        return True
    pol = _tool_access_policy().get(type_)
    if pol is None:
        return u.get("role") in DEFAULT_TOOL_ROLES
    roles = set(pol.get("roles") or [])
    users = set(pol.get("users") or [])
    if (u.get("role") in roles) or (u.get("id") in users):
        return True
    groups = {int(gid) for gid in (pol.get("groups") or [])}
    return bool(groups) and bool(groups & user_group_ids(u))


def accessible_tools(types, user=None):
    """Sous-ensemble de `types` accessible au user (préserve l'ordre)."""
    u = user if user is not None else current_user()
    return [t for t in types if can_access_tool(t, u)]

# ─── Décorateurs ─────────────────────────────────────────────

def _wants_json():
    """True si la requête vient d'un fetch / accepte JSON."""
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


def require_login(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            if _wants_json():
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("routes.login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def require_perm(perm):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user():
                if _wants_json():
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("routes.login_page", next=request.path))
            if not has_perm(perm):
                if _wants_json():
                    return jsonify({"error": "forbidden", "missing_permission": perm}), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco


def require_tool(view):
    """Décorateur pour les routes outil : login + accès à l'outil `type_` (1er arg de la
    vue, façon Flask `<type_>`). Combine l'authent et le contrôle d'accès par outil."""
    @wraps(view)
    def wrapper(type_, *args, **kwargs):
        if not current_user():
            if _wants_json():
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("routes.login_page", next=request.path))
        if not can_access_tool(type_):
            if _wants_json():
                return jsonify({"error": "forbidden", "tool": type_}), 403
            abort(403)
        return view(type_, *args, **kwargs)
    return wrapper
