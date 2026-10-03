# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Version de Bobi.Tools — la SOURCE UNIQUE, et le comparateur qui va avec (repris de Bobi.Studio).

★ POURQUOI UN NUMÉRO DANS LE CODE. Bobi.Tools n'en avait aucun : seul l'identifiant de build
(date · hash) disait ce qui tournait. Le dépôt public `bobitools` est publié par versions (tag
`v<VERSION>`), et un composant doit pouvoir exiger un cœur minimal (`requires.core_min`).

`app/` est embarqué dans tout paquet (builder) : pas de fichier VERSION à la racine, qui
marcherait en dev et manquerait sur une instance installée.

⚠ CETTE CONSTANTE ET LE TAG PUBLIÉ DOIVENT S'ACCORDER. `tools/export_public.py` en tire le
titre et le tag de la version publiée : deux sources recopiées à la main finissent toujours par
diverger, et c'est le genre d'écart que personne ne remarque avant d'en avoir besoin.
"""

VERSION = "1.0.0"


def analyser(v):
    """« 0.9.2 » → (0, 9, 2). None dès qu'un segment n'est pas numérique.

    ★ NONE PLUTÔT QU'UN ORDRE INVENTÉ. Sur « 0.24-fix » ou « main », on ne compare pas du tout :
    un refus fondé sur une comparaison bancale serait pire que l'absence de contrôle. C'est la
    règle déjà appliquée aux images de nœud (`docker_compute`), reprise ici pour que les deux
    contrôles se comportent pareil.
    """
    v = str(v or "").strip().lstrip("vV")
    if not v:
        return None
    parts = v.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def au_moins(courante, minimale):
    """`courante` satisfait-elle l'exigence `minimale` ?

    Renvoie True quand l'exigence est vide (rien n'est exigé) ET quand la comparaison est
    impossible — ne pas savoir comparer n'autorise pas à bloquer un déploiement. L'appelant qui
    veut le TRACER utilise `comparable()`.

    Les longueurs inégales sont comblées par des zéros : 0.9 vaut 0.9.0, sinon « 0.9 » serait
    jugé antérieur à « 0.9.0 » et un exploitant n'aurait aucun moyen de le comprendre.
    """
    if not str(minimale or "").strip():
        return True
    a, b = analyser(courante), analyser(minimale)
    if a is None or b is None:
        return True
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a >= b


def comparable(courante, minimale):
    """False quand l'un des deux numéros n'est pas analysable — pour le journaliser."""
    if not str(minimale or "").strip():
        return True
    return analyser(courante) is not None and analyser(minimale) is not None


def core_min_de(manifeste):
    """Version minimale de Bobi.Tools exigée par ce manifeste (plugin ou service), ou ""."""
    req = (manifeste or {}).get("requires") or {}
    return str(req.get("core_min") or "").strip()
