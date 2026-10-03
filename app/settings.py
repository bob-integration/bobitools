# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Settings accessor : DB d'abord (table `settings`), puis fallback sur les DEFAULTS
ci-dessous. Permet d'éditer la config via l'UI sans toucher au code. Dérivé de
l'accesseur de Bobi.Studio (defaults métier ST 2110/Proxmox retirés).
"""
from .database import db_get_setting, db_set_setting, db_get_all_settings


# ─── Defaults ────────────────────────────────────────────────
DEFAULTS = {
    # Premier démarrage : faux tant que l'assistant n'a pas été terminé/sauté.
    "setup_completed":  False,

    # Apparence
    "theme":            "classic",   # classic | studio | light
    # Langue d'interface par défaut (i18n) — repli quand l'utilisateur n'a pas de
    # préférence propre (users.lang). Codes : voir app/i18n.LANGUAGES.
    "ui_lang_default":  "fr",
    # Langues personnalisées créées via l'éditeur de traductions (i18n).
    "ui_custom_languages": [],

    # Personnalisation client (identité du déploiement, EN PLUS de la marque produit
    # « Bobi.Tools » qui reste affichée partout) : nom du système, entreprise, logo, lieu.
    "brand_system_name": "",
    "brand_org_name":    "",
    "brand_logo_url":    "",
    "brand_location":    "",

    # Contrôle d'accès par outil : { "<type>": {"roles": [...], "users": [uid,...]} }.
    # Absent pour un outil → rôles par défaut (cf. auth.DEFAULT_TOOL_ROLES).
    "tool_access":       {},

    # Outils désactivés (politique orchestrateur) : liste de types. Un outil désactivé
    # reste installé mais n'apparaît plus au lanceur / dans la nav.
    "tools_disabled":    [],

    # Nombre de reverse-proxys de confiance DEVANT l'application. 0 = aucun : l'adresse
    # du client est celle de la connexion TCP, et X-Forwarded-For est ignoré. À passer à 1
    # (ou plus) UNIQUEMENT si l'app est réellement servie derrière un proxy, sans quoi
    # n'importe quel client choisit l'adresse sous laquelle il est compté — et le blocage
    # anti-bourrinage du login, indexé dessus, ne se déclenche jamais. Lu au démarrage.
    "trusted_proxies":   0,

    # Sauvegarde quotidienne automatisée de la DB (vers backups/)
    "backup_enabled":   False,
    "backup_time":      "02:00",     # heure locale serveur HH:MM
    "backup_retention": 14,          # nombre de sauvegardes conservées
    # État runtime (écrit par le scheduler, lu par l'UI)
    "backup_last_date":   "",        # YYYY-MM-DD du dernier backup réussi (anti double-run)
    "backup_last_status": "",        # message du dernier run
    "backup_last_file":   "",        # nom du dernier fichier produit

    # Services globaux désactivés (politique orchestrateur) : liste d'ids. Un service
    # désactivé reste installé mais n'est ni booté ni proposé. Cf. app/core_plugins.py.
    "services_disabled":  [],

    # Déploiement / mise à jour inter-instances (cf. app/updater.py). Token PARTAGÉ entre
    # instances : vide → les endpoints serveur /api/update/{manifest,download,ping} sont
    # fermés (refus). À définir des deux côtés pour autoriser pull/push.
    "update_token":       "",
    # Mode serveur : cette instance accepte-t-elle de servir son code (manifest/download) et
    # d'être mise à jour par un pair (apply) ? None = jamais réglé → actif si un token
    # existe (comportement d'avant l'onglet Flotte, pour ne pas fermer une flotte en place).
    "update_server_enabled": None,

    # NB : les réglages PROPRES à un service (ex. emberplus_enabled / emberplus_port) ne
    # sont plus codés ici : ils sont déclarés dans services/<id>/manifest.json (settings_keys)
    # et fusionnés à la volée par _service_defaults() — le service est autoritaire pour SES
    # réglages (séparation cœur/services).
}


def _service_defaults():
    """Defaults déclarés par les services (manifest.settings_keys). Import paresseux :
    settings est chargé tôt, alors que core_plugins → services dépendent de app.settings."""
    try:
        from . import core_plugins
        return core_plugins.all_settings_defaults()
    except Exception:
        return {}


def _tool_config_defaults():
    """Defaults des réglages exposés par les outils (`config_schema` du manifeste), sous
    forme de clés préfixées `<type>__<clé>`. Import paresseux (plugins → settings)."""
    try:
        from . import plugins
        out = {}
        for t, schema in plugins.config_schemas().items():
            for f in schema or []:
                k = f.get("key")
                if k:
                    out[f"{t}__{k}"] = f.get("default")
        return out
    except Exception:
        return {}

# Thèmes connus + description (pour le sélecteur UI). Mêmes thèmes que Bobi.Studio.
THEMES = [
    {"id": "classic", "label": "Classic — terminal dark (par défaut)"},
    {"id": "studio",  "label": "Studio — broadcast pro, accent amber"},
    {"id": "light",   "label": "Daylight — clean light, accent indigo"},
]


def get(key):
    val = db_get_setting(key, None)
    if val is not None:
        return val
    if key in DEFAULTS:
        return DEFAULTS[key]
    svc = _service_defaults()
    if key in svc:
        return svc[key]
    return _tool_config_defaults().get(key)


def set(key, value):
    db_set_setting(key, value)


def all(include_unknown=True):
    """Tous les settings (DB mergée sur DEFAULTS + defaults des services + réglages outils).

    `include_unknown=False` restreint aux clés DÉCLARÉES (cœur, service, outil), ce que
    fait déjà `update_bulk` en écriture. C'est ce qu'il faut servir à l'extérieur : la
    table `settings` porte aussi des valeurs qui ne sont réglages de personne — au premier
    rang desquelles `flask_secret_key`, qui signe les cookies de session (cf. main.py).
    La rendre revient à distribuer de quoi forger une session d'administrateur."""
    merged = dict(_service_defaults())
    merged.update(_tool_config_defaults())
    merged.update(DEFAULTS)
    stored = db_get_all_settings()
    if not include_unknown:
        known = {*DEFAULTS, *_service_defaults(), *_tool_config_defaults()}
        stored = {k: v for k, v in stored.items() if k in known}
    merged.update(stored)
    return merged


# Réglages déclarés mais dont la VALEUR ne doit pas sortir : on ne rend qu'un booléen
# `<clé>_set`, suffisant pour que l'écran affiche « défini » sans transporter le secret.
def _is_secret_key(key):
    k = key.lower()
    return any(s in k for s in ("password", "token", "secret", "api_key", "apikey",
                                "passwd", "credential"))


# Ce que voit un formulaire à la place d'un secret déjà défini. Le renvoyer tel quel à
# l'enregistrement ne change rien (cf. update_bulk) : un écran qui réaffiche puis
# ré-enregistre un réglage ne doit pas effacer le mot de passe qu'il n'a jamais reçu.
SECRET_PLACEHOLDER = "••••••••"


def public():
    """Réglages destinés à l'extérieur : clés déclarées uniquement, valeurs sensibles
    remplacées par un masque + un booléen `<clé>_set`. L'écran de réglages procédait déjà
    ainsi pour le mot de passe SMTP — ce n'était qu'une précaution locale, appliquée à une
    seule clé, alors que la route en servait des dizaines d'autres en clair."""
    out = {}
    for k, v in all(include_unknown=False).items():
        if _is_secret_key(k):
            out[k] = SECRET_PLACEHOLDER if v else ""
            out[f"{k}_set"] = bool(v)
        else:
            out[k] = v
    return out


def update_bulk(items):
    """items: dict { key: value }. Ne stocke que les clés connues (cœur, service ou outil).
    NB : on construit l'ensemble via une littérale `{*...}` et NON `set(...)` — le nom
    `set` est ici la fonction module (cf. plus haut) qui masque le builtin."""
    known = {*DEFAULTS, *_service_defaults(), *_tool_config_defaults()}
    accepted = 0
    for k, v in items.items():
        if k not in known:
            continue
        if _is_secret_key(k):
            # Un secret ne s'efface pas par distraction : le masque et la chaîne vide
            # signifient « inchangé », car c'est ce qu'un formulaire renvoie quand il n'a
            # jamais reçu la valeur. Pour effacer, il faut l'écrire explicitement à `null`.
            if v is None:
                db_set_setting(k, "")
                accepted += 1
            elif v == "" or v == SECRET_PLACEHOLDER:
                continue
            else:
                db_set_setting(k, v)
                accepted += 1
            continue
        db_set_setting(k, v)
        accepted += 1
    return accepted
