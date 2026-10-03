# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Point d'entrée de Bobi.Tools : application Flask (port 5000) + un thread de fond léger
pour la sauvegarde quotidienne. Pas de surveillance d'infra broadcast (différence avec
Bobi.Studio) — les outils `runtime=docker` sont pilotés à la demande depuis l'UI.
"""
import json
import logging
import logging.handlers
import os
import secrets
import sys
import threading
import time
from datetime import timedelta

from flask import Flask
from jinja2 import ChoiceLoader, FileSystemLoader

from app.config import LOG_PATH, HTTP_PORT, SESSION_COOKIE_SECURE
from app.database import init_db, db_get_setting, db_set_setting
from app import backup
from app.routes import bp

# Journal applicatif : fichier TOURNANT (10 × 20 Mo). Il grossissait sans limite — 198 Mo le
# 2026-09-19, avant même la production, où les tallys du contrôleur en écrivent en continu.
# La console n'est ajoutée que dans un terminal : sous systemd, stderr était recopié DANS CE
# MÊME FICHIER (`StandardError=append:`), si bien que chaque ligne y apparaissait deux fois.
_log_handlers = [logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")]
if sys.stderr is not None and sys.stderr.isatty():
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
# Les outils peuvent fournir des fragments servis via /api/tools/<type>/ui ; les
# templates cœur vivent dans templates/. (Les plugins n'utilisent pas le loader Jinja.)
app.jinja_loader = ChoiceLoader([FileSystemLoader("templates")])
app.jinja_env.filters["from_json"] = lambda s: json.loads(s) if s else None


def _asset_v(path):
    """Cache-buster pour les assets statiques (mtime). `path` relatif au dossier static."""
    try:
        return int(os.path.getmtime(os.path.join("static", path)))
    except OSError:
        return 0


app.jinja_env.globals["asset_v"] = _asset_v

# i18n : globals Jinja t() / _() (référencent des CLÉS, cf. app/i18n.py)
from app import i18n as _i18n
app.jinja_env.globals["t"] = _i18n.t
app.jinja_env.globals["_"] = _i18n.t


@app.context_processor
def inject_i18n():
    lang = _i18n.current_lang()
    return {"lang": lang, "languages": _i18n.LANGUAGES,
            "js_catalog": _i18n.js_catalog(lang)}


# Secret key persistée en DB (générée au premier démarrage)
def _ensure_secret_key():
    init_db()
    raw = db_get_setting("flask_secret_key", None)
    if not raw:
        raw = secrets.token_hex(32)
        db_set_setting("flask_secret_key", raw)
    return raw


app.secret_key = _ensure_secret_key()
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(SESSION_COOKIE_SECURE)
# Le reverse-proxy doit transmettre X-Forwarded-Proto pour que Flask sache que c'est HTTPS.
#
# `x_for` est EXPLICITE, et vaut 0 par défaut. Werkzeug le met à 1 quand on ne dit rien :
# REMOTE_ADDR est alors écrasé par X-Forwarded-For, un en-tête que n'importe quel client
# peut écrire, sans aucune notion de proxy de confiance. Le compteur d'échecs de connexion
# étant indexé sur cette adresse, il suffisait de la faire varier à chaque tentative pour
# que le blocage ne se déclenche jamais. Une instance servie en direct — le cas ici, port
# 5000 sur 0.0.0.0 — ne doit croire personne sur parole ; derrière un reverse-proxy, régler
# `trusted_proxies` au NOMBRE de proxys traversés (généralement 1).
from werkzeug.middleware.proxy_fix import ProxyFix
from app import settings as _st
_trusted = 0
try:
    _trusted = max(0, min(4, int(_st.get("trusted_proxies") or 0)))
except (TypeError, ValueError):
    _trusted = 0
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trusted, x_proto=1, x_host=1)
log.info("ProxyFix: x_for=%d (réglage trusted_proxies)", _trusted)

# Routes propres aux services globaux (montées AVANT l'enregistrement du blueprint :
# Flask interdit d'ajouter des routes à un blueprint déjà enregistré).
from app import core_plugins
core_plugins.register_all_routes(bp)

app.register_blueprint(bp)

# Rôles modifiables : la table `roles` fait foi (semée au premier démarrage), et les
# permissions déclarées par les plugins rejoignent le catalogue.
from app.auth import recharger_roles  # noqa: E402
recharger_roles()


@app.context_processor
def inject_theme():
    """Thème effectif : préférence UTILISATEUR → défaut du système → 'classic'.

    Même cascade que la langue (`i18n.current_lang`), comme dans Bobi.Studio. Le setting
    global `theme` n'est plus le thème de tout le monde : c'est le défaut servi à qui n'a
    rien choisi (et aux pages hors session, login compris). `themes` alimente le sélecteur
    du menu utilisateur et de la page « Mon compte »."""
    from app import settings as stt
    valid = {t["id"] for t in stt.THEMES}
    theme = None
    try:
        from app.auth import current_user
        u = current_user()
        if u and u.get("theme") in valid:
            theme = u["theme"]
    except Exception:
        pass
    if not theme:
        theme = stt.get("theme") or "classic"
    return {"theme": theme if theme in valid else "classic", "themes": stt.THEMES}


@app.context_processor
def inject_version():
    """Version affichée en tête du menu « ? » : l'identité de build (date · hash git).
    Bobi.Tools n'a pas de numéro de version applicatif — ses composants si. Sur un dépôt
    git, builder.identity() la tient alignée sur HEAD (et la met en cache)."""
    from app import builder
    try:
        return {"app_version": builder.identity().get("label") or ""}
    except Exception:
        return {"app_version": ""}


@app.context_processor
def inject_brand():
    """Identité client (personnalisation), EN PLUS de la marque produit « Bobi.Tools »."""
    from app import settings as stt
    return {"brand": {
        "system_name": stt.get("brand_system_name") or "",
        "org_name":    stt.get("brand_org_name") or "",
        "logo_url":    stt.get("brand_logo_url") or "",
        "location":    stt.get("brand_location") or "",
    }}


@app.context_processor
def inject_auth():
    from app.auth import current_user, has_perm, current_permissions, all_permissions
    u = current_user()
    mine = (all_permissions() if u and u.get("role") == "admin"
            else sorted(current_permissions()))
    return {"current_user": u, "has_perm": has_perm, "my_perms": list(mine)}


@app.context_processor
def inject_plugins():
    """Sections de nav contribuées par les outils installés (filtrées par accès) +
    résumé des manifestes pour window.BT_TOOLS et le bloc <style> des badges."""
    from app import plugins
    from app.auth import current_user
    return {"plugin_sections": plugins.sections(user=current_user()),
            "bt_tools_json": plugins.manifest_summary_for_js(),
            "badge_css": plugins.badge_css_vars()}


_derniere_purge = [0.0]


def _purge_journal():
    """Purge quotidienne du journal d'audit, selon `audit_retention_days` (10 jours par défaut).

    Pourquoi c'est ici : le journal est écrit par le cœur à chaque appel non-GET, et rien ne
    l'a jamais élagué. Relevé le 2026-09-24 : 98 701 lignes, dont 88 473 pour la seule couche
    IPG et des entrées remontant à plus d'un an — 39 Mo sur les 48,7 de la base.

    C'est de l'ENTRETIEN, pas une réparation : il n'y a donc rien de discutable à le faire
    tout seul. La rétention se règle dans l'écran de l'outil « IPG Générique », onglet
    Journal → Réglages, et une purge immédiate y est offerte. Une rétention absente ou nulle
    ne purge RIEN plutôt que tout — c'est le sens de `audit_purge`."""
    from app import settings
    from app.database import audit_purge, audit_stats_by_tool
    if time.time() - _derniere_purge[0] < 86400:
        return
    _derniere_purge[0] = time.time()

    def _jours(v, defaut):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return defaut
        return v if v > 0 else defaut

    defaut = _jours(settings.get("audit_retention_days"), 10)
    # Rétention PAR OUTIL : un outil bavard ne doit pas obliger à raccourcir la mémoire de
    # tous les autres. La couche IPG pesait 39 Mo sur les 48,7 de la base le 2026-09-24,
    # pendant que `swp08` tenait en 98 lignes — leur imposer le même horizon n'a pas de sens.
    par_outil = settings.get("audit_retention_by_tool")
    if isinstance(par_outil, str):
        try:
            par_outil = json.loads(par_outil or "{}")
        except ValueError:
            par_outil = {}
    par_outil = par_outil if isinstance(par_outil, dict) else {}
    total, detail = 0, []
    for ligne in audit_stats_by_tool():
        outil = ligne.get("tool")
        if not outil:
            continue
        j = _jours(par_outil.get(outil), defaut)
        n = audit_purge(j, tool=outil)
        if n:
            total += n
            detail.append("%s %d (>%dj)" % (outil, n, j))
    # Les entrées SANS outil (rares : amorçage) suivent la rétention par défaut.
    total += audit_purge(defaut, tool="")
    if total:
        log.info("Journal d'audit : %d entrée(s) purgée(s) — %s", total, ", ".join(detail))


def _backup_loop():
    """Thread de fond : sauvegarde quotidienne (no-op si désactivée) + purge du journal."""
    while True:
        try:
            backup.maybe_daily_backup()
        except Exception as e:
            log.error("Erreur backup quotidien : %s", e)
        try:
            _purge_journal()
        except Exception as e:
            log.error("Erreur purge du journal : %s", e)
        time.sleep(300)


if __name__ == "__main__":
    init_db()
    # Si une mise à jour était en attente de validation, le fait de redémarrer confirme
    # qu'elle a réussi (cf. app/updater.py).
    from app import updater
    updater.confirm_boot_ok()
    threading.Thread(target=_backup_loop, daemon=True).start()
    # Services globaux (≠ outils-plugins) : démarrage générique depuis le registre
    # (chaque service expose un boot() optionnel). Configurés en Réglages.
    core_plugins.boot_all()
    log.info("Bobi.Tools démarré sur http://0.0.0.0:%s", HTTP_PORT)
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False)
