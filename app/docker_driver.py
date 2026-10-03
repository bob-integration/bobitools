# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Pilote Docker **local** (via la CLI `docker`, en subprocess). Bobi.Tools tourne sur une
VM/serveur où Docker est disponible localement : les outils `runtime=docker` sont de
simples conteneurs lancés ici même, exposant un port HTTP que l'app proxifie.

Aucune dépendance Python supplémentaire (pas de SDK docker) — on parle à la CLI, ce qui
suffit pour build/run/inspect/stop et reste lisible. Toutes les fonctions lèvent
`DockerError` avec un message actionnable en cas d'échec.
"""
import json
import logging
import re
import shutil
import subprocess
import zlib

log = logging.getLogger(__name__)


class DockerError(RuntimeError):
    pass


def _docker(*args, timeout=600, check=True):
    """Lance `docker <args>` et renvoie (stdout). Lève DockerError sur échec."""
    if not shutil.which("docker"):
        raise DockerError("Docker introuvable sur cet hôte (commande `docker` absente).")
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DockerError(f"docker {args[0]} : délai dépassé ({timeout}s)")
    if check and p.returncode != 0:
        raise DockerError((p.stderr or p.stdout or "échec docker").strip())
    return p.stdout


def available():
    """True si le démon Docker répond (sinon les outils Docker sont juste indisponibles)."""
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                       capture_output=True, text=True, timeout=10, check=True)
        return True
    except Exception:
        return False


def image_exists(tag):
    try:
        out = _docker("images", "-q", tag, timeout=30)
        return bool(out.strip())
    except DockerError:
        return False


def image_inspect(tag):
    """{size, created} d'une image locale, ou None si elle n'existe pas."""
    try:
        out = _docker("image", "inspect", tag, timeout=30, check=False)
        data = json.loads(out)[0] if out.strip() else None
    except (DockerError, ValueError, IndexError):
        return None
    if not data:
        return None
    return {"size": int(data.get("Size") or 0), "created": (data.get("Created") or "")[:19]}


def save_stream(tag, chunk=1 << 16):
    """`docker save <tag>`, compressé à la volée (gzip), en FLUX : une image pèse des
    centaines de Mo, et la passer par un fichier temporaire doublerait l'espace disque requis
    sur des machines qui en manquent souvent. L'appelant vérifie l'existence de l'image AVANT
    d'ouvrir le flux — une fois l'en-tête HTTP parti, une erreur ne peut plus se dire.
    Le processus est tué si le client abandonne le téléchargement."""
    p = subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    comp = zlib.compressobj(6, zlib.DEFLATED, 31)          # wbits 31 = conteneur gzip
    try:
        while True:
            bloc = p.stdout.read(chunk)
            if not bloc:
                break
            sortie = comp.compress(bloc)
            if sortie:
                yield sortie
        yield comp.flush()
        if p.wait(timeout=60) != 0:
            log.error("docker save %s : %s", tag, (p.stderr.read() or b"").decode(errors="replace").strip())
    finally:
        if p.poll() is None:
            p.kill()


def load(path):
    """`docker load -i <path>` (tar, compressé ou non) → tags chargés."""
    out = _docker("load", "-i", path, timeout=1800)
    return re.findall(r"Loaded image:\s*(\S+)", out)


def build_image(tag, context_dir, dockerfile="Dockerfile"):
    """Build l'image `tag` depuis `context_dir`. Lève DockerError (avec le log build)
    en cas d'échec. Build long → timeout généreux."""
    log.info("docker build %s (context=%s)", tag, context_dir)
    return _docker("build", "-t", tag, "-f", f"{context_dir}/{dockerfile}",
                   context_dir, timeout=1800)


def run_container(name, image, internal_port, env=None, labels=None, volumes=None):
    """Lance un conteneur détaché `name` depuis `image`, en publiant `internal_port`
    sur un port aléatoire de 127.0.0.1 (jamais exposé hors de l'hôte). Renvoie le port
    hôte mappé. Retire d'abord un éventuel conteneur homonyme arrêté.

    `volumes` : liste de chaînes "<volume_nommé>:<chemin_conteneur>" (persistance : un
    volume Docker nommé survit à l'arrêt/suppression du conteneur). Utilisé pour
    l'inventaire d'un outil (ex. switch_ports : ses switchs + identifiants)."""
    rm_container(name, force=True, ignore=True)
    # `host.docker.internal` → l'hôte, pour qu'un outil conteneurisé puisse rappeler l'app
    # (ex. switch_ports → service Mail via /api/mail/send). Standard Docker (host-gateway).
    args = ["run", "-d", "--name", name, "--restart", "unless-stopped",
            "--add-host", "host.docker.internal:host-gateway",
            "-p", f"127.0.0.1::{internal_port}"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    for k, v in (labels or {}).items():
        args += ["--label", f"{k}={v}"]
    for vol in (volumes or []):
        args += ["-v", vol]
    args.append(image)
    _docker(*args, timeout=120)
    return host_port(name, internal_port)


def host_port(name, internal_port):
    """Port hôte mappé pour `internal_port/tcp` d'un conteneur, ou None."""
    try:
        out = _docker("inspect", name, timeout=30)
        data = json.loads(out)[0]
        ports = (data.get("NetworkSettings") or {}).get("Ports") or {}
        binding = ports.get(f"{internal_port}/tcp")
        if binding:
            return int(binding[0]["HostPort"])
    except (DockerError, KeyError, IndexError, ValueError, TypeError):
        pass
    return None


def status(name, internal_port=None):
    """État d'un conteneur : {'state': running|exited|absent, 'port': <int|None>}."""
    try:
        out = _docker("inspect", name, timeout=30, check=False)
        if not out.strip():
            return {"state": "absent", "port": None}
        data = json.loads(out)[0]
        st = (data.get("State") or {}).get("Status") or "absent"
        image = (data.get("Config") or {}).get("Image") or ""   # tag avec lequel le conteneur tourne
        port = None
        if internal_port is not None:
            ports = (data.get("NetworkSettings") or {}).get("Ports") or {}
            binding = ports.get(f"{internal_port}/tcp")
            if binding:
                port = int(binding[0]["HostPort"])
        return {"state": st, "port": port, "image": image}
    except (DockerError, KeyError, IndexError, ValueError, TypeError):
        return {"state": "absent", "port": None}


def stop_container(name, ignore=False):
    try:
        _docker("stop", name, timeout=60)
    except DockerError:
        if not ignore:
            raise


def rm_container(name, force=False, ignore=False):
    try:
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(name)
        _docker(*args, timeout=60)
    except DockerError:
        if not ignore:
            raise


def logs(name, tail=200):
    try:
        return _docker("logs", "--tail", str(tail), name, timeout=30, check=False)
    except DockerError as e:
        return f"(logs indisponibles : {e})"
