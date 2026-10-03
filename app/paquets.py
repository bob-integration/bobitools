# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Installation d'un PAQUET d'outil ou de service (zip) — le chemin UNIQUE, partagé par
l'import manuel (Réglages → Plugins / Services), le catalogue et `tools/catalogue.py`.
Sans dépendance à Flask : utilisable hors requête (installation en ligne de commande)."""
import io
import os
import tempfile
import zipfile

from . import catalogue, core_plugins, plugins


def _find_manifest_dir(root, manifest="plugin.json"):
    """Dossier contenant `manifest` : racine de l'archive, ou un niveau dessous (archives
    GitHub, qui enveloppent tout dans un dossier « depot-ref/ »)."""
    if os.path.isfile(os.path.join(root, manifest)):
        return root
    for n in sorted(os.listdir(root)):
        d = os.path.join(root, n)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, manifest)):
            return d
    return None


def installer_archive(data, genre, activate, attendu=None):
    """LE chemin d'installation d'un paquet, pour l'import manuel ET le catalogue (deux chemins
    finissent toujours par diverger — leçon de Bobi.Studio). `data` = octets d'un zip ;
    `genre` = plugin | service ; `attendu` = identité exigée (catalogue). Lève ValueError
    avec un message lisible. Zip-slip refusé ; un dossier racine unique (archives GitHub)
    est toléré."""
    manifest = "plugin.json" if genre == "plugin" else "manifest.json"
    mod = plugins if genre == "plugin" else core_plugins
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                root = os.path.realpath(tmp)
                for n in z.namelist():
                    cible = os.path.realpath(os.path.join(tmp, n))
                    if cible != root and not cible.startswith(root + os.sep):
                        raise ValueError(f"archive refusée : chemin hors du paquet ({n})")
                z.extractall(tmp)
        except zipfile.BadZipFile:
            raise ValueError("archive invalide (zip illisible)")
        src = _find_manifest_dir(tmp, manifest)
        if not src:
            raise ValueError(f"{manifest} introuvable dans l'archive")
        man, err = mod.validate_package(src)
        if err:
            raise ValueError(err)
        ident = man.get("type") if genre == "plugin" else man.get("id")
        if attendu and ident != attendu:
            raise ValueError(f"le paquet annonce « {ident} », le catalogue attendait « {attendu} »")
        if catalogue.gere_par_git(genre, ident):
            raise ValueError(f"plugins|services/{ident} est géré par git (submodule) : "
                             "le mettre à jour par git, pas par paquet")
        mod.stamp_imported_at(src)
        try:
            return mod.install_package(src, activate=activate)
        except Exception as e:
            raise ValueError(str(e))
