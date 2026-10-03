# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Appel d'un outil DEPUIS le serveur (in-process), sans contexte HTTP ni utilisateur.

Les services globaux (ex. le provider Ember+) doivent parler aux outils sans passer
par la route HTTP `/api/tools/<type>/…` (protégée par `@require_tool`, qui exige une
session). Ce helper reproduit le dispatch de `routes.api_tool_dispatch` — docker
(proxy HTTP) ou inprocess (backend.api) — mais renvoie toujours `(status, data)` JSON.

Réservé au code système de confiance : il CONTOURNE volontairement l'auth par outil.
Ne pas l'exposer comme route. La route HTTP reste l'unique porte d'entrée pour l'UI
(et conserve le passthrough binaire dont ce helper n'a pas besoin)."""
import logging
import threading

import requests

from . import deploy, plugins, settings
from .database import (audit_log, plugin_store_create, plugin_store_delete,
                       plugin_store_get, plugin_store_list, plugin_store_update)

log = logging.getLogger(__name__)


class _SystemStore:
    """Store scopé à l'outil, pour un appel système (cf. routes._build_ctx)."""
    def __init__(self, type_):
        self._type = type_

    # Toutes les opérations par id sont BORNÉES à l'outil : les identifiants de
    # `plugin_store` sont globaux, donc un id venu d'une URL peut désigner la ligne d'un
    # autre outil — deux plugins qui emploient le même nom de scope se marcheraient dessus
    # en silence (le cas s'est présenté entre « newt » et « cde1922 », tous deux en scope
    # « device »).
    def list(self, scope=""):   return plugin_store_list(self._type, scope)
    def get(self, sid):         return plugin_store_get(sid, type_=self._type)
    def create(self, name, value, scope="", unique_name=False):
        return plugin_store_create(self._type, scope, name, value, unique_name)
    def update(self, sid, name=None, value=None):
        return plugin_store_update(sid, name=name, value=value, type_=self._type)
    def delete(self, sid):      return plugin_store_delete(sid, type_=self._type)


_chain = threading.local()      # chaîne d'appels outil→outil du thread courant
MAX_CALL_DEPTH = 3

# Session HTTP partagée vers les conteneurs : `requests.request` ouvre — et referme — une
# connexion TCP À CHAQUE APPEL. Sur localhost c'est invisible pour une page web, mais pas pour
# le chemin du tally : mesuré le 2026-09-17, 89 ms d'écart entre l'appel proxifié (93 ms) et le
# même appel fait directement au conteneur (4 ms). Une session avec keep-alive supprime la
# poignée de main TCP de chaque requête. Thread-safe côté requests (un pool par adaptateur).
_http = requests.Session()
_http.mount("http://", requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=64))


def make_caller(type_, actor):
    """Construit le `ctx.call` de l'outil `type_` : appeler UN AUTRE OUTIL depuis un backend.

    Sert la composition d'outils — l'agrégateur IPG interroge ses convertisseurs plutôt que
    de reparler à leur matériel. Volontairement ÉTROIT, pour deux raisons :

    • `call()` contourne l'auth par outil (cf. l'en-tête de ce module). Tolérable pour du code
      système, PAS pour un plugin : sans garde, un outil ouvert à un opérateur serait un pont
      vers un outil qui ne l'est pas. L'autorisation est donc la liste blanche `calls` du
      `plugin.json` de l'APPELANT — statique, lisible dans le manifeste, vérifiable sans
      exécuter le code, et modifiable seulement par qui installe l'outil.
    • Deux outils qui se citent mutuellement boucleraient à l'infini, dans un thread de
      service qui tourne sans personne pour l'interrompre. D'où la chaîne par thread : un
      type déjà présent en amont est refusé, et la profondeur est bornée."""
    def _call(target, subpath, method="GET", payload=None, timeout=30):
        allowed = (plugins.get(type_) or {}).get("calls") or []
        if not isinstance(allowed, list) or target not in allowed:
            log.warning("tools: %s a tenté d'appeler %s, hors de sa liste `calls`", type_, target)
            return 403, {"error": "appel inter-outils non autorisé : %s → %s" % (type_, target)}
        chain = getattr(_chain, "types", None)
        if chain is None:
            chain = _chain.types = []
        if target in chain or type_ in chain:
            log.warning("tools: cycle d'appels %s → %s (chaîne %s)", type_, target, chain)
            return 508, {"error": "cycle d'appels inter-outils"}
        if len(chain) >= MAX_CALL_DEPTH:
            return 508, {"error": "chaîne d'appels trop profonde (%d)" % MAX_CALL_DEPTH}
        # Un outil DÉSACTIVÉ n'est plus interrogé par ses agrégateurs, comme le service Ember+
        # l'écarte déjà. Sans ce garde, un convertisseur débranché et désactivé continuait de
        # coûter son délai d'attente (~3 s) à chaque reconstruction de l'arbre Ember+ via
        # `ipg_generique`, et retardait d'autant chaque écriture du contrôleur (constaté le
        # 2026-09-16 avec cde1922 et newt non raccordés).
        if plugins.is_disabled(target):
            return 503, {"error": "outil désactivé : %s" % target}
        chain.append(type_)
        try:
            return call(target, subpath, method, payload, actor=actor, timeout=timeout)
        finally:
            chain.pop()
    return _call


def _system_ctx(type_, actor):
    """`ctx` d'un backend inprocess pour un appel système (pas d'utilisateur loggé).
    L'audit est attribué à `actor` (ex. « Service Ember+ »), user_id nul."""
    m = plugins.get(type_) or {}

    def _setting(key, default=None):
        v = settings.get(f"{type_}__{key}")
        if v is None:
            v = settings.get(key)
        return default if v is None else v

    def _audit_fn(action, detail=""):
        audit_log(type_, action, detail, user_id=None, username=actor)

    def _send_mail(subject, body, to=None, **kw):
        """Remet un e-mail au service `mail` (asynchrone, best-effort). Résolution
        paresseuse du service ; audit attribué à `actor`."""
        from . import core_plugins
        mod = core_plugins.module("mail")
        if not mod:
            return {"queued": False, "error": "service mail absent"}
        kw.setdefault("actor", actor)
        return mod.enqueue(subject, body, to=to, **kw)

    def _notify(message, **kw):
        """Pousse une notification via le service `ntfy` (asynchrone, best-effort). Résolution
        paresseuse du service ; audit attribué à `actor`."""
        from . import core_plugins
        mod = core_plugins.module("ntfy")
        if not mod:
            return {"queued": False, "error": "service ntfy absent"}
        kw.setdefault("actor", actor)
        return mod.publish(message, **kw)

    return plugins.Ctx({"user": {"username": actor}, "store": _SystemStore(type_),
                        "setting": _setting, "audit": _audit_fn, "tool_dir": m.get("_dir"),
                        "send_mail": _send_mail, "notify": _notify,
                        "call": make_caller(type_, actor),
                        # Appel de confiance : le périmètre d'un appelant externe (contrôleur)
                        # est vérifié AVANT, dans call() ; ici, tout est permis.
                        "can": lambda perm, resource=None: True,
                        "has_perm": lambda perm: True})


def call(type_, subpath, method, payload=None, *, actor="système", timeout=30, principal=None):
    """Appelle l'outil `type_` sur `subpath` et renvoie (status:int, data).
    docker → proxy HTTP vers le conteneur ; inprocess → backend.api(...).
    Ne lève pas : renvoie un statut d'erreur (404/502/503/500) le cas échéant.

    `principal` : l'appelant RÉEL quand le service relaie un ordre venu de l'extérieur
    (un contrôleur Ember+ / SW-P-08, cf. droits.who_controller). Une écriture est alors
    soumise aux règles de périmètre de l'outil, comme celle d'un utilisateur. Sans lui
    (code système), l'appel reste de confiance."""
    payload = payload or {}
    m = plugins.get(type_)
    if not m:
        return 404, {"error": "type inconnu"}
    if principal is not None and method != "GET":
        from . import droits
        refus = droits.check_write(type_, method, subpath, payload, principal, lambda _p: True)
        if refus:
            audit_log(type_, "refus_droits",
                      f"{method} /{subpath} — {refus[1].get('code')} {refus[1].get('rule_mode') or ''} "
                      f"{refus[1].get('permission') or refus[1].get('missing_permission') or ''} "
                      f"(contrôleur {principal.get('name') or principal.get('id')})", user_id=None, username=actor)
            return refus

    if plugins.runtime(type_) == "docker":
        base = deploy.proxy_base(type_)
        if not base:
            return 503, {"error": "outil non démarré"}
        try:
            up = _http.request(
                method, f"{base}/{subpath}",
                params=payload if method == "GET" else None,
                json=payload if method in ("POST", "PUT") else None,
                timeout=timeout)
        except requests.RequestException as e:
            # L'adresse du conteneur est mémorisée (cf. deploy.proxy_base) : un redémarrage
            # venu d'ailleurs la périme. On la jette et on réessaie UNE fois, plutôt que de
            # rendre un 502 jusqu'à l'expiration du cache.
            deploy.oublier_proxy_base(type_)
            base2 = deploy.proxy_base(type_)
            if not base2 or base2 == base:
                return 502, {"error": f"proxy : {e}"}
            try:
                up = _http.request(
                    method, f"{base2}/{subpath}",
                    params=payload if method == "GET" else None,
                    json=payload if method in ("POST", "PUT") else None,
                    timeout=timeout)
            except requests.RequestException as e2:
                return 502, {"error": f"proxy : {e2}"}
        try:
            data = up.json()
        except ValueError:
            data = {"raw": up.text}
        return up.status_code, data

    # in-process
    mod = plugins.backend(type_)
    if not mod or not hasattr(mod, "api"):
        return 404, {"error": "cet outil n'a pas de backend"}
    try:
        res = mod.api(subpath, method, payload, _system_ctx(type_, actor))
    except Exception as e:
        log.exception("tools.call: backend %s a levé une exception", type_)
        return 500, {"error": str(e)}
    if isinstance(res, tuple) and len(res) == 2:
        return res[0], res[1]
    return 200, res
