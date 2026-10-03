#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Wrapper CLI du builder (app/builder.py).

Construit le zip de distribution dans dist/. Par défaut : TOUS les plugins et services
installés, identité de build (re)tamponnée (= nouvelle release).

  ./venv/bin/python tools/build_dist.py                 # tout, nouvelle release
  ./venv/bin/python tools/build_dist.py --plugins notes switch_ports
  ./venv/bin/python tools/build_dist.py --services emberplus --no-stamp
"""
import argparse
import os
import sys

# Racine du dépôt dans sys.path pour importer app.builder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import builder  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Construit le zip de distribution Bobi.Tools.")
    ap.add_argument("--plugins", nargs="*", default=None,
                    help="ids de plugins à inclure (défaut : tous les installés)")
    ap.add_argument("--services", nargs="*", default=None,
                    help="ids de services à inclure (défaut : tous les installés)")
    ap.add_argument("--dest", default=builder.DEFAULT_DEST, help="chemin du zip de sortie")
    ap.add_argument("--no-stamp", action="store_true",
                    help="réutilise l'identité de build existante (ne crée pas de release)")
    args = ap.parse_args()

    try:
        res = builder.build(plugins=args.plugins, services=args.services,
                            dest=args.dest, stamp=not args.no_stamp)
    except RuntimeError as e:
        print(f"ERREUR : {e}", file=sys.stderr)
        return 1
    mb = res["size"] / 1048576
    print(f"OK — {res['file']} ({mb:.2f} Mo, {res['count']} fichiers)")
    print(f"  build : {res['label']}")
    print(f"  plugins  : {', '.join(res['plugins']) or '(aucun)'}")
    print(f"  services : {', '.join(res['services']) or '(aucun)'}")
    print(f"  → {res['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
