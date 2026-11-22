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

★ INDEX STATIQUE (2026-11-22, repris de Bobi.Studio). Lire le catalogue par l'API coûte une
requête pour l'organisation plus UNE PAR DÉPÔT, sur 60 par heure et par adresse — deux
actualisations suffisaient à épuiser le quota d'un site entier. La publication écrit donc ce que
l'API répondrait dans `catalog.json`, dans un dépôt à part (`bobitools-catalog`), servi par
raw.githubusercontent : un CDN, hors quota. Une requête, aucun quota ; l'API reste le repli
(index absent, illisible, ou autre organisation). Construit par `tools/catalogue_index.py`,
lancé par `tools/publier_release.sh` après chaque publication. Même schéma que Studio.
L'index porte aussi le CŒUR (dernière release de `bobitools` et ses fichiers joints) : c'est
là que la page Mises à jour et la veille (`update_watch`) lisent la version disponible.
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
DEPOT_INDEX = "bobitools-catalog"
INDEX_FICHIER = "catalog.json"
INDEX_SCHEMA = 1
# Fichiers qu'une release du cœur doit porter pour être APPLICABLE (cf. updater.apply_update_github).
ARTEFACT_CORE = "bobitools.zip"
ARTEFACT_SOMMES = "SHA256SUMS"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CODELOAD = "https://codeload.github.com"
_TIMEOUT = 8
_TAILLE_MAX = 50 * 1024 * 1024
_UA = "bobitools-catalogue"

_verrou = threading.Lock()
_cache = {"t": 0.0, "entrees": [], "erreur": None, "org": None, "source": None}
# Cache du CŒUR, séparé : la page Mises à jour l'interroge à chaque affichage.
_cache_core = {"t": 0.0, "info": None, "org": None}
_verrou_core = threading.Lock()
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


def _publies_api(org):
    """Ce qui est PUBLIÉ, lu par l'API (1 requête + 1 par dépôt) : sans aucun état local."""
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
        req = man.get("requires")
        requires = [r.get("type") for r in (req if isinstance(req, list) else [])
                    if isinstance(r, dict) and r.get("type")] if genre == "plugin" else []
        from .version import core_min_de
        entrees.append({
            "genre": genre, "type": type_, "depot": nom,
            "tag": tag or "", "ref": ref, "dev": not tag,
            "label": man.get("label") or man.get("name") or type_,
            "description": (d.get("description") or "").strip(),
            "runtime": man.get("runtime") or ("inprocess" if genre == "service" else ""),
            "version_dispo": man.get("version") or "",
            "url": d.get("html_url") or "",
            "requires": requires,
            "core_min": core_min_de(man),
            "installable": bool(man),
            "indisponible": "" if man else (err or "manifeste illisible"),
        })
    return entrees


_CLES_INDEX = ("genre", "type", "depot", "tag", "ref", "dev", "label", "description", "runtime",
               "version_dispo", "url", "requires", "core_min", "installable", "indisponible")


def _index_cdn(org):
    """L'index publié (`catalog.json` sur le CDN), ou None s'il est absent ou pas digne de foi.

    ⚠ SA PROPRE ORGANISATION SEULEMENT. L'index nomme des dépôts que `telecharger` ira chercher :
    un index qui se réclame d'une autre organisation est refusé, comme toute entrée dont le dépôt
    ne porte pas un préfixe de paquet — la même règle que le chemin par l'API."""
    url = "%s/%s/%s/HEAD/%s" % (RAW, org, DEPOT_INDEX, INDEX_FICHIER)
    try:
        idx = json.loads(_http_texte(url))
    except Exception as e:                       # absent (404), hors ligne, pas du JSON : repli
        log.debug("catalogue : pas d'index à %s (%s)", url, e)
        return None
    if not isinstance(idx, dict) or idx.get("schema") != INDEX_SCHEMA or idx.get("org") != org:
        log.info("catalogue : index de %s ignoré (schéma/organisation)", url)
        return None
    entrees = []
    for x in idx.get("entries") or []:
        if not isinstance(x, dict):
            continue
        depot = str(x.get("depot") or "")
        if not (depot.startswith(PREFIXE_PLUGIN) or depot.startswith(PREFIXE_SERVICE)):
            continue
        e = {k: x.get(k) for k in _CLES_INDEX}
        e["genre"] = "plugin" if depot.startswith(PREFIXE_PLUGIN) else "service"
        e["type"] = str(e["type"] or depot.split("-", 2)[-1])
        e["label"] = str(e["label"] or e["type"])
        e["tag"] = str(e["tag"] or "")
        e["ref"] = str(e["ref"] or e["tag"] or "main")
        e["dev"] = not e["tag"]
        e["description"] = str(e["description"] or "")
        e["runtime"] = str(e["runtime"] or "")
        e["version_dispo"] = str(e["version_dispo"] or "")
        e["url"] = str(e["url"] or "")
        e["requires"] = [str(t) for t in (e["requires"] or []) if isinstance(t, str)]
        e["core_min"] = str(e["core_min"] or "")
        e["installable"] = bool(e["installable"])
        e["indisponible"] = str(e["indisponible"] or "")
        entrees.append(e)
    return {"entries": entrees, "core": idx.get("core") if isinstance(idx.get("core"), dict) else None,
            "generated_at": idx.get("generated_at")}


def construire_index(org):
    """L'index que publie `tools/catalogue_index.py` : ce que dit l'API, sans état local."""
    import datetime
    return {"schema": INDEX_SCHEMA, "org": org,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entries": [{k: e.get(k) for k in _CLES_INDEX} for e in _publies_api(org)],
            "core": _core_api(org)}


def _construire(org):
    """Le catalogue : ce qui est publié (index statique, sinon l'API) + ce qui est installé ICI.
    Rend (entrées, source) — source = "index" ou "api"."""
    from .version import exigence_non_tenue
    idx = _index_cdn(org)
    publies, source = (idx["entries"], "index") if idx else (_publies_api(org), "api")
    entrees = []
    for p in publies:
        e = dict(p)
        installee = _version_installee(e["genre"], e["type"])
        git = installee is not None and gere_par_git(e["genre"], e["type"])
        trop = exigence_non_tenue({"core_min": e.get("core_min")})
        e["version_installee"] = installee
        e["etat"] = "git" if git else _comparer(e["version_dispo"], installee)
        e["core_trop_ancien"] = trop
        e["installable"] = bool(e["installable"]) and not git and not trop
        e["indisponible"] = trop or e["indisponible"]
        entrees.append(e)
    entrees.sort(key=lambda e: (e["genre"], e["label"].lower()))
    return entrees, source


def lister(force=False):
    org, ttl, actif = _reglages()
    if not actif:
        return {"actif": False, "entrees": [], "erreur": None, "org": org, "age_s": None}
    with _verrou:
        frais = (time.time() - _cache["t"]) < ttl and _cache["org"] == org
        if _cache["entrees"] and frais and not force:
            return {"actif": True, "entrees": _rafraichir_installes(_cache["entrees"]), "source": _cache["source"],
                    "erreur": _cache["erreur"], "org": org,
                    "age_s": round(time.time() - _cache["t"], 1)}
    source = None
    try:
        (entrees, source), erreur = _construire(org), None
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
            _cache.update({"t": time.time(), "entrees": entrees, "erreur": None, "org": org,
                           "source": source})
        else:
            _cache["erreur"] = erreur
        return {"actif": True, "entrees": _rafraichir_installes(_cache["entrees"]), "source": _cache["source"],
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
        e["installable"] = bool(e["version_dispo"]) and not git and not e.get("core_trop_ancien")
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


def derniere_version_core(force=False):
    """La dernière version publiée de Bobi.Tools, ou None.

    On lit les RELEASES, pas la branche : une branche dit « l'état du dépôt à cet instant », ce
    qui n'est pas une version et ne se compare à rien. L'index statique d'abord (hors quota),
    l'API en repli. Mis en cache comme le reste ; `force` relit (bouton « Vérifier »).
    Rend {"version", "tag", "url", "publiee_le", "prerelease", "artefacts": [...]} ou None."""
    org, ttl, actif = _reglages()
    if not actif:
        return None
    with _verrou_core:
        frais = (time.time() - _cache_core["t"]) < ttl and _cache_core["org"] == org
        if frais and not force and _cache_core["info"] is not None:
            return dict(_cache_core["info"])
    idx = _index_cdn(org)
    info = (idx or {}).get("core")
    if not (isinstance(info, dict) and info.get("version") and info.get("tag")):
        info = _core_api(org)
    if not info:
        return None
    info = {k: info.get(k) for k in ("version", "tag", "url", "publiee_le", "prerelease", "artefacts")}
    info["artefacts"] = [str(a) for a in (info.get("artefacts") or [])]
    with _verrou_core:
        _cache_core.update({"t": time.time(), "info": dict(info), "org": org})
    return info


def _core_api(org):
    """Dernière release du cœur, lue par l'API, ou None."""
    try:
        rels = _http_json("%s/repos/%s/%s/releases?per_page=10" % (API, org, DEPOT_CORE))
    except Exception as e:
        log.info("catalogue : releases du cœur illisibles (%s)", e)
        return None
    from .version import analyser
    meilleure = None
    for r in rels or []:
        if r.get("draft"):
            continue
        tag = str(r.get("tag_name") or "")
        v = analyser(tag)
        if v is not None and (meilleure is None or v > meilleure[0]):
            meilleure = (v, r, tag)
    if not meilleure:
        return None
    _v, r, tag = meilleure
    return {"version": tag.lstrip("vV"), "tag": tag, "url": r.get("html_url"),
            "publiee_le": (r.get("published_at") or "")[:10],
            "prerelease": bool(r.get("prerelease")),
            # ⚠ Une release SANS ces fichiers n'est pas applicable : l'archive de source servie
            # d'office par GitHub n'a pas d'empreinte, et on n'applique pas du code non vérifié.
            "artefacts": [a.get("name") for a in (r.get("assets") or [])]}


def maj_core_disponible(force=False):
    """(disponible, info) — compare la dernière version publiée à celle de CETTE instance."""
    from .version import VERSION, au_moins, comparable
    info = derniere_version_core(force=force)
    if not info:
        return False, None
    info["version_installee"] = VERSION
    if not comparable(info["version"], VERSION):
        return False, info
    dispo = not au_moins(VERSION, info["version"])
    info["applicable"] = ARTEFACT_CORE in info["artefacts"] and ARTEFACT_SOMMES in info["artefacts"]
    return dispo, info
