# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Catalogue : les outils (plugins) et services PUBLIÉS sur GitHub, installables depuis
Réglages → Outils → Catalogue. Repris du catalogue de Bobi.Studio.

Les dépôts PUBLICS de l'organisation de confiance (`config.CATALOGUE_ORG`) dont le nom commence
par `bobitools-plugin-` ou `bobitools-service-` ; pour chacun, la dernière RELEASE et le
manifeste à ce tag. Différences avec Studio :

- un dépôt SANS release reste proposé, depuis sa branche par défaut, marqué « développement » :
  l'écosystème n'a pas toujours publié de releases, et masquer un outil disponible ne protège
  de rien — on le dit, on ne le cache pas ;
- un dossier d'outil géré par GIT (submodule, instance de développement) n'est JAMAIS écrasé :
  il est montré « géré par git », sans bouton ;
- les dépendances déclarées (`requires`) sont remontées, pour les installer ensemble.

★ L'ERREUR EST UNE DONNÉE. Un serveur sans Internet est un cas normal : la liste dit qu'elle n'a
pas pu lire, garde la dernière version connue et son âge — jamais une exception.
★ QUOTA. L'API anonyme de GitHub est plafonnée à 60 requêtes/heure/adresse : cache (30 min),
ETag (un 304 ne compte pas), manifestes lus sur raw.githubusercontent (CDN, hors quota), et un
jeton PERSONNEL facultatif (users.gh_token) pour qui bute sur le plafond.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from . import core_plugins as _cp
from . import plugins as _pl
from . import settings as _st

log = logging.getLogger(__name__)

PREFIXE_PLUGIN = "bobitools-plugin-"
PREFIXE_SERVICE = "bobitools-service-"
DEPOT_CORE = "bobitools"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CODELOAD = "https://codeload.github.com"
_TIMEOUT = 8
_TAILLE_MAX = 50 * 1024 * 1024
_UA = "bobitools-catalogue"

_verrou = threading.Lock()
_cache = {"t": 0.0, "entrees": [], "erreur": None, "org": None}
_etags = {}
_verrou_etag = threading.Lock()


def _reglages():
    """(organisation, TTL du cache, catalogue actif). L'organisation vient du CODE
    (config.CATALOGUE_ORG), jamais des réglages : c'est le seul point de confiance."""
    from . import config as _cfg
    org = str(getattr(_cfg, "CATALOGUE_ORG", "bob-integration") or "").strip()
    try:
        ttl = int(_st.get("catalogue_ttl_s") or 1800)
    except (TypeError, ValueError):
        ttl = 1800
    actif = str(_st.get("catalogue_actif") if _st.get("catalogue_actif") is not None else "1"
                ).strip().lower() not in ("0", "false", "off", "")
    return org, ttl, actif


def _jeton():
    """Jeton GitHub de l'utilisateur COURANT (par utilisateur, pas par site), ou None.
    Hors requête (ligne de commande, get.sh), seule la variable BOBI_GH_TOKEN en fournit un :
    une lecture du catalogue coûte ~1 requête par dépôt, et le quota anonyme (60/h, par IP)
    s'épuise vite derrière une adresse partagée."""
    try:
        from .auth import current_user
        j = ((current_user() or {}).get("gh_token") or "").strip()
        if j:
            return j
    except Exception:
        pass
    return os.environ.get("BOBI_GH_TOKEN", "").strip() or None


def _http_json(url):
    en_tetes = {"Accept": "application/vnd.github+json", "User-Agent": _UA}
    jeton = _jeton()
    if jeton:
        en_tetes["Authorization"] = "Bearer %s" % jeton
    with _verrou_etag:
        connu = _etags.get(url)
    if connu:
        en_tetes["If-None-Match"] = connu[0]
    req = urllib.request.Request(url, headers=en_tetes)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            corps = json.loads(r.read().decode("utf-8"))
            etag = r.headers.get("ETag")
            if etag:
                with _verrou_etag:
                    _etags[url] = (etag, corps)
            return corps
    except urllib.error.HTTPError as e:
        if e.code == 304 and connu:          # inchangé : gratuit en quota
            return connu[1]
        raise


def _http_texte(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8")


def _derniere_release(org, depot):
    """Tag de la release la plus récente (numérique), ou None. Pré-versions comprises."""
    from .version import analyser
    rels = _http_json("%s/repos/%s/%s/releases?per_page=10" % (API, org, depot))
    meilleure = None
    for r in rels or []:
        if r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "")
        v = analyser(tag)
        if v is not None and (meilleure is None or v > meilleure[0]):
            meilleure = (v, tag)
    return meilleure[1] if meilleure else None


def _manifeste_distant(org, depot, ref, fichier):
    """Le manifeste À CETTE RÉFÉRENCE : c'est lui qui fera foi après installation (un tag peut
    exister sans que la version du manifeste ait bougé). CDN raw : hors quota d'API."""
    try:
        return json.loads(_http_texte("%s/%s/%s/%s/%s" % (RAW, org, depot, ref, fichier)))
    except Exception:
        return None


def resume_changelog(meta, n=5):
    """Les `n` dernières entrées d'un journal de versions (meta.json), quel qu'en soit le format —
    trois coexistent dans l'écosystème :
      · {"changes": {"Nouveautés": [« vX — … »], "Corrections": […]}}
      · {"changelog": [{"version", "date", "notes"}]}
      · {"versions": [{"version", "published_at", "changes": [str]}]}"""
    meta = meta or {}
    out = []
    for e in meta.get("changelog") or []:
        if isinstance(e, dict) and e.get("notes"):
            out.append(f"v{e.get('version', '?')} — {e['notes']}")
    for e in meta.get("versions") or []:
        if isinstance(e, dict):
            for c in e.get("changes") or []:
                out.append(f"v{e.get('version', '?')} — {c}")
    if isinstance(meta.get("changes"), dict):
        for _bloc, entrees in meta["changes"].items():
            out.extend(x for x in entrees or [] if isinstance(x, str))
    return out[:n]


def _changelog_distant(org, depot, ref):
    return resume_changelog(_manifeste_distant(org, depot, ref, "meta.json"))


def dossier_local(genre, type_):
    return os.path.join(_pl.PLUGINS_DIR if genre == "plugin" else _cp.SERVICES_DIR, type_)


def _chemins_submodules():
    """Chemins déclarés en submodule dans le `.gitmodules` du cœur (instance de développement)."""
    racine = os.path.dirname(_pl.PLUGINS_DIR)
    try:
        with open(os.path.join(racine, ".gitmodules"), encoding="utf-8") as f:
            return {l.split("=", 1)[1].strip() for l in f if l.strip().startswith("path")}
    except OSError:
        return set()


def gere_par_git(genre, type_):
    """Le dossier est un SUBMODULE du cœur : le catalogue n'y écrit jamais — ce serait salir le
    checkout, et sa mise à jour passe par git. Critère : la déclaration dans `.gitmodules`, et
    non la simple présence d'un `.git` — une instance installée AVANT le catalogue (outils en
    submodules) garde ces `.git` après la mise à jour du cœur, et ses outils doivent rester
    gérables depuis le catalogue."""
    return f"{'plugins' if genre == 'plugin' else 'services'}/{type_}" in _chemins_submodules()


def _version_installee(genre, type_):
    if genre == "plugin":
        return (_pl.get(type_) or {}).get("version") if _pl.is_tool(type_) else None
    return (_cp.get(type_) or {}).get("version") if _cp.is_service(type_) else None


def _comparer(dispo, installee):
    if installee is None:
        return "absent"
    if not dispo:
        return "inconnu"
    if str(dispo) == str(installee):
        return "a_jour"
    return "maj" if _pl._ver_key(dispo) > _pl._ver_key(installee) else "locale_plus_recente"


def _construire(org):
    depots = _http_json("%s/orgs/%s/repos?per_page=100&type=public" % (API, org))
    entrees = []
    for d in depots:
        nom = d.get("name") or ""
        if nom.startswith(PREFIXE_PLUGIN):
            genre, ident, fichier = "plugin", nom[len(PREFIXE_PLUGIN):], "plugin.json"
        elif nom.startswith(PREFIXE_SERVICE):
            genre, ident, fichier = "service", nom[len(PREFIXE_SERVICE):], "manifest.json"
        else:
            continue
        try:
            tag = _derniere_release(org, nom)
            err = ""
        except Exception as e:
            tag, err = None, "releases illisibles (%s)" % e
        branche = d.get("default_branch") or "main"
        ref = tag or branche
        man = _manifeste_distant(org, nom, ref, fichier) or {}
        # L'identité vient du MANIFESTE, pas du nom du dépôt (un dépôt se renomme).
        type_ = (man.get("type") or man.get("id") or ident) if man else ident
        installee = _version_installee(genre, type_)
        git = installee is not None and gere_par_git(genre, type_)
        dispo = man.get("version") or ""
        requires = [r.get("type") for r in (man.get("requires") or [])
                    if isinstance(r, dict) and r.get("type")] if genre == "plugin" else []
        entrees.append({
            "genre": genre, "type": type_, "depot": nom,
            "tag": tag or "", "ref": ref, "dev": not tag,
            "label": man.get("label") or man.get("name") or type_,
            "description": (d.get("description") or "").strip(),
            "runtime": man.get("runtime") or ("inprocess" if genre == "service" else ""),
            "version_dispo": dispo, "version_installee": installee,
            "etat": "git" if git else _comparer(dispo, installee),
            "url": d.get("html_url") or "",
            "requires": requires,
            "installable": bool(man) and not git,
            "indisponible": "" if man else (err or "manifeste illisible"),
        })
    entrees.sort(key=lambda e: (e["genre"], e["label"].lower()))
    return entrees


def lister(force=False):
    org, ttl, actif = _reglages()
    if not actif:
        return {"actif": False, "entrees": [], "erreur": None, "org": org, "age_s": None}
    with _verrou:
        frais = (time.time() - _cache["t"]) < ttl and _cache["org"] == org
        if _cache["entrees"] and frais and not force:
            return {"actif": True, "entrees": _rafraichir_installes(_cache["entrees"]),
                    "erreur": _cache["erreur"], "org": org,
                    "age_s": round(time.time() - _cache["t"], 1)}
    try:
        entrees, erreur = _construire(org), None
    except urllib.error.HTTPError as e:
        entrees = None
        if e.code == 403:
            erreur = ("quota GitHub épuisé pour votre jeton (5 000 requêtes/heure) — réessayez "
                      "plus tard" if _jeton() else
                      "quota GitHub anonyme épuisé (60 requêtes/heure). Un jeton personnel porte "
                      "la limite à 5 000 par heure.")
        else:
            erreur = "GitHub a répondu HTTP %s" % e.code
    except urllib.error.URLError as e:
        entrees, erreur = None, "pas d'accès à GitHub : %s" % (getattr(e, "reason", e),)
    except Exception as e:
        entrees, erreur = None, "catalogue illisible : %r" % (e,)
    with _verrou:
        if entrees is not None:
            _cache.update({"t": time.time(), "entrees": entrees, "erreur": None, "org": org})
        else:
            _cache["erreur"] = erreur
        return {"actif": True, "entrees": _rafraichir_installes(_cache["entrees"]),
                "erreur": _cache["erreur"], "org": org,
                "age_s": None if not _cache["t"] else round(time.time() - _cache["t"], 1)}


def _rafraichir_installes(entrees):
    """L'état INSTALLÉ change sans que GitHub change (on vient d'installer) : il se relit à
    chaque appel, sans toucher au cache de la liste distante."""
    out = []
    for e in entrees:
        e = dict(e)
        inst = _version_installee(e["genre"], e["type"])
        git = inst is not None and gere_par_git(e["genre"], e["type"])
        e["version_installee"] = inst
        e["etat"] = "git" if git else _comparer(e["version_dispo"], inst)
        e["installable"] = bool(e["version_dispo"]) and not git
        out.append(e)
    return out


def entree(depot):
    for e in lister().get("entrees") or []:
        if e["depot"] == depot:
            return e
    return None


def nouveautes(depot):
    e = entree(depot)
    if not e:
        return []
    org, _t, _a = _reglages()
    return _changelog_distant(org, depot, e["ref"])


def telecharger(depot):
    """Archive zip de la référence publiée (tag, à défaut branche). ⚠ Le nom du dépôt vient
    d'une requête : on exige qu'il figure DÉJÀ dans le catalogue — une liste blanche ne se
    contourne pas avec un caractère bien choisi."""
    e = entree(depot)
    if not e:
        raise ValueError("dépôt absent du catalogue : %s" % depot)
    org, _ttl, _actif = _reglages()
    kind = "tags" if e["tag"] else "heads"
    url = "%s/%s/%s/zip/refs/%s/%s" % (CODELOAD, org, depot, kind, e["ref"])
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        brut = r.read(_TAILLE_MAX + 1)
    if len(brut) > _TAILLE_MAX:
        raise ValueError("archive trop volumineuse (> %d Mo)" % (_TAILLE_MAX // (1024 * 1024)))
    return brut, e


def derniere_version_core():
    """La dernière version publiée du cœur (releases de `bobitools`), ou None."""
    org, _ttl, actif = _reglages()
    if not actif:
        return None
    try:
        tag = _derniere_release(org, DEPOT_CORE)
    except Exception:
        return None
    return tag.lstrip("vV") if tag else None
