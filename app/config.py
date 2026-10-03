# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

import os

# ── Valeurs par défaut ────────────────────────────────────────────────────────
# Surcharger dans config_local.py à la racine du projet (non versionné).

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH  = os.path.join(_ROOT, "db_bobitools.db")
LOG_PATH = os.path.join(_ROOT, "bobitools.log")

# Port d'écoute de l'app Flask.
HTTP_PORT = 5000

# Préfixe des labels Docker posés sur les conteneurs d'outils (runtime=docker).
# Sert au pilote Docker à retrouver « ses » conteneurs sans collision avec d'autres.
DOCKER_LABEL = "bobitool"

# Marque le cookie de session « Secure » (transmis uniquement en HTTPS). À activer
# (config_local.py) dès que l'app est servie derrière un reverse-proxy TLS. Laissé à
# False par défaut pour ne pas casser un accès direct en HTTP (http://ip:5000).
SESSION_COOKIE_SECURE = False

# Organisation GitHub du CATALOGUE (Réglages → Outils → Catalogue). C'est le SEUL point de
# confiance du mécanisme : installer un outil, c'est exécuter son code sur ce serveur. Elle ne
# se règle donc PAS depuis l'interface (cela ferait d'un droit de réglage un droit d'exécution),
# seulement ici ou dans config_local.py — ce qui exige un accès au serveur. Repris de Bobi.Studio.
CATALOGUE_ORG = "bob-integration"

# ── Surcharge locale ──────────────────────────────────────────────────────────
try:
    # config_local.py est à la racine du projet (dans sys.path au démarrage).
    from config_local import *          # noqa: F401,F403
except ImportError:
    pass
