# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Services globaux de Bobi.Tools (facilités transverses, ≠ outils-plugins).

Un *service* n'apparaît pas au lanceur : c'est une facilité de la plateforme,
démarrée au boot et configurée depuis les Réglages. Contrairement aux outils
(plugins/<type>/), les services vivent à la RACINE du dépôt (services/<id>/) et
sont des unités VERSIONNÉES de première classe, séparées du cœur (app/) :

    services/<id>/
      manifest.json     # id, label, version (+ settings_keys/nav_tab optionnels)
      meta.json         # changelog/version
      __init__.py       # module Python du service : boot()? register_routes(bp)?
      versions/<ver>/   # archives (rollback interne, hors git)

Le registre, le scan, le versioning et l'import/export/activation sont gérés par
app/core_plugins.py — jumeau de app/plugins.py pour les outils.
"""
