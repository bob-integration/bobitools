# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Le catalogue en ligne de commande — utilisé par get.sh pour poser des outils dès
l'installation, sans attendre l'interface.

    ./venv/bin/python tools/catalogue.py liste
    ./venv/bin/python tools/catalogue.py installe tous            # tout ce qui est publié
    ./venv/bin/python tools/catalogue.py installe switch_ports notes
    ./venv/bin/python tools/catalogue.py installe tous --plugins  # les outils seulement

Mêmes règles que l'écran : dépôts publics de l'organisation de confiance, même chemin
d'installation (app/paquets.py), dépendances (`requires`) installées d'abord. Anonyme par
défaut : 60 requêtes/heure par adresse IP, et une lecture en coûte une par dépôt — assez pour
une installation, pas pour dix. `BOBI_GH_TOKEN=<jeton>` porte la limite à 5 000.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import catalogue, paquets  # noqa: E402
from app.database import init_db  # noqa: E402


def main(argv):
    if not argv or argv[0] not in ("liste", "installe"):
        print(__doc__)
        return 2
    init_db()
    res = catalogue.lister(force=True)
    if res.get("erreur"):
        print(f"✗ catalogue : {res['erreur']}")
        return 1
    entrees = res.get("entrees") or []
    if argv[0] == "liste":
        for e in entrees:
            print(f"{e['genre']:8} {e['type']:20} {e['version_dispo'] or '—':10} "
                  f"{e['etat']:22} {e['label']}{'  (développement)' if e['dev'] else ''}")
        return 0
    noms = [a for a in argv[1:] if not a.startswith("--")]
    seulement_plugins = "--plugins" in argv
    if not noms:
        print("préciser « tous » ou des noms d'outils")
        return 2
    if noms == ["tous"]:
        choix = [e for e in entrees if not (seulement_plugins and e["genre"] != "plugin")]
    else:
        choix = [e for e in entrees if e["type"] in noms]
        inconnus = set(noms) - {e["type"] for e in choix}
        if inconnus:
            print(f"✗ inconnus du catalogue : {', '.join(sorted(inconnus))}")
            return 1
    # Dépendances d'abord : un outil qui lit l'inventaire d'un autre doit le trouver.
    par_type = {e["type"]: e for e in entrees}
    ordre, vus = [], set()

    def ajouter(e):
        if e["type"] in vus:
            return
        vus.add(e["type"])
        for t in e.get("requires") or []:
            if t in par_type:
                ajouter(par_type[t])
        ordre.append(e)

    for e in choix:
        ajouter(e)
    code = 0
    for e in ordre:
        if not e["installable"] or e["etat"] in ("a_jour", "git", "locale_plus_recente"):
            print(f"· {e['label']} : {e['etat']}")
            continue
        try:
            data, _ = catalogue.telecharger(e["depot"])
            r = paquets.installer_archive(data, e["genre"], True, attendu=e["type"])
            print(f"✓ {e['label']} {r['version']}")
        except Exception as err:
            print(f"✗ {e['label']} : {err}")
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
