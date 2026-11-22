# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Mises à jour du CŒUR depuis les releases GitHub, et veille — routes reprises de Bobi.Studio
(`app/routes/updates.py`). Module à part, branché sur le Blueprint unique par `main.py`.

★ CE CHEMIN N'EXISTAIT PAS. Le cœur se mettait à jour par git (Déploiement → GitHub) ou par un
zip importé à la main ; rien ne disait qu'une version était publiée, et une instance installée
depuis les archives de GitHub n'avait aucun moyen d'obtenir la suivante — alors que ses outils,
eux, se mettent à jour depuis le Catalogue.

⚠ `GET /api/update/core` INFORME, il n'applique pas ; `applicable` dit pourquoi un bouton serait
vain (release sans paquet vérifiable, ou instance suivie par git). `POST …/apply` REDÉMARRE le
service : il n'est donc pas automatique — sur une installation d'antenne, le moment se choisit.
"""
from flask import jsonify, request

from .auth import current_user, require_login, require_perm
from .database import audit_log
from .routes import bp


def _audit(action, detail):
    u = current_user() or {}
    audit_log("system", action, detail, user_id=u.get("id"), username=u.get("username"))


@bp.route("/api/update/core", methods=["GET"])
@require_login
def maj_core():
    """La dernière version publiée de Bobi.Tools, comparée à celle de cette instance.
    `?force=1` relit GitHub (bouton « Vérifier »)."""
    from . import catalogue, gitupdate
    from .version import VERSION
    force = (request.args.get("force") or "") in ("1", "true", "yes")
    dispo, info = catalogue.maj_core_disponible(force=force)
    git = gitupdate.is_git()
    if info is not None and git:
        info["applicable"] = False
    return jsonify({"version_installee": VERSION, "disponible": dispo, "derniere": info,
                    "git": git, "catalogue_actif": catalogue._reglages()[2]})


@bp.route("/api/update/core/apply", methods=["POST"])
@require_perm("settings.edit")
def maj_core_apply():
    """Tire la release publiée (la dernière, ou `tag`), vérifie son empreinte, applique, relance."""
    from . import updater
    data = request.get_json(silent=True) or {}
    ok, msg = updater.apply_update_github(tag=(data.get("tag") or None))
    _audit("core_update_github", msg)
    return jsonify({"ok": bool(ok), "message": msg}), (200 if ok else 400)


@bp.route("/api/update/notice", methods=["GET"])
@require_login
def maj_notice():
    """Ce que le bandeau doit montrer à l'utilisateur courant (rien s'il ne peut pas agir)."""
    from . import update_watch
    from .auth import has_perm
    u = current_user() or {}
    if not has_perm("settings.edit"):
        return jsonify({"enabled": False, "items": []})
    return jsonify(update_watch.notice_for(u.get("id")))


@bp.route("/api/update/notice/dismiss", methods=["POST"])
@require_perm("settings.edit")
def maj_notice_dismiss():
    from . import update_watch
    update_watch.dismiss((current_user() or {}).get("id"))
    return jsonify({"ok": True})


@bp.route("/api/update/watch", methods=["GET", "POST"])
@require_perm("settings.edit")
def maj_watch():
    """État et réglages de la veille ; POST {enabled?, interval_h?, scope?} les modifie."""
    from . import settings as st
    from . import update_watch
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            st.set("update_check_enabled", "1" if data.get("enabled") else "0")
        if "interval_h" in data:
            try:
                h = int(data.get("interval_h"))
            except (TypeError, ValueError):
                return jsonify({"error": "intervalle invalide"}), 400
            if h not in update_watch.INTERVALS_H:
                return jsonify({"error": "intervalle invalide"}), 400
            st.set("update_check_interval_h", h)
        if "scope" in data:
            if data.get("scope") not in update_watch.SCOPES:
                return jsonify({"error": "portée invalide"}), 400
            st.set("update_check_scope", data.get("scope"))
        _audit("update_watch_settings", str({k: data[k] for k in ("enabled", "interval_h", "scope") if k in data}))
    s = update_watch.state()
    return jsonify({"settings": update_watch._settings(), "intervals_h": list(update_watch.INTERVALS_H),
                    "checked_at": s.get("checked_at"), "error": s.get("error"),
                    "available": s.get("available") or []})


@bp.route("/api/update/watch/check", methods=["POST"])
@require_perm("settings.edit")
def maj_watch_check():
    """« Vérifier maintenant » — même passage que la veille, interrupteur ou pas."""
    from . import update_watch
    s = update_watch.check()
    return jsonify({"checked_at": s.get("checked_at"), "error": s.get("error"),
                    "available": s.get("available") or []})
