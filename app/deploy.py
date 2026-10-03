# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Cycle de vie d'un outil `runtime=docker` : build de l'image (depuis le Dockerfile du
plugin), démarrage/arrêt du conteneur, statut + résolution du port proxy.

Le manifeste d'un outil Docker déclare une section `docker` :

    "docker": {
        "image": "bobitool-<type>:<version>",   # tag d'image (défaut: dérivé du type/version)
        "build": true,                            # build depuis le Dockerfile du plugin si absente
        "port":  8080,                            # port HTTP interne du conteneur
        "env_settings": ["switch_host", ...]      # settings à injecter en variables d'env (secrets)
    }

L'app sert l'UI et **proxifie** /api/tools/<type>/<path> vers http://127.0.0.1:<port>/<path>
du conteneur (cf. routes.py). Aucun secret ne transite par le navigateur : les valeurs
sensibles vivent en settings et sont injectées comme variables d'environnement du conteneur.
"""
import json
import logging
import os
import re
import tarfile
import threading
import time

from . import plugins, settings
from .config import DOCKER_LABEL
from . import docker_driver as dk

log = logging.getLogger(__name__)


def _ver_of_image(image):
    """Version extraite d'un tag d'image `repo:tag` → `tag` (vide si indéterminable)."""
    return image.rsplit(":", 1)[-1] if image and ":" in image else ""


def disk_version(type_):
    """Version déclarée dans le plugin.json SUR DISQUE — la version « disponible » à migrer.
    Peut devancer le registre en mémoire tant que l'app n'a pas re-scanné (cas du dev)."""
    m = plugins.get(type_) or {}
    d = m.get("_dir")
    if d:
        try:
            with open(os.path.join(d, "plugin.json"), encoding="utf-8") as f:
                return json.load(f).get("version") or m.get("version") or ""
        except (OSError, ValueError):
            pass
    return m.get("version") or ""


def _docker_spec(type_):
    m = plugins.get(type_) or {}
    spec = dict(m.get("docker") or {})
    spec.setdefault("port", 8080)
    spec.setdefault("build", True)
    spec.setdefault("image", f"{DOCKER_LABEL}-{type_}:{m.get('version', 'latest')}")
    return spec


def container_name(type_):
    return f"{DOCKER_LABEL}-{type_}"


def is_docker_tool(type_):
    return plugins.runtime(type_) == "docker"


def ensure_image(type_):
    """Build l'image de l'outil si nécessaire (Dockerfile dans le dossier du plugin)."""
    m = plugins.get(type_)
    if not m:
        raise ValueError("type inconnu")
    spec = _docker_spec(type_)
    tag = spec["image"]
    if dk.image_exists(tag):
        return tag
    if not spec.get("build"):
        raise dk.DockerError(f"image {tag} absente et build désactivé pour {type_}")
    dk.build_image(tag, m["_dir"])
    return tag


def _env_for(type_, spec):
    """Variables d'env injectées dans le conteneur, lues des settings (secrets côté serveur).
    Clé `env_settings` du manifeste = liste de noms de settings ; chacune devient une variable
    d'env en MAJUSCULES."""
    env = {}
    for key in (spec.get("env_settings") or []):
        val = settings.get(f"{type_}__{key}")
        if val is None:
            val = settings.get(key)
        if val is not None:
            env[key.upper()] = str(val)
    return env


def _volumes_for(type_, spec):
    """Volumes nommés à monter. Le manifeste peut déclarer `docker.volume` = chemin dans
    le conteneur (ex. "/data") → un volume nommé `bobitool-<type>-data` y est monté, pour
    que l'inventaire/état de l'outil survive aux redéploiements.

    Il peut aussi déclarer `docker.shared_volumes` = liste de volumes nommés (appartenant
    à un AUTRE outil) à monter, typiquement en lecture seule, pour consommer l'inventaire
    d'un autre plugin sans le dupliquer ni le réécrire. Chaque entrée :
        { "volume": "bobitool-switch_ports-data", "mount": "/inventory", "ro": true }
    Le volume reste la propriété de l'outil qui l'écrit ; `ro` empêche toute écriture ici.
    """
    out = []
    vol = spec.get("volume")
    if vol:
        out.append(f"{DOCKER_LABEL}-{type_}-data:{vol}")
    own = f"{DOCKER_LABEL}-{type_}-data"
    for sv in (spec.get("shared_volumes") or []):
        name = sv.get("volume")
        mount = sv.get("mount")
        if not name or not mount:
            continue
        # Un outil ne peut écrire QUE dans son propre volume : le volume d'un AUTRE outil
        # est monté en lecture seule d'office, même si le manifeste demande ro:false
        # (sinon un plugin pourrait corrompre l'inventaire d'un autre).
        ro = sv.get("ro", True) or (name != own)
        suffix = ":ro" if ro else ""
        out.append(f"{name}:{mount}{suffix}")
    return out


def start_tool(type_):
    """Build (si besoin) + lance le conteneur de l'outil. Renvoie le statut."""
    spec = _docker_spec(type_)
    image = ensure_image(type_)
    name = container_name(type_)
    env = _env_for(type_, spec)
    labels = {f"{DOCKER_LABEL}.type": type_}
    volumes = _volumes_for(type_, spec)
    port = dk.run_container(name, image, spec["port"], env=env, labels=labels, volumes=volumes)
    oublier_proxy_base(type_)                 # le port d'hôte change à chaque recréation
    log.info("outil %s démarré (conteneur=%s, port hôte=%s, volumes=%s)", type_, name, port, volumes)
    return tool_status(type_)


def stop_tool(type_):
    """Arrête et retire le conteneur de l'outil."""
    name = container_name(type_)
    dk.stop_container(name, ignore=True)
    dk.rm_container(name, force=True, ignore=True)
    oublier_proxy_base(type_)
    return tool_status(type_)


def tool_status(type_):
    """{running, state, port, image, available, running_version, available_version, up_to_date}.
    `available_version` = version sur disque (à migrer) ; `running_version` = tag de l'image
    avec laquelle le conteneur tourne réellement ; `up_to_date` = pas de migration nécessaire."""
    spec = _docker_spec(type_)
    avail = disk_version(type_)
    if not dk.available():
        return {"running": False, "state": "no-docker", "port": None,
                "image": spec["image"], "available": False,
                "running_image": "", "running_version": "",
                "available_version": avail, "up_to_date": True,
                "missing_requirements": plugins.missing_requirements(type_)}
    st = dk.status(container_name(type_), spec["port"])
    running = st["state"] == "running"
    run_img = st.get("image") or ""
    run_ver = _ver_of_image(run_img)
    # On ne signale un décalage que si le conteneur TOURNE avec un tag identifiable ≠ disponible.
    up_to_date = (not running) or (not run_ver) or (run_ver == avail)
    return {"running": running, "state": st["state"], "port": st["port"],
            "image": spec["image"], "available": True,
            "running_image": run_img, "running_version": run_ver,
            "available_version": avail, "up_to_date": up_to_date,
            # Dépendances déclarées mais non installées. Sans cette remontée, un outil
            # consommateur démarre sur un volume vide créé à la volée par Docker et paraît
            # fonctionner — le pire des deux mondes (cf. plugins.missing_requirements).
            "missing_requirements": plugins.missing_requirements(type_),
            # Construction en arrière-plan (cf. migrate_async) : la barre d'état la suit.
            "build": build_state(type_)}


def migrate_tool(type_):
    """Aligne le conteneur sur la version disponible (disque) : re-scan du registre, build de
    l'image cible si absente, puis recréation du conteneur. Renvoie le statut à jour."""
    plugins.reload()             # le registre prend la version du disque
    start_tool(type_)            # ensure_image (build si besoin) + run (retire l'ancien conteneur)
    return tool_status(type_)


def migrate_async(type_):
    """« Migrer » depuis l'interface → (statut, asynchrone?).

    Image déjà présente : on recrée le conteneur tout de suite (quelques secondes). Image à
    construire : la construction part en ARRIÈRE-PLAN et recrée le conteneur à la fin. La
    version synchrone tenait une requête web ouverte plusieurs minutes (image de base, pip,
    apt) : la page ne montrait rien, et une requête coupée ou une page rechargée faisait
    disparaître le résultat — succès comme échec. Constaté le 2026-10-02 sur une installation
    distante : « la migration ne se fait pas, et pas de message d'erreur »."""
    plugins.reload()
    tag = _docker_spec(type_)["image"]
    if dk.image_exists(tag) or not _docker_spec(type_).get("build"):
        return migrate_tool(type_), False
    try:
        build_in_background(type_, puis_demarrer=True)
    except RuntimeError:
        pass                     # déjà en cours : on rend simplement l'état
    return tool_status(type_), True


# Adresse du conteneur, mise en CACHE. `tool_status` lance un `docker inspect` — un
# sous-processus, ~70 ms — et le dispatch d'outil l'appelait à CHAQUE requête : sur le chemin
# du tally, ces 70 ms pesaient plus que tout le reste réuni (mesuré le 2026-09-17 : 79 ms par
# le proxy contre 4 ms en tapant le conteneur directement). Le port d'un conteneur ne change
# qu'à un redémarrage, donc on le garde, et `oublier_proxy_base` le jette quand on y touche.
# `tools.call` le jette aussi sur échec de connexion et réessaie : même si un redémarrage
# venait d'ailleurs, l'appel suivant retrouve le bon port tout seul.
_BASE_TTL_S = 30.0
_base_cache = {}
_base_lock = threading.Lock()


def oublier_proxy_base(type_=None):
    """Invalide l'adresse mémorisée (un type, ou toutes)."""
    with _base_lock:
        if type_ is None:
            _base_cache.clear()
        else:
            _base_cache.pop(type_, None)


def proxy_base(type_):
    """URL de base pour proxifier les appels vers le conteneur, ou None si non démarré."""
    with _base_lock:
        hit = _base_cache.get(type_)
    if hit and time.monotonic() - hit[0] < _BASE_TTL_S:
        return hit[1]
    st = tool_status(type_)
    base = f"http://127.0.0.1:{st['port']}" if st["running"] and st["port"] else None
    with _base_lock:
        _base_cache[type_] = (time.monotonic(), base)
    return base


def tool_logs(type_, tail=200):
    return dk.logs(container_name(type_), tail=tail)


# ─── Images des outils : construire, exporter, importer (installations hors ligne) ─────────
# Construire une image télécharge l'image de base et les dépendances de l'outil : une machine
# sans Internet (ou sans DNS — cas rencontré le 2026-10-02 : `lookup registry-1.docker.io on
# [::1]:53 … connection refused`) ne PEUT PAS migrer un outil Docker, et l'interface restait
# sur l'ancienne version sans autre explication que le journal de build. La parade : construire
# là où Internet est disponible, exporter l'image, l'importer sur la machine isolée. « Migrer »
# trouve alors l'image déjà présente et se contente de recréer le conteneur (cf. ensure_image).
_builds = {}                    # type → {state: en_cours|ok|echec, debut, fin, erreur}
_builds_lock = threading.Lock()
_TAG = re.compile(r"^[a-z0-9][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]*$")


def images_inventory():
    """Pour chaque outil Docker : l'image ATTENDUE (celle que « Migrer » utilisera), sa
    présence locale, et la version du conteneur qui tourne."""
    plugins.reload()                      # après une mise à jour, le disque devance le registre
    dispo = dk.available()
    out = []
    for m in sorted(plugins.all(), key=lambda x: (x.get("label") or x.get("type") or "").lower()):
        type_ = m.get("type")
        if plugins.runtime(type_) != "docker":
            continue
        spec = _docker_spec(type_)
        tag = spec["image"]
        info = dk.image_inspect(tag) if dispo else None
        st = tool_status(type_) if dispo else {}
        with _builds_lock:
            b = dict(_builds.get(type_) or {})
        out.append({"type": type_, "label": m.get("label") or type_,
                    "version": disk_version(type_), "image": tag,
                    "present": bool(info), "size": (info or {}).get("size"),
                    "created": (info or {}).get("created"),
                    "running": bool(st.get("running")),
                    "running_version": st.get("running_version") or "",
                    "up_to_date": st.get("up_to_date", True),
                    "can_build": bool(spec.get("build")), "build": b})
    return {"docker": dispo, "label": DOCKER_LABEL, "tools": out}


def image_tag(type_):
    """Tag attendu d'un outil Docker, ou ValueError."""
    if not plugins.get(type_) or plugins.runtime(type_) != "docker":
        raise ValueError("outil Docker inconnu : %s" % type_)
    plugins.reload()                      # la version du DISQUE fait foi, pas le registre en mémoire
    return _docker_spec(type_)["image"]


def build_state(type_):
    with _builds_lock:
        return dict(_builds.get(type_) or {})


def build_in_background(type_, puis_demarrer=False):
    """Construit l'image attendue d'un outil, en arrière-plan. Une construction dure plusieurs
    minutes (image de base, pip, apt) : une requête web qui l'attendrait serait coupée en
    route, et l'exploitant ne saurait pas si elle a abouti."""
    tag = image_tag(type_)
    with _builds_lock:
        if (_builds.get(type_) or {}).get("state") == "en_cours":
            raise RuntimeError("une construction de cette image est déjà en cours")
        _builds[type_] = {"state": "en_cours", "debut": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "image": tag, "migration": bool(puis_demarrer)}

    def _run():
        try:
            if dk.image_exists(tag):
                res = {"state": "ok", "note": "image déjà présente"}
            else:
                dk.build_image(tag, plugins.get(type_)["_dir"])
                res = {"state": "ok"}
            if puis_demarrer:
                start_tool(type_)        # recrée le conteneur sur l'image neuve
                res["migre"] = True
        except Exception as e:
            # Le journal de build complet est long ; ses dernières lignes disent la cause
            # (« lookup registry-1.docker.io … », « pip … not found »).
            res = {"state": "echec", "erreur": "\n".join(str(e).strip().splitlines()[-12:])}
        with _builds_lock:
            _builds[type_].update(res, fin=time.strftime("%Y-%m-%dT%H:%M:%S"))
        log.info("construction %s : %s", tag, res.get("state"))

    threading.Thread(target=_run, daemon=True, name="build-" + type_).start()
    return {"type": type_, "image": tag, "state": "en_cours"}


def archive_tags(path):
    """Tags déclarés par une archive `docker save` (tar, gzip ou non), lus dans son
    manifest.json SANS rien extraire ni charger. On refuse ce qui n'est pas une image
    d'outil de CETTE application : importer une image arbitraire, c'est lui donner de quoi
    tourner sur l'hôte au prochain démarrage d'un conteneur qui porterait son nom."""
    try:
        with tarfile.open(path, mode="r:*") as tf:
            f = tf.extractfile("manifest.json")
            manifeste = json.load(f) if f else []
    except (tarfile.TarError, KeyError, ValueError, OSError, EOFError):
        raise ValueError("ce fichier n'est pas une archive d'image Docker (`docker save`).")
    tags = [t for ent in manifeste for t in (ent.get("RepoTags") or [])]
    if not tags:
        raise ValueError("l'archive ne porte aucun nom d'image : rien à quoi la rattacher.")
    outils = {m.get("type") for m in plugins.all() if plugins.runtime(m.get("type")) == "docker"}
    for t in tags:
        repo = t.rsplit(":", 1)[0]
        type_ = repo[len(DOCKER_LABEL) + 1:] if repo.startswith(DOCKER_LABEL + "-") else None
        if not _TAG.match(t) or type_ not in outils:
            raise ValueError("« %s » n'est pas l'image d'un outil Docker de cette installation "
                             "(attendu : %s-<outil>:<version>)." % (t, DOCKER_LABEL))
    return tags


def import_archive(path):
    """Contrôle puis charge une archive ; rend, pour chaque image, si c'est bien celle que
    l'outil attend — sinon « Migrer » ne s'en servira pas, et il faut le dire."""
    tags = archive_tags(path)
    charges = dk.load(path) or tags
    plugins.reload()
    out = []
    for t in charges:
        repo, ver = t.rsplit(":", 1)
        type_ = repo[len(DOCKER_LABEL) + 1:]
        attendue = _docker_spec(type_)["image"] if plugins.get(type_) else ""
        out.append({"image": t, "type": type_, "version": ver,
                    "attendue": attendue, "conforme": t == attendue})
    return out
