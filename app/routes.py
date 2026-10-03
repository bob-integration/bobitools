# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
API REST + pages de Bobi.Tools (Blueprint unique). Pas de métier broadcast : uniquement
le socle (auth, réglages, i18n, sauvegarde) et le système d'outils-plugins :
  - service des UI d'outils (page.html/js/css),
  - dispatch unifié /api/tools/<type>/<path> → backend in-process OU proxy conteneur Docker,
  - stockage générique par outil, contrôle d'accès par outil, journal d'audit,
  - gestion des plugins (import/export/versions/activation/désactivation).
"""
import hmac
import secrets
import io
import os
import sys
import time
import json
import zipfile
import tempfile
import logging
import threading
import subprocess

import requests
from flask import (Blueprint, render_template, request, jsonify, redirect,
                   url_for, session, send_file, abort, Response, current_app)

from . import settings as st
from . import plugins, core_plugins, deploy, backup, i18n as i18n_mod
from . import tools as tools_mod
from . import builder, updater, gitupdate, droits
from .auth import (require_login, require_perm, require_tool, current_user,
                   login_user, logout_user, hash_password, verify_password,
                   has_perm, can_access_tool, accessible_tools, user_group_ids,
                   ROLES, ROLE_LABELS, PERMISSIONS, DEFAULT_TOOL_ROLES,
                   MIN_PASSWORD_LEN, ROLE_INTOUCHABLE, ROLES_DEFAUT,
                   plugin_permissions, all_permissions, recharger_roles)
from .database import (db_get_user, db_create_user, db_list_users, db_update_user,
                       db_delete_user, db_count_users, db_get_user_by_id,
                       db_list_groups, db_get_group, db_create_group,
                       db_update_group, db_delete_group,
                       plugin_store_list, plugin_store_get, plugin_store_create,
                       plugin_store_update, plugin_store_delete,
                       audit_log, audit_list, audit_tools)

log = logging.getLogger(__name__)
bp = Blueprint("routes", __name__)

_START_TS = time.time()   # pour /api/service/info (uptime)

# ── Anti-brute-force login (fenêtre glissante en mémoire, par IP) ────────────
# Volontairement simple : pas de dépendance externe, borné par IP. Suffisant pour
# ralentir un bourrinage sur une petite instance ; un vrai WAF/reverse-proxy reste
# recommandé en frontal.
_LOGIN_MAX_FAILS = 8          # échecs tolérés dans la fenêtre
_LOGIN_WINDOW_S = 300         # fenêtre d'observation (5 min)
_LOGIN_LOCK_S = 300           # durée de blocage une fois le seuil atteint
_login_fails = {}             # ip -> [timestamps d'échec]
_LOGIN_MAX_KEYS = 4096        # au-delà, on purge les adresses hors fenêtre (cf. _login_record_fail)
_login_lock = threading.Lock()


def _login_client_ip():
    return request.remote_addr or "?"


def _login_is_blocked(ip):
    now = time.time()
    with _login_lock:
        fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_LOCK_S]
        _login_fails[ip] = fails
        return len(fails) >= _LOGIN_MAX_FAILS


def _login_record_fail(ip):
    now = time.time()
    with _login_lock:
        fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW_S]
        fails.append(now)
        _login_fails[ip] = fails
        # Purge des adresses qu'on ne reverra pas : le filtrage par fenêtre ne s'applique
        # qu'aux clés RELUES, si bien qu'une adresse vue une fois restait indéfiniment.
        # Sans cela, un balayage suffit à faire enfler le dictionnaire sans limite.
        if len(_login_fails) > _LOGIN_MAX_KEYS:
            for k in [k for k, v in _login_fails.items()
                      if not v or now - v[-1] > _LOGIN_WINDOW_S]:
                del _login_fails[k]


def _login_reset(ip):
    with _login_lock:
        _login_fails.pop(ip, None)


# ─── Helpers ────────────────────────────────────────────────

def _json():
    return request.get_json(force=True, silent=True) or {}


def _audit(tool, action, detail=""):
    u = current_user() or {}
    audit_log(tool, action, detail, user_id=u.get("id"), username=u.get("username"))


# Clés sensibles à masquer avant journalisation (audit en clair sinon).
_SECRET_KEYS = ("token", "password", "passwd", "secret", "api_key", "apikey", "pass",
                "csv", "credential", "auth", "key")
# Au-delà de cette longueur, une chaîne n'est plus journalisée mais résumée par sa taille :
# un contenu de fichier ou un blob n'apprend rien dans un journal et peut porter des
# secrets en nombre (cf. _redact).
_AUDIT_MAX_STR = 200


def _redact(value):
    """Masque récursivement les valeurs des clés sensibles (token, mot de passe…).

    Filtrer par NOM de clé ne suffit pas : un secret transporté dans un champ au nom
    anodin passe intact. Le cas vu en vrai — l'import CSV du coffre, dont la charge utile
    s'appelle `csv` et contient une colonne « mot_de_passe » — écrivait les mots de passe
    en clair dans la table d'audit. D'où la seconde règle : toute chaîne LONGUE est
    remplacée par sa taille. Un import, un fichier collé ou un blob n'ont de toute façon
    aucune valeur dans un journal, et c'est exactement là que se cachent les secrets en
    nombre."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(s in k.lower() for s in _SECRET_KEYS) and v:
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > _AUDIT_MAX_STR:
        return f"<{len(value)} caractères non journalisés>"
    return value


def _safe_next(dest):
    """Destination d'après-connexion, bornée à CETTE application.

    `next` vient de l'URL : un lien de connexion préparé (« …/login?next=https://ailleurs »)
    renverrait l'utilisateur vers un site tiers juste après une authentification réussie —
    l'instant où il est le plus enclin à faire confiance à ce qui s'affiche. On n'accepte
    donc qu'un chemin relatif à la racine, et jamais « //hôte » que le navigateur lit comme
    une URL absolue."""
    d = (dest or "").strip()
    if not d.startswith("/") or d.startswith("//") or "\\" in d:
        return url_for("routes.home")
    return d


def _setup_needed():
    return db_count_users() == 0


# ─── Premier démarrage : création de l'admin ────────────────

@bp.route("/setup", methods=["GET", "POST"])
def setup_page():
    """Création du premier administrateur. Accessible uniquement tant qu'aucun
    utilisateur n'existe (sinon → login)."""
    if not _setup_needed():
        return redirect(url_for("routes.login_page"))
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        if not username or len(pw) < MIN_PASSWORD_LEN:
            error = f"Identifiant requis et mot de passe d'au moins {MIN_PASSWORD_LEN} caractères."
        else:
            uid = db_create_user(username, hash_password(pw), "admin")
            login_user({"id": uid})
            st.set("setup_completed", True)
            _audit("system", "setup", f"Administrateur initial créé : {username}")
            return redirect(url_for("routes.home"))
    return render_template("login.html", setup=True, error=error)


# ─── Auth ───────────────────────────────────────────────────

@bp.route("/login", methods=["GET", "POST"])
def login_page():
    if _setup_needed():
        return redirect(url_for("routes.setup_page"))
    error = ""
    nxt = request.values.get("next") or ""
    if request.method == "POST":
        ip = _login_client_ip()
        if _login_is_blocked(ip):
            _audit("system", "login_blocked", ip)
            error = "Trop de tentatives. Réessayez dans quelques minutes."
            return render_template("login.html", error=error, next=nxt), 429
        username = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        user = db_get_user(username)
        if user and verify_password(pw, user["password_hash"]):
            _login_reset(ip)
            login_user(user)
            _audit("system", "login", username)
            return redirect(_safe_next(request.form.get("next")))
        _login_record_fail(ip)
        error = "Identifiant ou mot de passe incorrect."
    return render_template("login.html", error=error, next=nxt)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    logout_user()
    return redirect(url_for("routes.login_page"))


# ─── Pages ──────────────────────────────────────────────────

@bp.route("/")
@require_login
def home():
    secs = plugins.sections(user=current_user())
    ordered = sorted(secs.values(), key=lambda s: s["order"])
    return render_template("home.html", page="home", sections=ordered,
                           scan_errors=plugins.scan_errors() if has_perm("tools.manage") else {})


@bp.route("/tools/<section>")
@require_login
def tools_section(section):
    """Shell de test d'une section : onglets = outils accessibles de la section."""
    secs = plugins.sections(user=current_user())
    sec = secs.get(section)
    if not sec:
        abort(404)
    tools = [{"type": m["type"], "label": m.get("label", m["type"]),
              "runtime": plugins.runtime(m["type"]),
              "description": m.get("description", "")} for m in sec["plugins"]]
    return render_template("tool_section.html", page=section,
                           section_id=section, section_label=sec["label"], tools=tools)


@bp.route("/t/<type_>")
@require_tool
def tool_focus(type_):
    """Vue focus bookmarkable : l'outil sur toute la page, chrome minimal."""
    m = plugins.get(type_)
    if not m or plugins.is_disabled(type_):
        abort(404)
    return render_template("tool_focus.html", page="focus",
                           type=type_, label=m.get("label", type_),
                           runtime=plugins.runtime(type_),
                           section=(m.get("nav") or {}).get("section", "outils"))


@bp.route("/aide")
@require_login
def aide_page():
    return render_template("aide.html", page="aide")


@bp.route("/compte")
@require_login
def compte_page():
    """Mon compte : identité, préférences (langue, thème), mot de passe, accès."""
    return render_template("compte.html", page="compte")


@bp.route("/api/plugins/help")
@require_login
def plugins_help():
    """Agrège les help.md de tous les outils installés (non désactivés), rendus en HTML.
    Retourne [{type, label, version, category, order, html}] trié par catégorie puis order.
    Consommé par la page Aide (wiki) — cf. templates/aide.html. (Mécanisme repris de Studio.)"""
    import re as _re
    import markdown as _md
    articles = []
    for m in (plugins.all() or []):
        type_ = m.get("type") or ""
        if not type_ or plugins.is_disabled(type_):
            continue
        plugin_dir = m.get("_dir") or os.path.join(plugins.PLUGINS_DIR, type_)
        help_path = os.path.join(plugin_dir, "help.md")
        if not os.path.isfile(help_path):
            continue
        try:
            with open(help_path, encoding="utf-8") as f:
                md_text = f.read()
            # Le wiki (templates/aide.html) coiffe déjà l'article d'un <h1> = label de
            # l'outil. On retire le titre de tête du help.md pour éviter un second <h1>
            # (doublon de titre à l'écran + hiérarchie cassée pour les lecteurs d'écran).
            md_text = _re.sub(r"^\s*#\s+[^\n]*\n", "", md_text, count=1)
            html = _md.markdown(md_text, extensions=["tables", "fenced_code"])
        except Exception as e:
            html = f"<p><em>Erreur de rendu : {e}</em></p>"
        help_meta = m.get("help") or {}
        nav = m.get("nav") or {}
        articles.append({
            "type":     type_,
            "label":    m.get("label") or type_,
            "version":  m.get("version") or "",
            "category": help_meta.get("category") or nav.get("section") or "autres",
            "order":    int(help_meta.get("order") or nav.get("order") or 99),
            "html":     html,
        })
    articles.sort(key=lambda a: (a["category"], a["order"], a["label"]))
    return jsonify(articles)


@bp.route("/settings")
@require_login
def settings_page():
    # Admin/settings.edit → page complète. Opérateur (settings.theme seul) → page réduite
    # au choix du style (theme_only). Tout autre rôle (viewer) → interdit.
    if not (has_perm("settings.edit") or has_perm("settings.theme")):
        abort(403)
    return render_template("settings.html", page="settings",
                           services_present={m.get("id") for m in core_plugins.all()},
                           roles=ROLE_LABELS, permissions=PERMISSIONS,
                           themes=st.THEMES, current_theme=st.get("theme"),
                           tool_config_schemas=plugins.config_schemas(),
                           theme_only=not has_perm("settings.edit"))


# ─── Service (uptime / restart) ─────────────────────────────

@bp.route("/api/service/info")
@require_login
def api_service_info():
    return jsonify({"uptime_s": int(time.time() - _START_TS)})


@bp.route("/api/service/restart", methods=["POST"])
@require_perm("settings.edit")
def api_service_restart():
    _audit("system", "service_restart", "")

    def _do():
        time.sleep(0.5)
        # Sous systemd : restart propre ; sinon ré-exec du process.
        if subprocess.call(["bash", "-c", "command -v systemctl >/dev/null 2>&1"]) == 0:
            subprocess.Popen(["systemctl", "restart", "bobitools"])
        else:
            os.execv(sys.executable, [sys.executable, *sys.argv])
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})


# ─── Liste des outils (pour le lanceur / nav) ───────────────

@bp.route("/api/tools")
@require_login
def api_tools():
    out = []
    for m in plugins.all():
        t = m["type"]
        if plugins.is_disabled(t) or not can_access_tool(t):
            continue
        out.append({"type": t, "label": m.get("label", t),
                    "runtime": plugins.runtime(t),
                    "description": m.get("description", ""),
                    "section": (m.get("nav") or {}).get("section", "outils")})
    return jsonify(out)


# ─── Service des assets UI d'un outil ───────────────────────

_UI_KEYS = {"html": ("page_html", "text/html; charset=utf-8"),
            "js":   ("page_js",   "application/javascript; charset=utf-8"),
            "css":  ("page_css",  "text/css; charset=utf-8")}


@bp.route("/api/tools/<type_>/ui/<kind>")
@require_tool
def api_tool_ui(type_, kind):
    spec = _UI_KEYS.get(kind)
    if not spec:
        abort(404)
    key, mime = spec
    version = request.args.get("version") or None
    path = plugins.ui_asset_path(type_, key, version)
    if not path:
        # CSS/JS optionnels : renvoyer un corps vide plutôt qu'un 404 bruyant.
        if kind in ("css", "js"):
            return Response("", mimetype=mime.split(";")[0])
        abort(404)
    with open(path, encoding="utf-8") as f:
        return Response(f.read(), mimetype=mime.split(";")[0])


# ─── Cycle de vie d'un outil Docker ─────────────────────────

@bp.route("/api/tools/<type_>/lifecycle", methods=["GET", "POST"])
@require_tool
def api_tool_lifecycle(type_):
    if not deploy.is_docker_tool(type_):
        return jsonify({"running": True, "state": "inprocess",
                        "port": None, "available": True,
                        "missing_requirements": plugins.missing_requirements(type_)})
    if request.method == "GET":
        return jsonify(deploy.tool_status(type_))
    if not has_perm("tools.use"):
        return jsonify({"error": "forbidden"}), 403
    action = (_json().get("action") or "").lower()
    try:
        if action == "start":
            res = deploy.start_tool(type_)
            _audit(type_, "container_start", res.get("image", ""))
            return jsonify(res)
        if action == "stop":
            res = deploy.stop_tool(type_)
            _audit(type_, "container_stop", "")
            return jsonify(res)
        if action == "migrate":
            res, en_fond = deploy.migrate_async(type_)
            _audit(type_, "container_migrate" + (" (construction en arrière-plan)" if en_fond else ""),
                   res.get("available_version", ""))
            return jsonify(res), (202 if en_fond else 200)
        if action == "logs":
            return jsonify({"logs": deploy.tool_logs(type_)})
        return jsonify({"error": "action inconnue"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Stockage générique d'un outil ──────────────────────────

def _store_api_open(type_):
    """La route générique /store lit et écrit la table d'un outil SANS jamais appeler son
    code : elle court-circuite donc tout contrôle d'accès qu'un backend porterait sur ses
    propres objets. Un outil comme « coffre » range dans son store le propriétaire et les
    partages de chaque coffre — les exposer ici laisserait n'importe quel compte ayant
    accès à l'outil se réécrire propriétaire, puis révéler les mots de passe par la voie
    normale.

    Elle n'est donc ouverte qu'aux outils qui la déclarent explicitement
    (`"store_api": true` dans plugin.json), c'est-à-dire ceux dont le stockage EST l'API :
    des outils front-purs, sans logique d'accès par objet. Pour tous les autres, le backend
    est la seule porte."""
    m = plugins.get(type_) or {}
    return bool(m.get("store_api"))


_STORE_CLOSED = ("cet outil n'expose pas le stockage générique : "
                 "ses données passent par son propre backend, qui porte ses règles d'accès")


def _store_rights(type_, method, subpath, payload):
    """Le stockage générique est une ÉCRITURE de l'outil comme une autre : sans ce contrôle,
    un outil dont les données vivent dans le store (ex. notes) échappait à ses droits fins.
    L'outil le déclare dans `acl.routes` sous le chemin `store` / `store/{id}` ; le corps
    porte `scope` (et, en création, `name`/`value`)."""
    refus = droits.check_write(type_, method, subpath, payload,
                               droits.who_user(current_user()), has_perm)
    if refus:
        _audit(type_, "refus_droits", f"{method} /{subpath} — {refus[1].get('code')} "
               f"{refus[1].get('rule_mode') or ''} "
               f"{refus[1].get('missing_permission') or refus[1].get('permission') or ''}")
        return jsonify(refus[1]), refus[0]
    return None


@bp.route("/api/tools/<type_>/store", methods=["GET", "POST"])
@require_tool
def api_tool_store(type_):
    if not _store_api_open(type_):
        return jsonify({"error": _STORE_CLOSED}), 403
    scope = request.args.get("scope", "")
    if request.method == "GET":
        return jsonify(plugin_store_list(type_, scope))
    if not has_perm("tools.use"):
        return jsonify({"error": "forbidden"}), 403
    body = _json()
    refus = _store_rights(type_, "POST", "store", {**body, "scope": scope})
    if refus:
        return refus
    id_ = plugin_store_create(type_, scope, body.get("name"), body.get("value"),
                              unique_name=bool(body.get("unique_name")))
    if id_ is None:
        return jsonify({"error": "nom déjà utilisé"}), 409
    _audit(type_, "store_create", body.get("name") or "")
    return jsonify({"id": id_})


@bp.route("/api/tools/<type_>/store/<int:sid>", methods=["PUT", "DELETE"])
@require_tool
def api_tool_store_item(type_, sid):
    if not _store_api_open(type_):
        return jsonify({"error": _STORE_CLOSED}), 403
    if not has_perm("tools.use"):
        return jsonify({"error": "forbidden"}), 403
    cur = plugin_store_get(sid)
    if not cur or cur.get("type") != type_:
        return jsonify({"error": "introuvable"}), 404
    refus = _store_rights(type_, request.method, f"store/{sid}",
                          {**(_json() if request.method == "PUT" else {}),
                           "scope": cur.get("scope") or ""})
    if refus:
        return refus
    if request.method == "DELETE":
        plugin_store_delete(sid)
        _audit(type_, "store_delete", cur.get("name") or "")
        return jsonify({"ok": True})
    body = _json()
    plugin_store_update(sid, name=body.get("name"), value=body.get("value"))
    _audit(type_, "store_update", body.get("name") or cur.get("name") or "")
    return jsonify({"ok": True})


# ─── Accès par outil (qui peut ouvrir quel outil) ───────────

@bp.route("/api/tools/<type_>/access", methods=["GET", "POST"])
@require_perm("tools.manage")
def api_tool_access(type_):
    pol = dict(st.get("tool_access") or {})
    if request.method == "GET":
        return jsonify(pol.get(type_)
                       or {"roles": list(DEFAULT_TOOL_ROLES), "users": [], "groups": []})
    body = _json()
    # Mise à jour PARTIELLE : une clé absente du corps garde sa valeur. L'écran de
    # réglages n'envoie que les rôles quand on coche une case ; sans ça, il effacerait
    # au passage les utilisateurs et groupes autorisés à titre individuel.
    cur = pol.get(type_) or {}
    new = {"roles": list(cur.get("roles") or []),
           "users": [int(u) for u in (cur.get("users") or [])],
           "groups": [int(x) for x in (cur.get("groups") or [])]}
    if body.get("roles") is not None:
        new["roles"] = list(body.get("roles") or [])
    if body.get("users") is not None:
        new["users"] = [int(u) for u in (body.get("users") or [])]
    if body.get("groups") is not None:
        new["groups"] = [int(x) for x in (body.get("groups") or [])]
    pol[type_] = new
    st.set("tool_access", pol)
    _audit(type_, "access_set", json.dumps(pol[type_], ensure_ascii=False))
    return jsonify({"ok": True})


# ─── Droits fins d'un outil (lus par l'UI de l'outil) ──────

def _identity_headers(type_):
    """Identité de l'appelant transmise au conteneur d'un outil Docker. INFORMATIVE : la
    décision d'accès est prise par le cœur avant le proxy (le conteneur n'est joignable
    que par lui, sur 127.0.0.1). Elle sert à l'outil pour journaliser et adapter son UI."""
    u = current_user() or {}
    rs = droits.rights_summary(type_, u, has_perm)
    return {"X-BT-User": str(u.get("username") or ""), "X-BT-User-Id": str(u.get("id") or ""),
            "X-BT-Role": str(u.get("role") or ""), "X-BT-Perms": ",".join(rs["granted"])}


@bp.route("/api/tools/<type_>/rights")
@require_tool
def api_tool_rights(type_):
    """Ce que l'appelant peut faire dans cet outil : permissions accordées, et celles que
    des règles de périmètre restreignent (à vérifier par ressource via /rights/check)."""
    return jsonify(droits.rights_summary(type_, current_user(), has_perm))


@bp.route("/api/tools/<type_>/rights/check", methods=["POST"])
@require_tool
def api_tool_rights_check(type_):
    """Vérifie un lot {perm, resource} → [bool] : l'UI grise ce qui serait refusé."""
    checks = (_json().get("checks") or [])[:2000]
    who = droits.who_user(current_user())
    return jsonify({"results": [
        droits.check_resource(type_, str(c.get("perm") or ""), c.get("resource") or {}, who, has_perm)
        for c in checks if isinstance(c, dict)]})


# ─── Dispatch unifié : backend in-process OU proxy Docker ───

def _build_ctx(type_):
    """Contexte passé au backend in-process d'un outil."""
    m = plugins.get(type_) or {}
    u = current_user() or {}

    class _Store:
        # Opérations par id BORNÉES à l'outil : les identifiants de `plugin_store` sont
        # globaux, un id venu d'une URL peut donc désigner la ligne d'un autre outil.
        def list(self, scope=""):   return plugin_store_list(type_, scope)
        def get(self, sid):         return plugin_store_get(sid, type_=type_)
        def create(self, name, value, scope="", unique_name=False):
            return plugin_store_create(type_, scope, name, value, unique_name)
        def update(self, sid, name=None, value=None):
            return plugin_store_update(sid, name=name, value=value, type_=type_)
        def delete(self, sid):      return plugin_store_delete(sid, type_=type_)

    def _setting(key, default=None):
        v = st.get(f"{type_}__{key}")
        if v is None:
            v = st.get(key)
        return default if v is None else v

    def _audit_fn(action, detail=""):
        _audit(type_, action, detail)

    def _users():
        """Annuaire minimal (sans hash de mot de passe) : un outil qui gère un partage
        doit pouvoir proposer la liste des comptes sans importer le cœur."""
        return [{"id": x["id"], "username": x["username"], "role": x["role"],
                 "prenom": x.get("prenom") or "", "nom": x.get("nom") or ""}
                for x in db_list_users()]

    def _groups():
        return db_list_groups()

    def _my_groups():
        return sorted(user_group_ids())

    def _send_mail(subject, body, to=None, **kw):
        """Remet un e-mail au service `mail` (asynchrone, best-effort). Résolution
        paresseuse : le cœur ne dépend pas du service. Audit attribué à l'utilisateur."""
        mod = core_plugins.module("mail")
        if not mod:
            return {"queued": False, "error": "service mail absent"}
        kw.setdefault("actor", u.get("username") or type_)
        return mod.enqueue(subject, body, to=to, **kw)

    def _notify(message, **kw):
        """Pousse une notification via le service `ntfy` (asynchrone, best-effort). Résolution
        paresseuse : le cœur ne dépend pas du service. Audit attribué à l'utilisateur."""
        mod = core_plugins.module("ntfy")
        if not mod:
            return {"queued": False, "error": "service ntfy absent"}
        kw.setdefault("actor", u.get("username") or type_)
        return mod.publish(message, **kw)

    return plugins.Ctx({"user": u, "store": _Store(), "setting": _setting,
                        "audit": _audit_fn, "tool_dir": m.get("_dir"),
                        "send_mail": _send_mail, "notify": _notify,
                        "users": _users, "groups": _groups, "my_groups": _my_groups,
                        "has_perm": has_perm,
                        # Droit FIN : permission courte de l'outil (ex. "port.vlan") sur une
                        # ressource ({"switch": …, "port": …}), périmètre compris.
                        "can": lambda perm, resource=None: droits.check_resource(
                            type_, perm, resource, droits.who_user(u), has_perm),
                        "call": tools_mod.make_caller(type_, u.get("username") or type_)})


def _ember_notify(type_):
    """Prévient le provider Ember+ qu'un outil contributeur vient d'être modifié.

    Le provider ne reconstruit son arbre QUE sur sollicitation : sans ce signal, poser un
    slot ou une clé canonique ne se voyait au pupitre qu'après renavigation (ou redémarrage
    de l'app). Silencieux si le service est absent ou arrêté — ce n'est qu'une notification.
    """
    m = plugins.get(type_) or {}
    if not (m.get("ember") or m.get("ember_bindings") or m.get("ember_io")):
        return
    try:
        svc = core_plugins.module("emberplus")
        if svc and hasattr(svc, "refresh") and getattr(svc, "is_running", lambda: False)():
            svc.refresh()
            log.info("emberplus: rafraîchissement demandé après écriture sur %s", type_)
        else:
            log.info("emberplus: écriture sur %s — provider inactif, rien à notifier", type_)
    except Exception as e:
        log.warning("emberplus: notification après %s échouée : %s", type_, e)
    # Même signal vers les protocoles de pupitre. Sans lui, une affectation changée depuis
    # l'écran resterait invisible au pupitre : SW-P-08 ne prévoit pas qu'un contrôleur soit
    # sollicité, et celui qu'on vise ne redemande jamais rien de lui-même.
    try:
        swp = core_plugins.module("swp08")
        if swp and hasattr(swp, "notify_change") and getattr(swp, "is_running", lambda: False)():
            threading.Thread(target=swp.notify_change, daemon=True).start()
    except Exception as e:
        log.warning("swp08: notification après %s échouée : %s", type_, e)


@bp.route("/api/tools/<type_>/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
@require_tool
def api_tool_dispatch(type_, subpath):
    """Tout outil parle ici, quel que soit son runtime. L'UI ignore où tourne la logique."""
    m = plugins.get(type_)
    if not m:
        abort(404)
    method = request.method
    # Un outil DÉSACTIVÉ n'accepte plus d'écriture. Jusqu'ici le drapeau ne faisait que le
    # masquer du lanceur : une page déjà ouverte, un onglet oublié ou un appel direct
    # continuaient de piloter le matériel. Or on désactive justement un outil parce qu'il
    # fait quelque chose de nuisible — l'interrupteur doit couper, pas cacher.
    # La LECTURE reste permise : diagnostiquer un outil qu'on vient d'arrêter est
    # légitime, et n'écrit rien.
    if method != "GET" and plugins.is_disabled(type_):
        return jsonify({"error": "outil désactivé : seule la lecture reste possible"}), 409
    # Écrire via un outil exige `tools.use`, comme /store et /lifecycle. `require_tool`
    # ne garantit que l'ACCÈS à l'outil (can_access_tool) : sans ce garde, un viewer à qui
    # l'outil est explicitement ouvert pourrait déclencher des actions mutatives.
    if method != "GET" and not has_perm("tools.use"):
        return jsonify({"error": "forbidden"}), 403
    payload = _json() if method in ("POST", "PUT") else dict(request.args)
    # Droits FINS (permission déclarée par l'outil + périmètre), vérifiés ICI, avant de
    # transmettre : un conteneur n'a pas à être cru sur parole. Cf. app/droits.py.
    if method != "GET":
        refus = droits.check_write(type_, method, subpath, payload,
                                   droits.who_user(current_user()), has_perm)
        if refus:
            _audit(type_, "refus_droits", f"{method} /{subpath} — {refus[1].get('code')} "
                   f"{refus[1].get('rule_mode') or ''} "
                   f"{refus[1].get('missing_permission') or refus[1].get('permission') or ''} "
                   f"{json.dumps(refus[1].get('resource') or {}, ensure_ascii=False)}")
            return jsonify(refus[1]), refus[0]

    # Journalise les actions mutatives (lecture = pas de bruit dans l'audit).
    # Les secrets (token, mot de passe…) sont masqués avant journalisation.
    if method != "GET":
        _audit(type_, f"{method} /{subpath}",
               json.dumps(_redact(payload), ensure_ascii=False)[:500] if payload else "")

    if plugins.runtime(type_) == "docker":
        base = deploy.proxy_base(type_)
        if not base:
            return jsonify({"error": "outil non démarré"}), 503
        try:
            up = requests.request(method, f"{base}/{subpath}",
                                  params=request.args if method == "GET" else None,
                                  json=payload if method in ("POST", "PUT") else None,
                                  headers=_identity_headers(type_), timeout=30)
            ct = up.headers.get("Content-Type", "application/json")
            if method != "GET" and up.status_code < 400:
                _ember_notify(type_)
            return Response(up.content, status=up.status_code, mimetype=ct.split(";")[0])
        except requests.RequestException as e:
            return jsonify({"error": f"proxy : {e}"}), 502

    # in-process
    mod = plugins.backend(type_)
    if not mod or not hasattr(mod, "api"):
        return jsonify({"error": "cet outil n'a pas de backend"}), 404
    try:
        res = mod.api(subpath, method, payload, _build_ctx(type_))
    except Exception as e:
        log.exception("backend %s a levé une exception", type_)
        return jsonify({"error": str(e)}), 500
    # Un backend peut renvoyer un Response Flask tel quel (ex. servir une image binaire) :
    # le dispatch jsonifie tout le reste, donc c'est la seule voie pour du non-JSON.
    if isinstance(res, Response):
        return res
    if isinstance(res, tuple) and len(res) == 2:
        status, data = res
        if method != "GET" and status < 400:
            _ember_notify(type_)
        return jsonify(data), status
    if method != "GET":
        _ember_notify(type_)
    return jsonify(res)


# ─── Réglages génériques ────────────────────────────────────

def _coerce_tool_settings(payload):
    """Borne les réglages par-outil (clés `<type>__<clé>`) selon le `config_schema` de
    l'outil : min/max/type garantis côté serveur, en plus de la validation front. Les
    clés hors schéma (réglages globaux du cœur) sont laissées intactes."""
    schemas = plugins.config_schemas()
    if not schemas:
        return payload
    out = dict(payload or {})
    by_type = {}                                  # type -> {clé nue: valeur}
    for full, v in list(out.items()):
        t, sep, k = full.partition("__")
        if sep and t in schemas:
            by_type.setdefault(t, {})[k] = v
    for t, params in by_type.items():
        coerced = plugins.coerce_config(t, params)
        for k, v in coerced.items():
            out[f"{t}__{k}"] = v
    return out


@bp.route("/api/settings", methods=["GET", "POST"])
@require_perm("settings.edit")
def api_settings():
    if request.method == "GET":
        # `public()` et non `all()` : la table `settings` contient aussi des valeurs qui ne
        # sont réglages de personne — dont la clé de signature des sessions — et les mots
        # de passe des réglages n'ont pas à redescendre dans le navigateur.
        return jsonify(st.public())
    payload = _coerce_tool_settings(_json())
    n = st.update_bulk(payload)
    _audit("system", "settings_update", f"{n} clé(s)")
    if "ui_custom_languages" in _json():
        i18n_mod.reload()
    return jsonify({"ok": True, "accepted": n})


@bp.route("/api/settings/theme", methods=["POST"])
@require_perm("settings.theme")
def api_settings_theme():
    """Changer UNIQUEMENT le thème (style). Accessible aux opérateurs (settings.theme),
    contrairement au reste des réglages. Surface volontairement étroite : aucune autre clé
    n'est touchée, le thème est validé contre la liste connue."""
    theme = (_json() or {}).get("theme")
    if theme not in {t["id"] for t in st.THEMES}:
        return jsonify({"error": "thème inconnu"}), 400
    st.set("theme", theme)
    _audit("system", "settings_update", f"theme={theme}")
    return jsonify({"ok": True, "theme": theme})


# NB : les routes /api/emberplus/* vivent désormais dans le service lui-même
# (services/emberplus/__init__.py:register_routes), montées par core_plugins.


@bp.route("/api/brand/logo", methods=["POST"])
@require_perm("settings.edit")
def api_brand_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "aucun fichier"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    # SVG volontairement exclu : servi en same-origin, il peut porter du JS (XSS stocké).
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return jsonify({"error": "format non supporté"}), 400
    updir = os.path.join(current_app.static_folder, "uploads")
    os.makedirs(updir, exist_ok=True)
    name = f"brand-logo{ext}"
    f.save(os.path.join(updir, name))
    url = f"/static/uploads/{name}"
    st.set("brand_logo_url", url)
    return jsonify({"ok": True, "url": url})


# ─── Utilisateurs ───────────────────────────────────────────

@bp.route("/api/users", methods=["GET", "POST"])
@require_perm("settings.edit")
def api_users():
    if request.method == "GET":
        return jsonify(db_list_users())
    body = _json()
    username = (body.get("username") or "").strip()
    pw = body.get("password") or ""
    role = body.get("role") if body.get("role") in ROLES else "viewer"
    if not username or len(pw) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"identifiant + mot de passe (≥{MIN_PASSWORD_LEN}) requis"}), 400
    if db_get_user(username):
        return jsonify({"error": "identifiant déjà pris"}), 409
    uid = db_create_user(username, hash_password(pw), role,
                         body.get("prenom"), body.get("nom"), body.get("email"))
    _audit("system", "user_create", f"{username} ({role})")
    return jsonify({"id": uid})


@bp.route("/api/users/<int:uid>", methods=["PATCH", "DELETE"])
@require_perm("settings.edit")
def api_user_item(uid):
    target = db_get_user_by_id(uid)
    if not target:
        return jsonify({"error": "introuvable"}), 404
    me = current_user() or {}
    admins = [x for x in db_list_users() if x.get("role") == ROLE_INTOUCHABLE]
    dernier_admin = target.get("role") == ROLE_INTOUCHABLE and len(admins) <= 1
    if request.method == "DELETE":
        if db_count_users() <= 1:
            return jsonify({"error": "impossible de supprimer le dernier compte"}), 400
        if target["id"] == me.get("id"):
            return jsonify({"error": "impossible de supprimer son propre compte"}), 400
        if dernier_admin:
            return jsonify({"error": "impossible de supprimer le dernier administrateur"}), 400
        db_delete_user(uid)
        _audit("system", "user_delete", target["username"])
        return jsonify({"ok": True})
    body = _json()
    new_pw = body.get("password")
    if new_pw is not None and new_pw != "":
        if len(new_pw) < MIN_PASSWORD_LEN:
            return jsonify({"error": f"mot de passe trop court (≥{MIN_PASSWORD_LEN})"}), 400
        pw_hash = hash_password(new_pw)
    else:
        pw_hash = None
    role = body.get("role") if body.get("role") in ROLES else None
    if role and role != ROLE_INTOUCHABLE and dernier_admin:
        return jsonify({"error": "impossible de rétrograder le dernier administrateur"}), 400
    db_update_user(uid, role=role, password_hash=pw_hash,
                   prenom=body.get("prenom"), nom=body.get("nom"),
                   email=body.get("email"), lang=body.get("lang"))
    _audit("system", "user_update", target["username"])
    return jsonify({"ok": True})


# ─── Groupes d'utilisateurs ─────────────────────────────────
# Un groupe ne porte aucune permission : c'est un destinataire nommé, réutilisable par
# toute politique de partage (accès par outil, coffres du plugin « coffre »…).

@bp.route("/api/groups", methods=["GET", "POST"])
@require_login
def api_groups():
    # Lecture ouverte à tout compte connecté : un outil doit pouvoir proposer « partager
    # avec le groupe X » sans exiger les droits de réglages. L'écriture reste réservée.
    if request.method == "GET":
        return jsonify(db_list_groups())
    if not has_perm("settings.edit"):
        return jsonify({"error": "forbidden", "missing_permission": "settings.edit"}), 403
    body = _json()
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "nom requis"}), 400
    gid = db_create_group(name, body.get("description") or "")
    if gid is None:
        return jsonify({"error": "nom de groupe déjà pris"}), 409
    if body.get("members") is not None:
        db_update_group(gid, members=body.get("members"))
    _audit("system", "group_create", name)
    return jsonify({"id": gid})


@bp.route("/api/groups/<int:gid>", methods=["PATCH", "DELETE"])
@require_perm("settings.edit")
def api_group_item(gid):
    g_ = db_get_group(gid)
    if not g_:
        return jsonify({"error": "introuvable"}), 404
    if request.method == "DELETE":
        db_delete_group(gid)
        # Le groupe disparaît aussi des politiques d'accès aux outils, sinon un futur
        # groupe réutilisant l'id hériterait de ses droits.
        pol = dict(st.get("tool_access") or {})
        touched = False
        for type_, p in pol.items():
            keep = [x for x in (p.get("groups") or []) if int(x) != gid]
            if len(keep) != len(p.get("groups") or []):
                pol[type_] = dict(p, groups=keep)
                touched = True
        if touched:
            st.set("tool_access", pol)
        _audit("system", "group_delete", g_["name"])
        return jsonify({"ok": True})
    body = _json()
    ok = db_update_group(gid, name=body.get("name"), description=body.get("description"),
                         members=body.get("members"))
    if not ok:
        return jsonify({"error": "nom de groupe déjà pris"}), 409
    _audit("system", "group_update", body.get("name") or g_["name"])
    return jsonify({"ok": True})


# ─── Rôles (modifiables) ────────────────────────────────────
# Matrice rôles × permissions, comme les habilitations de Bobi.Studio. Chaque bascule est
# un PATCH immédiat ; la réponse renvoie l'état réel, que l'UI reprend tel quel.

_ROLE_ID = __import__("re").compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _roles_payload():
    from app.database import db_roles_list, db_count_users_by_role
    counts = db_count_users_by_role()
    rows = {r["id"]: r for r in db_roles_list()}
    roles = []
    for rid in ROLES:
        r = rows.get(rid) or {}
        roles.append({"id": rid, "label": ROLE_LABELS.get(rid, rid),
                      "builtin": bool(r.get("builtin")) or rid in ROLES_DEFAUT,
                      "locked": rid == ROLE_INTOUCHABLE,
                      "permissions": sorted(ROLES.get(rid) or []), "users": counts.get(rid, 0)})
    return {"roles": roles,
            "catalogue": {"core": list(PERMISSIONS), "tools": plugin_permissions()},
            "me": (current_user() or {}).get("role")}


@bp.route("/api/roles", methods=["GET", "POST"])
@require_perm("settings.edit")
def api_roles():
    from app.database import db_role_upsert
    if request.method == "POST":
        body = _json()
        rid = (body.get("id") or "").strip().lower()
        if not _ROLE_ID.match(rid):
            return jsonify({"error": "identifiant invalide (a-z, 0-9, _ et -, 2 à 32 caractères)"}), 400
        if rid in ROLES:
            return jsonify({"error": "ce rôle existe déjà"}), 409
        db_role_upsert(rid, label=(body.get("label") or "").strip() or rid, permissions=[])
        recharger_roles()
        _audit("system", "role_create", rid)
    return jsonify(_roles_payload())


@bp.route("/api/roles/<rid>", methods=["PATCH", "DELETE"])
@require_perm("settings.edit")
def api_role_item(rid):
    from app.database import db_role_upsert, db_role_delete, db_count_users_by_role
    if rid not in ROLES:
        return jsonify({"error": "rôle inconnu"}), 404
    if rid == ROLE_INTOUCHABLE:
        return jsonify({"error": "le rôle administrateur n'est pas modifiable"}), 400
    if request.method == "DELETE":
        n = db_count_users_by_role().get(rid, 0)
        if n:
            return jsonify({"error": f"{n} compte(s) portent ce rôle : les réaffecter d'abord"}), 400
        if rid in ROLES_DEFAUT:
            return jsonify({"error": "un rôle intégré se réinitialise, il ne se supprime pas"}), 400
        db_role_delete(rid)
        recharger_roles()
        _audit("system", "role_delete", rid)
        return jsonify(_roles_payload())
    body = _json()
    perms = set(ROLES.get(rid) or set())
    known = set(all_permissions())
    if body.get("reset"):
        if rid not in ROLES_DEFAUT:
            return jsonify({"error": "seul un rôle intégré se réinitialise"}), 400
        label, defaults = ROLES_DEFAUT[rid]
        # Défaut = cœur d'origine + permissions d'outil dont ce rôle est un default_roles.
        perms = set(defaults) | {p["id"] for p in plugin_permissions() if rid in p["default_roles"]}
        db_role_upsert(rid, label=label, permissions=sorted(perms & known))
        detail = "reset"
    else:
        if "permissions" in body and isinstance(body["permissions"], list):
            perms = {p for p in body["permissions"] if p in known}
        if body.get("perm") in known and "on" in body:
            (perms.add if body.get("on") else perms.discard)(body["perm"])
        label = body.get("label")
        db_role_upsert(rid, label=label.strip() if isinstance(label, str) else None,
                       permissions=sorted(perms))
        detail = (f"{'+' if body.get('on') else '-'}{body['perm']}" if body.get("perm")
                  else ("label" if label is not None else "permissions"))
    recharger_roles()
    _audit("system", "role_update", f"{rid} {detail}")
    return jsonify(_roles_payload())


# ─── Règles de périmètre ────────────────────────────────────

def _acl_tools():
    """Outils qui déclarent des permissions (et donc peuvent recevoir des règles)."""
    out = []
    for m in plugins.all():
        perms = droits.tool_permissions(m.get("type"))
        if not perms:
            continue
        acl = droits.tool_acl(m.get("type"))
        out.append({"type": m.get("type"), "label": m.get("label") or m.get("type"),
                    "permissions": [{"id": p["id"], "label": p.get("label") or p["id"]} for p in perms],
                    "resources": acl.get("resources") or []})
    return sorted(out, key=lambda x: x["label"].lower())


def _clean_controllers(items):
    """Contrôleurs cités par une règle : « ctl:<id> » d'un contrôleur déclaré, ou une
    adresse/plage IP brute (équipement non déclaré). Le reste est refusé."""
    import ipaddress
    from app.database import db_controllers_list
    known = {f"ctl:{c['id']}" for c in db_controllers_list()}
    out = []
    for x in items:
        x = str(x).strip()
        if not x:
            continue
        if x.startswith("ctl:"):
            if x not in known:
                raise ValueError(f"contrôleur inconnu : {x}")
        else:
            try:
                ipaddress.ip_network(x, strict=False)
            except ValueError:
                raise ValueError(f"adresse de contrôleur invalide : {x}")
        out.append(x)
    return out


def _clean_rule(body):
    tool = (body.get("tool") or "").strip()
    known = {p["id"] for p in droits.tool_permissions(tool)}
    if not known:
        raise ValueError("outil inconnu ou sans permissions déclarées")
    perms = [p for p in (body.get("perms") or []) if p in known]
    if not perms:
        raise ValueError("choisir au moins une permission")
    pr = body.get("principals") or {}
    principals = {"users": [int(x) for x in pr.get("users") or [] if str(x).isdigit()],
                  "groups": [int(x) for x in pr.get("groups") or [] if str(x).isdigit()],
                  "roles": [str(x) for x in pr.get("roles") or [] if str(x) in ROLES],
                  "controllers": _clean_controllers(pr.get("controllers") or [])}
    if not any(principals.values()):
        raise ValueError("choisir au moins un bénéficiaire (utilisateur, groupe, rôle ou contrôleur)")
    keys = {r.get("key") for r in droits.tool_acl(tool).get("resources") or []}
    scope = {}
    for k, sel in (body.get("scope") or {}).items():
        if k not in keys or sel in (None, "*", "", {}):
            continue
        if not isinstance(sel, dict):
            raise ValueError(f"sélecteur invalide pour {k}")
        clean = {}
        for a, v in sel.items():
            if isinstance(v, list):
                v = [str(x).strip() for x in v if str(x).strip()]
            elif isinstance(v, str):
                v = v.strip()
            if v:
                clean[str(a)] = v
        if clean:
            scope[k] = clean
    mode = body.get("mode") if body.get("mode") in droits.RULE_MODES else "allow"
    if mode == "reserve" and not scope:
        raise ValueError("une réservation porte sur des ressources : choisir « certains » pour au moins une")
    return {"tool": tool, "perms": perms, "principals": principals, "scope": scope,
            "note": (body.get("note") or "").strip()[:300], "mode": mode}


@bp.route("/api/access-rules", methods=["GET", "POST"])
@require_perm("tools.manage")
def api_access_rules():
    from app.database import db_rules_list, db_rule_save
    if request.method == "POST":
        try:
            rule = _clean_rule(_json())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        rid = db_rule_save(rule)
        droits.forget_resolved(rule["tool"])
        _audit(rule["tool"], "rule_create", json.dumps({"id": rid, **rule}, ensure_ascii=False)[:500])
        return jsonify({"ok": True, "id": rid})
    from app.database import db_controllers_list
    return jsonify({"rules": db_rules_list(), "tools": _acl_tools(),
                    "roles": ROLE_LABELS,
                    "controllers": [{"id": f"ctl:{c['id']}", "name": c["name"],
                                     "addresses": c["addresses"]} for c in db_controllers_list()]})


@bp.route("/api/access-rules/impact", methods=["POST"])
@require_perm("tools.manage")
def api_access_rule_impact():
    """Ce que changerait l'enregistrement d'une règle, CHIFFRÉ, pour l'avertissement de
    l'écran : par permission, qui perd quoi (comptes non-admin qui portent la permission par
    leur rôle, et contrôleurs déclarés). Calcul sur les bénéficiaires ; le périmètre exact
    (quels ports…) est rappelé tel que saisi."""
    from app.database import db_rules_list, db_controllers_list
    body = _json()
    try:
        rule = _clean_rule(body.get("rule") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    rid = body.get("id")
    others = [r for r in db_rules_list(rule["tool"]) if r["id"] != rid]
    users = [u for u in db_list_users() if u.get("role") != ROLE_INTOUCHABLE]
    ctls = db_controllers_list()
    labels = {p["id"]: p.get("label") or p["id"] for p in droits.tool_permissions(rule["tool"])}
    out = []
    for perm in rule["perms"]:
        full = f"{rule['tool']}.{perm}"
        holders = [u for u in users if full in ROLES.get(u.get("role"), set())]
        same = [r for r in others if perm in r["perms"] and droits.rule_mode(r) == rule["mode"]] + [rule]
        def covered(who):
            return any(droits._principal_matches(r["principals"], who) for r in same)
        def ctl_named(c, rules):
            return any(f"ctl:{c['id']}" in (r["principals"].get("controllers") or [])
                       or any(a in (r["principals"].get("controllers") or []) for a in c["addresses"])
                       for r in rules)
        if rule["mode"] == "deny":
            # Interdire : perdent ceux que CETTE règle nomme (et qui avaient la permission).
            losers = [u["username"] for u in holders
                      if droits._principal_matches(rule["principals"], droits.who_user(u))]
            ctl_out = [c["name"] for c in ctls if ctl_named(c, [rule])]
        else:
            losers = [u["username"] for u in holders if not covered(droits.who_user(u))]
            ctl_out = [c["name"] for c in ctls if not ctl_named(c, same)]
        out.append({"perm": perm, "label": labels.get(perm, perm), "mode": rule["mode"],
                    "first": rule["mode"] == "allow" and not any(
                        perm in r["perms"] and droits.rule_mode(r) == "allow" for r in others),
                    "losers": losers, "controllers": ctl_out, "holders": len(holders),
                    "everywhere": not rule["scope"]})
    return jsonify({"impact": out})


@bp.route("/api/access-rules/<int:rid>", methods=["PUT", "DELETE"])
@require_perm("tools.manage")
def api_access_rule_item(rid):
    from app.database import db_rule_get, db_rule_save, db_rule_delete
    cur = db_rule_get(rid)
    if not cur:
        return jsonify({"error": "règle inconnue"}), 404
    if request.method == "DELETE":
        db_rule_delete(rid)
        _audit(cur["tool"], "rule_delete", json.dumps(cur, ensure_ascii=False)[:500])
        return jsonify({"ok": True})
    try:
        rule = _clean_rule(_json())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    db_rule_save(rule, rid)
    _audit(rule["tool"], "rule_update", json.dumps({"id": rid, **rule}, ensure_ascii=False)[:500])
    return jsonify({"ok": True, "id": rid})


@bp.route("/api/access-rules/choices/<type_>/<key>")
@require_perm("tools.manage")
def api_access_rule_choices(type_, key):
    """Valeurs proposées pour une clé de ressource (ex. la liste des switchs), lues chez
    l'outil via le chemin `choices` déclaré. Format attendu : liste de {id, name} ou d'objets
    portant `id` et `name`/`label`."""
    res = next((r for r in droits.tool_acl(type_).get("resources") or [] if r.get("key") == key), None)
    if not res or not res.get("choices"):
        return jsonify({"choices": []})
    status, data = tools_mod.call(type_, res["choices"], "GET", actor="réglages", timeout=10)
    if status != 200:
        return jsonify({"choices": [], "error": (data or {}).get("error") if isinstance(data, dict) else None})
    # Forme de la liste déclarée par l'outil : clé du tableau (`choices_list`), champ id
    # (`choices_id`, défaut « id ») et champ affiché (`choices_name`, défaut name/label).
    lk = res.get("choices_list")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get(lk) if lk else (data.get("items") or data.get(key + "s") or [])
        if isinstance(items, dict):   # {id: objet} → liste
            items = [{**(v if isinstance(v, dict) else {}), "__id": k} for k, v in items.items()]
    else:
        items = []
    fid, fname = res.get("choices_id") or "id", res.get("choices_name")
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        ident = it.get(fid, it.get("__id"))
        if ident is None:
            continue
        name = it.get(fname) if fname else (it.get("name") or it.get("label"))
        out.append({"id": str(ident), "name": str(name or ident)})
    return jsonify({"choices": out})


# ─── Contrôleurs broadcast (Ember+ / SW-P-08) ───────────────

def _clean_controller(body):
    import ipaddress
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("nom requis")
    addrs = []
    for a in body.get("addresses") or []:
        a = str(a).strip()
        if not a:
            continue
        try:
            addrs.append(str(ipaddress.ip_network(a, strict=False)) if "/" in a else str(ipaddress.ip_address(a)))
        except ValueError:
            raise ValueError(f"adresse invalide : {a}")
    if not addrs:
        raise ValueError("au moins une adresse IP (ou plage CIDR)")
    protos = [p for p in (body.get("protocols") or []) if p in ("emberplus", "swp08")]
    return {"name": name[:80], "addresses": addrs, "protocols": protos,
            "note": (body.get("note") or "").strip()[:300]}


@bp.route("/api/controllers", methods=["GET", "POST"])
@require_perm("settings.edit")
def api_controllers():
    from app.database import db_controllers_list, db_controller_save
    if request.method == "POST":
        try:
            c = _clean_controller(_json())
            cid = db_controller_save(c)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:   # nom UNIQUE
            return jsonify({"error": f"enregistrement impossible : {e}"}), 409
        _audit("system", "controller_create", json.dumps(c, ensure_ascii=False))
        return jsonify({"ok": True, "id": cid})
    rules = __import__("app.database", fromlist=["x"]).db_rules_list()
    used = {}
    for r in rules:
        for x in (r.get("principals") or {}).get("controllers") or []:
            used[x] = used.get(x, 0) + 1
    return jsonify({"controllers": [{**c, "rules": used.get(f"ctl:{c['id']}", 0)}
                                    for c in db_controllers_list()]})


@bp.route("/api/controllers/<int:cid>", methods=["PUT", "DELETE"])
@require_perm("settings.edit")
def api_controller_item(cid):
    from app.database import db_controllers_list, db_controller_save, db_controller_delete, db_rules_list
    cur = next((c for c in db_controllers_list() if c["id"] == cid), None)
    if not cur:
        return jsonify({"error": "contrôleur inconnu"}), 404
    if request.method == "DELETE":
        n = sum(1 for r in db_rules_list() if f"ctl:{cid}" in ((r.get("principals") or {}).get("controllers") or []))
        if n:
            return jsonify({"error": f"{n} règle(s) de périmètre le citent : les modifier d'abord"}), 400
        db_controller_delete(cid)
        _audit("system", "controller_delete", cur["name"])
        return jsonify({"ok": True})
    try:
        c = _clean_controller(_json())
        db_controller_save(c, cid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"enregistrement impossible : {e}"}), 409
    _audit("system", "controller_update", json.dumps(c, ensure_ascii=False))
    return jsonify({"ok": True})


@bp.route("/api/me/lang", methods=["POST"])
@require_login
def api_me_lang():
    """Préférence de langue de l'utilisateur courant (changement immédiat)."""
    lang = (_json().get("lang") or "").strip()
    if lang not in i18n_mod.LANG_CODES:
        return jsonify({"error": "langue inconnue"}), 400
    db_update_user(current_user()["id"], lang=lang)
    return jsonify({"ok": True})


@bp.route("/api/me/theme", methods=["POST"])
@require_login
def api_me_theme():
    """Thème de l'utilisateur courant — PRÉFÉRENCE PERSONNELLE, comme la langue.
    `theme: ""` remet le compte sur le défaut du système (setting global `theme`)."""
    theme = (_json().get("theme") or "").strip()
    if theme and theme not in {t["id"] for t in st.THEMES}:
        return jsonify({"error": "thème inconnu"}), 400
    db_update_user(current_user()["id"], theme=theme)
    return jsonify({"ok": True, "theme": theme})


@bp.route("/api/me", methods=["GET", "PATCH"])
@require_login
def api_me():
    """Fiche du compte COURANT. Lecture, et modification de ce qui lui appartient :
    prénom, nom, e-mail. Le rôle et l'identifiant restent l'affaire d'un administrateur."""
    u = current_user()
    if request.method == "PATCH":
        body = _json()
        champs = {}
        for k in ("prenom", "nom", "email"):
            if k in body:
                v = (body.get(k) or "").strip()
                if len(v) > 200:
                    return jsonify({"error": f"{k} trop long"}), 400
                champs[k] = v
        if champs.get("email") and "@" not in champs["email"]:
            return jsonify({"error": "adresse e-mail invalide"}), 400
        db_update_user(u["id"], **champs)
        _audit("system", "me_update", ", ".join(sorted(champs)))
        return jsonify({"ok": True})
    mes_groupes = set(user_group_ids(u))
    perms = ROLES.get(u.get("role"), set())
    return jsonify({
        "id": u["id"], "username": u["username"], "role": u.get("role"),
        "role_label": ROLE_LABELS.get(u.get("role"), u.get("role")),
        "prenom": u.get("prenom") or "", "nom": u.get("nom") or "",
        "email": u.get("email") or "", "lang": u.get("lang") or "",
        "theme": u.get("theme") or "", "created_at": u.get("created_at"),
        "permissions": [{"cle": p, "accordee": u.get("role") == ROLE_INTOUCHABLE or p in perms}
                        for p in PERMISSIONS],
        "permissions_outils": [{"cle": p["id"], "label": p["label"], "outil": p["tool_label"],
                                "accordee": u.get("role") == ROLE_INTOUCHABLE or p["id"] in perms}
                               for p in plugin_permissions()],
        "groupes": [g["name"] for g in db_list_groups() if g["id"] in mes_groupes],
        "outils": sorted(({"type": m.get("type"), "label": m.get("label") or m.get("type")}
                          for m in plugins.all() if can_access_tool(m.get("type"), u)),
                         key=lambda o: (o["label"] or "").lower()),
        "min_password_len": MIN_PASSWORD_LEN,
    })


@bp.route("/api/me/password", methods=["POST"])
@require_login
def api_me_password():
    """Changement de SON mot de passe : l'ancien est exigé (une session laissée ouverte ne
    doit pas suffire à s'approprier le compte)."""
    body = _json()
    old = body.get("old_password") or ""
    new = body.get("new_password") or ""
    u = current_user()
    if not verify_password(old, u["password_hash"]):
        return jsonify({"error": "ancien mot de passe incorrect"}), 403
    if len(new) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"mot de passe trop court (≥{MIN_PASSWORD_LEN})"}), 400
    if verify_password(new, u["password_hash"]):
        return jsonify({"error": "le nouveau mot de passe est identique à l'ancien"}), 400
    db_update_user(u["id"], password_hash=hash_password(new))
    _audit("system", "me_password", "")
    return jsonify({"ok": True})


# ─── Journal d'audit ────────────────────────────────────────

@bp.route("/api/system/network")
@require_perm("settings.edit")
def api_system_network():
    """État réseau de la MACHINE. Lecture seule — cf. app/system_net.py, qui explique
    pourquoi une modification depuis l'intérieur d'un conteneur n'est pas durable."""
    from app import system_net
    out = system_net.state()
    out["pending"] = system_net.pending()
    return jsonify(out)


@bp.route("/api/system/network/change", methods=["POST"])
@require_perm("settings.edit")
def api_system_network_change():
    """Change l'adresse d'une interface. Le changement est appliqué MAIS révoqué
    automatiquement s'il n'est pas confirmé depuis la nouvelle adresse (cf. system_net)."""
    from app import system_net
    d = _json()
    ok, res = system_net.change(d.get("iface") or "", d.get("address") or "",
                                d.get("gateway") or None,
                                persist=bool(d.get("persist", True)),
                                delay=d.get("delay"))
    if not ok:
        return jsonify({"error": res}), 400
    _audit("system", "network_change",
           "%s → %s (passerelle %s), confirmation sous %s s"
           % (d.get("iface"), d.get("address"), d.get("gateway") or "—", res["delay"]))
    return jsonify(res)


@bp.route("/api/system/network/confirm", methods=["POST"])
@require_perm("settings.edit")
def api_system_network_confirm():
    from app import system_net
    ok, err = system_net.confirm((_json().get("token") or ""))
    if not ok:
        return jsonify({"error": err}), 400
    _audit("system", "network_confirm", "changement réseau confirmé")
    return jsonify({"ok": True})


@bp.route("/api/system/network/cancel", methods=["POST"])
@require_perm("settings.edit")
def api_system_network_cancel():
    from app import system_net
    ok, err = system_net.cancel()
    if not ok:
        return jsonify({"error": err}), 400
    _audit("system", "network_cancel", "changement réseau annulé, retour arrière")
    return jsonify({"ok": True})


@bp.route("/api/audit")
@require_perm("tools.manage")
def api_audit():
    tool = request.args.get("tool") or None
    uid = request.args.get("user")
    uid = int(uid) if uid and uid.isdigit() else None
    rows = audit_list(tool=tool, user_id=uid, limit=int(request.args.get("limit", 500)))
    return jsonify({"rows": rows, "tools": audit_tools()})


# ─── Éditeur i18n ───────────────────────────────────────────

@bp.route("/api/i18n/langs")
@require_perm("settings.edit")
def api_i18n_langs():
    return jsonify(i18n_mod.LANGUAGES)


@bp.route("/api/i18n/rows")
@require_perm("settings.edit")
def api_i18n_rows():
    lang = request.args.get("lang", "en")
    return jsonify(i18n_mod.editor_rows(lang))


@bp.route("/api/i18n/override", methods=["POST"])
@require_perm("settings.edit")
def api_i18n_override():
    b = _json()
    i18n_mod.set_override(b.get("lang"), b.get("key"), b.get("value"))
    return jsonify({"ok": True})


@bp.route("/api/i18n/lang", methods=["POST", "DELETE"])
@require_perm("settings.edit")
def api_i18n_lang():
    b = _json()
    if request.method == "DELETE":
        i18n_mod.remove_language(b.get("code"))
        return jsonify({"ok": True})
    res = i18n_mod.add_language(b.get("code"), b.get("label"))
    return jsonify(res)


@bp.route("/api/i18n/export")
@require_perm("settings.edit")
def api_i18n_export():
    lang = request.args.get("lang", "fr")
    data = i18n_mod.export_catalog(lang)
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name=f"bobitools-{lang}.json")


@bp.route("/api/i18n/import", methods=["POST"])
@require_perm("settings.edit")
def api_i18n_import():
    b = _json()
    n = i18n_mod.import_overrides(b.get("lang"), b.get("strings") or {})
    return jsonify({"ok": True, "count": n})


# ─── Sauvegarde ─────────────────────────────────────────────

@bp.route("/api/backup/list")
@require_perm("settings.edit")
def api_backup_list():
    return jsonify({"backups": backup.list_backups(),
                    "last_date": st.get("backup_last_date"),
                    "last_status": st.get("backup_last_status")})


@bp.route("/api/backup/run", methods=["POST"])
@require_perm("settings.edit")
def api_backup_run():
    try:
        path = backup.run_backup()
        return jsonify({"ok": True, "file": os.path.basename(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Gestion des plugins (outils) ───────────────────────────

@bp.route("/api/plugins")
@require_perm("tools.manage")
def api_plugins():
    out = []
    pol = st.get("tool_access") or {}
    for m in plugins.all():
        t = m["type"]
        entry = {
            "type": t, "label": m.get("label", t),
            "version": m.get("version"), "runtime": plugins.runtime(t),
            "description": m.get("description", ""),
            "disabled": plugins.is_disabled(t),
            "section": (m.get("nav") or {}).get("section", "outils"),
            "versions": plugins.versions(t),
            "access": pol.get(t),
        }
        if plugins.runtime(t) == "docker":
            try:                       # version réellement en cours vs disponible (migration)
                ds = deploy.tool_status(t)
                entry.update(running=ds.get("running"),
                             running_version=ds.get("running_version"),
                             available_version=ds.get("available_version"),
                             up_to_date=ds.get("up_to_date"))
            except Exception:
                pass
        out.append(entry)
    out.sort(key=lambda x: x["type"])
    return jsonify({"plugins": out, "scan_errors": plugins.scan_errors(),
                    "roles": ROLE_LABELS})


@bp.route("/api/plugins/<type_>/toggle", methods=["POST"])
@require_perm("tools.manage")
def api_plugin_toggle(type_):
    flag = bool(_json().get("disabled"))
    plugins.set_disabled(type_, flag)
    _audit(type_, "tool_disabled" if flag else "tool_enabled", "")
    return jsonify({"ok": True, "disabled": flag})


@bp.route("/api/plugins/<type_>", methods=["DELETE"])
@require_perm("tools.manage")
def api_plugin_delete(type_):
    try:
        if deploy.is_docker_tool(type_):
            deploy.stop_tool(type_)   # nettoie le conteneur avant suppression du dossier
    except Exception:
        pass
    try:
        plugins.delete_plugin(type_)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    _audit(type_, "tool_delete", "")
    return jsonify({"ok": True})


@bp.route("/api/plugins/<type_>/activate", methods=["POST"])
@require_perm("tools.manage")
def api_plugin_activate(type_):
    version = _json().get("version")
    try:
        res = plugins.activate_version(type_, version)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    _audit(type_, "version_activate", version or "")
    return jsonify(res)


@bp.route("/api/plugins/<type_>/export")
@require_perm("tools.manage")
def api_plugin_export(type_):
    version = request.args.get("version")
    if version:
        src, ver = plugins.export_version_dir(type_, version)
    else:
        src, ver = plugins.export_dir(type_), (plugins.get(type_) or {}).get("version")
    if not src:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(src):
            # Export d'une version courante : on n'embarque pas le sous-dossier versions/.
            if not version and os.path.basename(root) == "versions":
                _dirs[:] = []
                continue
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, src))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{type_}-{ver}.bobitool")


@bp.route("/api/plugins/import", methods=["POST"])
@require_perm("tools.manage")
def api_plugin_import():
    f = request.files.get("package")
    if not f or not f.filename:
        return jsonify({"error": "aucun fichier"}), 400
    activate = request.form.get("activate", "true").lower() != "false"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(f.stream) as z:
                z.extractall(tmp)
        except zipfile.BadZipFile:
            return jsonify({"error": "archive .bobitool invalide"}), 400
        src = _find_manifest_dir(tmp)
        if not src:
            return jsonify({"error": "plugin.json introuvable dans l'archive"}), 400
        man, err = plugins.validate_package(src)
        if err:
            return jsonify({"error": err}), 400
        plugins.stamp_imported_at(src)
        try:
            res = plugins.install_package(src, activate=activate)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    _audit(res["type"], "tool_import", f"v{res['version']}")
    return jsonify(res)


def _find_manifest_dir(root, manifest="plugin.json"):
    """Trouve le dossier contenant `manifest` (racine de l'archive ou 1 niveau dessous)."""
    if os.path.isfile(os.path.join(root, manifest)):
        return root
    for n in sorted(os.listdir(root)):
        d = os.path.join(root, n)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, manifest)):
            return d
    return None


# ─── Gestion des services globaux (miroir de /api/plugins/*) ─

@bp.route("/api/services")
@require_perm("settings.edit")
def api_services():
    out = []
    for m in core_plugins.all():
        sid = m["id"]
        out.append({
            "id": sid, "label": m.get("label", sid),
            "version": m.get("version"),
            "description": m.get("description", ""),
            "disabled": core_plugins.is_disabled(sid),
            "versions": core_plugins.versions(sid),
        })
    out.sort(key=lambda x: x["id"])
    return jsonify({"services": out, "scan_errors": core_plugins.scan_errors()})


@bp.route("/api/services/<sid>/toggle", methods=["POST"])
@require_perm("settings.edit")
def api_service_toggle(sid):
    flag = bool(_json().get("disabled"))
    core_plugins.set_disabled(sid, flag)
    _audit(sid, "service_disabled" if flag else "service_enabled", "")
    return jsonify({"ok": True, "disabled": flag})


@bp.route("/api/services/<sid>", methods=["DELETE"])
@require_perm("settings.edit")
def api_service_delete(sid):
    try:
        core_plugins.delete_service(sid)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    _audit(sid, "service_delete", "")
    return jsonify({"ok": True})


@bp.route("/api/services/<sid>/activate", methods=["POST"])
@require_perm("settings.edit")
def api_service_activate(sid):
    version = _json().get("version")
    try:
        res = core_plugins.activate_version(sid, version)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    _audit(sid, "service_version_activate", version or "")
    return jsonify(res)


@bp.route("/api/services/<sid>/export")
@require_perm("settings.edit")
def api_service_export(sid):
    version = request.args.get("version")
    if version:
        src, ver = core_plugins.export_version_dir(sid, version)
    else:
        src, ver = core_plugins.export_dir(sid), (core_plugins.get(sid) or {}).get("version")
    if not src:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(src):
            # Export d'une version courante : on n'embarque pas le sous-dossier versions/.
            if not version and os.path.basename(root) == "versions":
                _dirs[:] = []
                continue
            if "__pycache__" in _dirs:
                _dirs.remove("__pycache__")
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, src))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{sid}-{ver}.bobitool")


@bp.route("/api/services/import", methods=["POST"])
@require_perm("settings.edit")
def api_service_import():
    f = request.files.get("package")
    if not f or not f.filename:
        return jsonify({"error": "aucun fichier"}), 400
    activate = request.form.get("activate", "true").lower() != "false"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(f.stream) as z:
                z.extractall(tmp)
        except zipfile.BadZipFile:
            return jsonify({"error": "archive .bobitool invalide"}), 400
        src = _find_manifest_dir(tmp, "manifest.json")
        if not src:
            return jsonify({"error": "manifest.json introuvable dans l'archive"}), 400
        man, err = core_plugins.validate_package(src)
        if err:
            return jsonify({"error": err}), 400
        core_plugins.stamp_imported_at(src)
        try:
            res = core_plugins.install_package(src, activate=activate)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    _audit(res["id"], "service_import", f"v{res['version']}")
    return jsonify(res)


# ─── Déploiement (builder) + mise à jour inter-instances ─────
# Modèle repris de Bobi.Studio (onglet Déploiement → Flotte) :
#   · chaque instance a SON token (settings.update_token) et un MODE SERVEUR
#     (update_server_enabled) ; les endpoints serveur (manifest/download/apply) exigent
#     les deux. `ping` est public : il ne livre qu'une identité, et la découverte en dépend.
#   · le registre des pairs (app/peers.py) mémorise le token de CHAQUE pair ; vide → on
#     présente le nôtre (flotte à token partagé, le modèle d'avant).
#   · Endpoints CLIENT (peers, build, rollback…) : session settings.edit.
def _update_server_on():
    v = st.get("update_server_enabled")
    return bool(st.get("update_token")) if v is None else bool(v)


def _require_update_token():
    """Vrai si le mode serveur est actif et que l'en-tête porte NOTRE token. Refuse si aucun
    token n'est défini (endpoint serveur fermé par défaut)."""
    if not _update_server_on():
        return False
    want = st.get("update_token") or ""
    got = request.headers.get(updater.UPDATE_TOKEN_HEADER, "")
    # `compare_digest` et non `==` : ces endpoints déclenchent l'application d'une archive
    # sur l'instance, sans session ni limite de débit. Une comparaison qui s'arrête au
    # premier octet différent laisse deviner le jeton octet par octet en mesurant le temps
    # de réponse — et le gain, ici, c'est l'exécution de code sur toute la flotte.
    return bool(want) and hmac.compare_digest(str(got), str(want))


def _my_identity():
    """Identité publique de l'instance (aucun secret) : nom, version, dernier déploiement,
    et si elle suit un dépôt git — une mise à jour par pair l'en ferait sortir."""
    info = builder.identity()
    return {"name": st.get("brand_system_name") or st.get("brand_org_name") or "Bobi.Tools",
            "label": info.get("label"), "build_id": info.get("build_id"),
            "git_hash": info.get("git_hash"),
            "deployed_at": updater.deploy_info().get("deployed_at"),
            "git_checkout": os.path.exists(os.path.join(builder.ROOT, ".git"))}


@bp.route("/api/update/ping")
def api_update_ping():
    """Identité légère pour la découverte réseau — pas de code exposé, donc pas de token."""
    return jsonify(_my_identity())


@bp.route("/api/update/manifest")
def api_update_manifest():
    if not _require_update_token():
        abort(403)
    return jsonify(updater.current_manifest())


@bp.route("/api/update/download")
def api_update_download():
    if not _require_update_token():
        abort(403)
    updater.ensure_build()
    if not os.path.isfile(updater.ZIP_PATH):
        abort(404)
    return send_file(updater.ZIP_PATH, mimetype="application/zip",
                     as_attachment=True, download_name="bobitools-update.zip")


@bp.route("/api/update/apply", methods=["POST"])
@bp.route("/api/update/trigger", methods=["POST"])   # ancien nom (push d'avant la Flotte)
def api_update_apply():
    """Se met à jour DEPUIS `source_url`. Appelé par un pair (push : il nous demande de tirer
    chez lui, avec SON token dans le corps) ou par une session settings.edit.
    Sans token dans le corps, on présente le nôtre (flotte à token partagé)."""
    if not (_require_update_token() or has_perm("settings.edit")):
        abort(403)
    data = _json()
    source_url = (data.get("source_url") or "").strip()
    if not source_url:
        return jsonify({"error": "source_url manquant"}), 400
    token = data.get("token") or st.get("update_token") or ""
    _audit("system", "update_apply", source_url)
    ok, msg = updater.apply_update(source_url, token, install_new=data.get("install_new"))
    return (jsonify({"ok": True, "message": msg}) if ok
            else (jsonify({"error": msg}), 502))


@bp.route("/api/update/self")
@require_perm("settings.edit")
def api_update_self():
    """État local pour l'onglet Flotte : identité, mode serveur, token, sauvegarde dispo."""
    bk = updater.latest_backup()
    return jsonify({
        "identity":       _my_identity(),
        "server_enabled": _update_server_on(),
        "token":          st.get("update_token") or "",
        "has_backup":     bool(bk),
        "backup":         os.path.basename(bk) if bk else None,
        "pending":        updater.pending_build_id(),
    })


@bp.route("/api/update/server", methods=["POST"])
@require_perm("settings.edit")
def api_update_server():
    """Active/désactive le mode serveur, (re)génère OU pose explicitement le token. Le poser
    à la main sert à aligner une flotte sur un même jeton."""
    data = _json()
    if "enabled" in data:
        st.set("update_server_enabled", bool(data.get("enabled")))
    if data.get("regen_token"):
        st.set("update_token", secrets.token_urlsafe(24))
    elif "token" in data:
        tok = str(data.get("token") or "").strip()
        if tok and len(tok) < 16:
            return jsonify({"error": "token trop court (16 caractères minimum)"}), 400
        st.set("update_token", tok)
    elif data.get("enabled") and not st.get("update_token"):
        st.set("update_token", secrets.token_urlsafe(24))
    _audit("system", "update_server", "on" if _update_server_on() else "off")
    return jsonify({"ok": True, "token": st.get("update_token") or "",
                    "server_enabled": _update_server_on()})


# ─── Flotte : registre des pairs + pull/push avec aperçu ────

def _peer_token(p):
    return (p.get("token") or "").strip() or (st.get("update_token") or "")


def _peer_public(p):
    """Un pair tel que l'UI le voit : jamais son token en clair (on dit seulement s'il y en a un)."""
    q = dict(p)
    q["has_token"] = bool((q.pop("token", "") or "").strip())
    return q


@bp.route("/api/peers", methods=["GET", "POST"])
@require_perm("settings.edit")
def api_peers():
    from app import peers
    if request.method == "POST":
        data = _json()
        try:
            p = peers.add_peer(data.get("name"), data.get("url"), data.get("token"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit("system", "peer_add", p["url"] if p else "")
        return jsonify({"ok": True, "peer": _peer_public(p)})
    return jsonify({"peers": [_peer_public(p) for p in peers.list_peers()],
                    "self": _my_identity(), "default_cidr": peers.default_cidr()})


@bp.route("/api/peers/<int:pid>", methods=["PATCH", "DELETE"])
@require_perm("settings.edit")
def api_peer_item(pid):
    from app import peers
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"error": "pair inconnu"}), 404
    if request.method == "DELETE":
        peers.delete_peer(pid)
        _audit("system", "peer_delete", p["url"])
        return jsonify({"ok": True})
    data = _json()
    q = peers.update_peer(pid, name=data.get("name"), token=data.get("token"))
    _audit("system", "peer_update", p["url"])
    return jsonify({"ok": True, "peer": _peer_public(q)})


@bp.route("/api/peers/refresh", methods=["POST"])
@require_perm("settings.edit")
def api_peers_refresh():
    from app import peers
    return jsonify({"peers": [_peer_public(p) for p in peers.refresh_all()]})


@bp.route("/api/peers/discover", methods=["POST"])
@require_perm("settings.edit")
def api_peers_discover():
    from app import peers
    cidr = (_json().get("cidr") or "").strip() or peers.default_cidr()
    if not cidr:
        return jsonify({"error": "réseau à scanner indéterminé — préciser une plage (CIDR)"}), 400
    try:
        found = peers.discover(cidr)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "cidr": cidr, "found": found})


@bp.route("/api/peers/<int:pid>/preview")
@require_perm("settings.edit")
def api_peer_preview(pid):
    """Aperçu (lecture seule) de ce que ferait un Pull/Push : versions source → cible,
    composants mis à jour/ajoutés/retirés. N'applique rien."""
    from app import peers
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"error": "pair inconnu"}), 404
    direction = request.args.get("direction", "pull")
    if direction not in ("pull", "push"):
        return jsonify({"error": "direction invalide"}), 400

    warning = reason = None
    can_apply = True
    local = updater.current_manifest()
    me = _my_identity()
    try:
        remote = updater.fetch_manifest(p["url"], _peer_token(p))
    except Exception as e:
        remote = {}
        warning = f"pair injoignable ou token refusé : {e}"
    try:
        remote_id = updater.ping(p["url"])
    except Exception:
        remote_id = {}

    if direction == "pull":       # on tire le pair → CETTE instance redémarre
        old, new = local, remote
        target = {"label": local.get("label") or "?", "url": request.host_url.rstrip("/"),
                  "is_local": True, "git_checkout": me.get("git_checkout")}
        source = {"label": remote.get("label") or "?", "git_hash": remote.get("git_hash"),
                  "built_at": remote.get("built_at")}
        if warning:
            can_apply, reason = False, warning
    else:                         # on pousse vers le pair → le PAIR redémarre
        old, new = remote, local
        target = {"label": remote.get("label") or "?", "url": p["url"], "is_local": False,
                  "git_checkout": remote_id.get("git_checkout")}
        source = {"label": local.get("label") or "?", "git_hash": local.get("git_hash"),
                  "built_at": local.get("built_at")}
        if warning:
            # Un pair qui nous refuse son manifeste refusera aussi l'ordre de mise à jour :
            # inutile de laisser confirmer une opération qui échouera.
            can_apply, reason = False, warning
        elif not _update_server_on() or not st.get("update_token"):
            can_apply, reason = False, "activer d'abord le mode serveur (token) sur cette instance"

    # Pair injoignable → son manifeste est vide, et un diff serait trompeur (tout « ajouté »
    # ou « retiré ») : on ne le calcule que si les deux côtés sont là.
    if warning:
        diff = {"components": [], "counts": {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}}
    else:
        diff = updater.diff_manifests(old, new)
    return jsonify({
        "ok": True, "direction": direction, "target": target, "source": source,
        "same_build": bool(old.get("build_id") and old.get("build_id") == new.get("build_id")),
        "components": diff["components"], "counts": diff["counts"],
        "package": {"sha256": new.get("sha256"), "size": new.get("size")},
        "warning": warning, "can_apply": can_apply, "reason": reason,
    })


@bp.route("/api/peers/<int:pid>/pull", methods=["POST"])
@require_perm("settings.edit")
def api_peer_pull(pid):
    """Met à jour CETTE instance depuis le pair."""
    from app import peers
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"error": "pair inconnu"}), 404
    _audit("system", "update_pull", p["url"])
    ok, msg = updater.apply_update(p["url"], _peer_token(p),
                                   install_new=_json().get("install_new"))
    return (jsonify({"ok": True, "message": msg}) if ok
            else (jsonify({"error": msg}), 502))


@bp.route("/api/peers/<int:pid>/push", methods=["POST"])
@require_perm("settings.edit")
def api_peer_push(pid):
    """Met à jour le PAIR depuis cette instance : on lui demande de tirer chez nous, avec
    NOTRE token (qu'il présentera à notre /manifest et /download)."""
    from app import peers
    p = peers.get_peer(pid)
    if not p:
        return jsonify({"error": "pair inconnu"}), 404
    if not _update_server_on() or not st.get("update_token"):
        return jsonify({"error": "activer d'abord le mode serveur (token) sur cette instance"}), 400
    try:
        r = requests.post(p["url"].rstrip("/") + "/api/update/apply",
                          json={"source_url": request.host_url.rstrip("/"),
                                "token": st.get("update_token"),
                                "install_new": _json().get("install_new")},
                          headers={updater.UPDATE_TOKEN_HEADER: _peer_token(p)}, timeout=330)
    except Exception as e:
        return jsonify({"error": f"pair injoignable : {e}"}), 502
    _audit("system", "update_push", p["url"])
    if r.status_code == 403:
        return jsonify({"error": "le pair refuse : mode serveur inactif chez lui, ou token erroné"}), 502
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not r.ok:
        return jsonify({"error": body.get("error") or f"HTTP {r.status_code}"}), 502
    return jsonify({"ok": True, "message": body.get("message") or ""})


@bp.route("/api/deploy/info")
@require_perm("settings.edit")
def api_deploy_info():
    """État local pour l'onglet Déploiement : identité de build, dernière sélection,
    plugins/services disponibles, dernier déploiement appliqué."""
    dist = None
    if os.path.isfile(builder.DEFAULT_DEST):
        dist = {"name": os.path.basename(builder.DEFAULT_DEST),
                "size": os.path.getsize(builder.DEFAULT_DEST)}
    return jsonify({
        "build": builder.current_build_info(),
        "available": builder.available(),
        "last_selection": builder.last_selection(),
        "deploy": updater.deploy_info(),
        "token_set": bool(st.get("update_token")),
        "dist": dist,
        "has_backup": bool(updater.latest_backup()),
    })


@bp.route("/api/deploy/build", methods=["POST"])
@require_perm("settings.edit")
def api_deploy_build():
    """Construit le zip de distribution (sélection optionnelle plugins/services)."""
    data = _json() or {}
    try:
        res = builder.build(plugins=data.get("plugins"), services=data.get("services"),
                            stamp=bool(data.get("stamp", True)))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    _audit("system", "deploy_build", res.get("label") or "")
    return jsonify(res)


@bp.route("/api/deploy/download")
@require_perm("settings.edit")
def api_deploy_download():
    """Télécharge le dernier zip de distribution construit."""
    if not os.path.isfile(builder.DEFAULT_DEST):
        abort(404)
    return send_file(builder.DEFAULT_DEST, mimetype="application/zip",
                     as_attachment=True, download_name="bobitools.zip")


@bp.route("/api/deploy/import", methods=["POST"])
@require_perm("settings.edit")
def api_deploy_import():
    """Import HORS-LIGNE (airgap) : applique un paquet .zip téléversé (construit sur une
    autre instance). Aucun réseau requis — pensé pour les instances broadcast sans Internet."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "aucun fichier"}), 400
    fd, zpath = tempfile.mkstemp(prefix="btimp-", suffix=".zip")
    os.close(fd)
    try:
        f.save(zpath)
        _audit("system", "deploy_import", f.filename)
        ok, msg = updater.apply_zip(zpath)
        return (jsonify({"ok": True, "message": msg}) if ok
                else (jsonify({"error": msg}), 400))
    finally:
        try:
            os.remove(zpath)
        except OSError:
            pass


# ─── Images des outils Docker (installations hors ligne) ─────────────────────
@bp.route("/api/deploy/images")
@require_perm("settings.edit")
def api_deploy_images():
    """Image attendue par chaque outil Docker, sa présence locale, la version qui tourne."""
    return jsonify(deploy.images_inventory())


@bp.route("/api/deploy/images/<type_>/build", methods=["POST"])
@require_perm("settings.edit")
def api_deploy_image_build(type_):
    """Construit l'image attendue, en arrière-plan (suivi par /api/deploy/images)."""
    try:
        res = deploy.build_in_background(type_)
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    _audit("system", "image_build", res.get("image", ""))
    return jsonify(res), 202


@bp.route("/api/deploy/images/<type_>/export")
@require_perm("settings.edit")
def api_deploy_image_export(type_):
    """Télécharge l'image attendue d'un outil (`docker save`, gzip), en flux."""
    try:
        tag = deploy.image_tag(type_)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    # Vérifié AVANT d'ouvrir le flux : une fois l'en-tête HTTP envoyé, l'échec ne se dit plus.
    if not deploy.dk.image_exists(tag):
        return jsonify({"error": "image %s absente : construisez-la d'abord." % tag}), 404
    _audit("system", "image_export", tag)
    nom = tag.replace(":", "-") + ".tar.gz"
    return Response(deploy.dk.save_stream(tag), mimetype="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % nom})


@bp.route("/api/deploy/images/import", methods=["POST"])
@require_perm("settings.edit")
def api_deploy_image_import():
    """Importe une archive d'image (exportée ici ou ailleurs). Aucun réseau requis."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "aucun fichier"}), 400
    fd, chemin = tempfile.mkstemp(prefix="btimg-", suffix=".tar")
    os.close(fd)
    try:
        f.save(chemin)
        try:
            res = deploy.import_archive(chemin)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except deploy.dk.DockerError as e:
            return jsonify({"error": "docker load : %s" % e}), 500
        _audit("system", "image_import", ", ".join(r["image"] for r in res))
        return jsonify({"ok": True, "images": res})
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass


# ─── Mise à jour depuis GitHub (source unique) ───────────────────────────────
@bp.route("/api/git/status")
@require_perm("settings.edit")
def api_git_status():
    """État git de CETTE instance ; `?fetch=1` interroge d'abord origin (réseau)."""
    return jsonify(gitupdate.status(fetch=(request.args.get("fetch") == "1")))


@bp.route("/api/git/update", methods=["POST"])
@require_perm("settings.edit")
def api_git_update():
    """Fast-forward depuis origin/<branche> + submodules, puis redémarrage."""
    ok, msg = gitupdate.update()
    _audit("system", "git_update", msg)
    return (jsonify({"ok": True, "message": msg}) if ok
            else (jsonify({"error": msg}), 400))


@bp.route("/api/git/rollback", methods=["POST"])
@require_perm("settings.edit")
def api_git_rollback():
    """Revient au commit d'avant la dernière mise à jour git."""
    ok, msg = gitupdate.rollback()
    _audit("system", "git_rollback", msg)
    return (jsonify({"ok": True, "message": msg}) if ok
            else (jsonify({"error": msg}), 400))


@bp.route("/api/update/rollback", methods=["POST"])
@require_perm("settings.edit")
def api_update_rollback():
    """Restaure le dernier backup de code et redémarre."""
    ok, msg = updater.rollback()
    _audit("system", "update_rollback", msg)
    return (jsonify({"ok": True, "message": msg}) if ok
            else (jsonify({"error": msg}), 400))
