#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
"""Construit `catalog.json`, l'index statique du catalogue (repris de Bobi.Studio, 2026-11-22).

Lancé par la publication APRÈS chaque release (cœur, outil ou service), puis committé dans le
dépôt `bobitools-catalog` de l'organisation (`tools/publier_release.sh` s'en charge). Les
instances le lisent sur raw.githubusercontent — un CDN, hors du quota de l'API GitHub — au lieu
de dépenser une requête d'API par dépôt (cf. `app/catalogue.py`, « INDEX STATIQUE »). Sans lui
elles fonctionnent quand même, par l'API, et butent bien plus tôt sur les 60 requêtes/heure
d'une adresse anonyme.

    GITHUB_TOKEN=… ./venv/bin/python tools/catalogue_index.py -o /chemin/bobitools-catalog/catalog.json

Le jeton ne sert qu'à relever le quota d'API pour CETTE exécution (≈ 1 + une requête par
dépôt) ; l'index ne contient rien de secret. La sortie est refusée quand une entrée n'a pas pu
être lue (releases illisibles) ou quand la release du cœur est illisible : publier un index qui
cache un composant serait pire que garder le précédent.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    from app import catalogue as cat
    from app import config
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--org", default=getattr(config, "CATALOGUE_ORG", "bob-integration"))
    p.add_argument("-o", "--output", default="catalog.json")
    a = p.parse_args()
    jeton = (os.environ.get("GITHUB_TOKEN") or os.environ.get("BOBI_GH_TOKEN") or "").strip() or None
    cat._jeton = lambda: jeton                   # pas d'utilisateur Flask ici : le jeton de cette exécution
    idx = cat.construire_index(a.org)
    douteux = [e["depot"] for e in idx["entries"]
               if (e.get("indisponible") or "").startswith("releases illisibles")]
    if douteux:
        sys.exit("index refusé — releases illisibles pour : %s (quota ? réseau ?)" % ", ".join(douteux))
    if not idx.get("core"):
        sys.exit("index refusé — la release du cœur est illisible")
    with open(a.output, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print("%s : %d entrées, cœur %s, organisation %s" % (a.output, len(idx["entries"]),
                                                       idx["core"]["tag"], a.org))


if __name__ == "__main__":
    main()
