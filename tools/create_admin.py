#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Crée (ou réinitialise) un compte administrateur en ligne de commande.

Usage : ./venv/bin/python tools/create_admin.py
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db, db_get_user, db_create_user, db_update_user
from app.auth import hash_password


def main():
    init_db()
    print("=== Création d'un administrateur Bobi.Tools ===")
    username = input("Identifiant : ").strip()
    if not username:
        print("Identifiant requis."); return 1
    pw = getpass.getpass("Mot de passe (≥6) : ")
    if len(pw) < 6:
        print("Mot de passe trop court."); return 1
    if getpass.getpass("Confirmer : ") != pw:
        print("Les mots de passe ne correspondent pas."); return 1

    existing = db_get_user(username)
    if existing:
        db_update_user(existing["id"], role="admin", password_hash=hash_password(pw))
        print(f"✅ Compte « {username} » mis à jour (admin, mot de passe réinitialisé).")
    else:
        db_create_user(username, hash_password(pw), "admin")
        print(f"✅ Administrateur « {username} » créé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
