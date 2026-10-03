# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Projet Bobi.Tools

Hébergement web d'outils sous forme de **plugins**. Dérivé du socle de **Bobi.Studio**
(même charte, thèmes, i18n, auth, système de plugins versionnés) mais sans le métier
broadcast ST 2110/Proxmox. Un « plugin » n'est plus un *type de container* déployé en LXC :
c'est un **outil hébergé**, soit in-process, soit en conteneur Docker local.

## Stack
- Python 3.13, Flask, SQLite. Pas de build, ni lint, ni tests configurés. Un seul venv `./venv`.
- Lancement : `./venv/bin/python main.py` → Flask sur `0.0.0.0:5000` + thread backup quotidien.

## Architecture (modèle hybride — `runtime` déclaré par l'outil)
- **`runtime: "inprocess"`** : `backend.py` chargé en process (ou front-pur). C'est le SEUL
  code plugin exécuté in-process. Contrat : `def api(path, method, payload, ctx)`.
- **`runtime: "docker"`** : conteneur Docker **local** (build depuis le `Dockerfile` du
  plugin). L'app **proxifie** `/api/tools/<type>/<path>` vers le port HTTP du conteneur.
- Dispatch unifié côté front : tout outil parle à `/api/tools/<type>/<path>` ; l'UI ignore
  le runtime. Voir `app/routes.py:api_tool_dispatch`.

## Structure fichiers (`app/`)
```
config.py        ← DB_PATH, LOG_PATH, HTTP_PORT, DOCKER_LABEL (+ config_local.py)
database.py      ← SQLite : settings, users, i18n_overrides, plugin_store, audit
auth.py          ← rôles (admin/operator/viewer) + GROUPES (user_group_ids) + ACCÈS PAR
                   OUTIL (can_access_tool, require_tool)
settings.py      ← accesseur DB-first (DEFAULTS élagués)
i18n.py          ← moteur i18n (REPRIS VERBATIM de Studio) : fichiers + surcouche DB
plugins.py       ← REGISTRE D'OUTILS : scan, runtime, backend loader, sections, versions,
                   import/export/activation de paquets .bobitool, coerce_config
core_plugins.py  ← REGISTRE DE SERVICES (jumeau de plugins.py) : scan services/, versions,
                   import/export/activation, boot_all, register_all_routes, all_settings_defaults
tools.py         ← appel d'un outil DEPUIS le serveur (in-process, sans HTTP/session) : tools.call
builder.py       ← zip de distribution sélectif (cœur + plugins/services), garde-fou anti-secret
updater.py       ← MAJ inter-instances (pull/push) : manifeste+sha256, diff, backup, apply, rollback
docker_driver.py ← pilote Docker local (build/run/inspect/stop via subprocess)
deploy.py        ← cycle de vie d'un outil Docker (ensure_image, start/stop, proxy_base)
backup.py        ← sauvegarde SQLite quotidienne
routes.py        ← Blueprint unique (pages + API outils + services + réglages + i18n + audit + déploiement)
```

## Séparation cœur / plugins / services (versionnés, distribuables)
- **Dépôts séparés (submodules)** : chaque plugin/service est son PROPRE dépôt git
  (`bob-integration/bobitools-plugin-<type>` / `bobitools-service-<id>`), agrégé ici en
  **submodule** (`.gitmodules`, URLs **HTTPS** — SSH indispo en CI/serveur). Modèle du repo
  live `bobi.studio`. Cloner avec `--recurse-submodules` ; `install.sh` fait
  `git submodule update --init`. Un submodule initialisé = dossier normal → builder/updater
  et les registres (scan disque) fonctionnent sans changement.
- **`plugins/<type>/`** : outils (cf. ci-dessous), registre `app/plugins.py`.
- **`services/<id>/`** (RACINE, hors `app/`) : services globaux versionnés (≠ outils, pas au
  lanceur), registre `app/core_plugins.py`. Manifeste `manifest.json` (REQUIRED : `id`,`label`,
  `version` ; `settings_keys`/`nav_tab` optionnels). Module Python in-process exposant
  `boot()?` et `register_routes(bp)?`. Un service est AUTORITAIRE pour ses réglages
  (`settings_keys` → fusionnés dans `settings.DEFAULTS` via `core_plugins.all_settings_defaults`).
  Ex. `services/emberplus/` (provider Ember+). Le « moule IPG » (profil canonique rendant
  SNP et Neuron interchangeables au pupitre VSM) est documenté dans **`EMBERPLUS-IPG.md`** —
  À LIRE avant d'y toucher, en particulier le §1 sur le piège des images Docker périmées
  (code d'un plugin docker modifié sans bump de version = jamais déployé).
- **Distribution** : `app/builder.py` (+ CLI `tools/build_dist.py`) produit `dist/bobitools.zip`
  (code seul, jamais de secret/`.db`/`config_local.py`). `versions/` exclus du zip.
- **Mise à jour inter-instances** (modèle « Flotte » de Studio) : `app/updater.py` +
  `app/peers.py` (table `peers`) + routes `/api/update/*`, `/api/peers/*`, `/api/deploy/*`.
  Chaque instance a SON token (`update_token`) et un MODE SERVEUR (`update_server_enabled`,
  None = actif si token) ; chaque pair mémorise le token du pair (vide → le nôtre). `ping`
  est PUBLIC (identité seule, sert au scan). Pull/Push passent par un APERÇU (diff des
  manifestes) ; on n'applique que le cœur + les composants DÉJÀ installés sur la cible
  (`install_new` = opt-in), après contrôle des dépendances (`requirements.txt` du zip).
  `builder.identity()` re-tamponne `build_info.json` quand HEAD bouge (dépôt git). L'extraction
  PRÉSERVE l'état local (`config_local.py`, `*.db`, `static/uploads/`, `dist/`, `coffre.key`).
  Réglages → Système → Déploiement (sous-onglets Flotte / GitHub / Paquet). Artefacts (`dist/`,
  `build_info.json`, `deploy_info.json`, `UPDATE_PENDING`) gitignored ;
  `services/*/versions/` gitignored (comme les plugins).

## Modèle de plugin « outil »
```
plugins/<type>/
  plugin.json   ← type, label, version, runtime, description, nav{section,label,order},
                  badge{label,class,oklch}, ui{page_html,page_js,page_css},
                  docker{image,build,port,env_settings,shared_volumes}?  config_schema?
                  requires[{type,reason}]?  ← DÉPENDANCE DURE à un autre outil
  page.html     ← fragment UI monté pleine page
  page.js       ← window.BTTools["<type>"] = { mount(el, ctx), unmount() }
  page.css      ← optionnel
  backend.py    ← optionnel (inprocess) : def api(path, method, payload, ctx) -> data | (status, data)
  Dockerfile    ← optionnel (docker)
  i18n/<code>.json  ← optionnel (clés plugin.<type>.*)
  meta.json     ← optionnel (changelog/version)
  versions/<ver>/   ← archives (mécanisme repris de Studio)
```
### Inventaires partagés entre outils (patron maison)
Un outil PROPRIÉTAIRE d'un inventaire le publie dans son volume ; les consommateurs le
montent en **lecture seule** via `docker.shared_volumes` (l'app force `:ro` sur le volume
d'un autre outil). Avantage sur une API : le fichier reste lisible **conteneur du
propriétaire arrêté**. Propriétaires actuels : `switch_ports` (switchs), `nmos_parc` (parc
NMOS, contrat versionné `park.json`). Un consommateur DOIT vérifier `version` et refuser un
schéma inconnu plutôt que le lire au mieux.
- **`requires`** : `shared_volumes` est souple (Docker crée un volume VIDE si le
  propriétaire n'existe pas → le consommateur démarre et ne voit rien, indiscernable d'un
  inventaire vide). `requires` déclare la dépendance dure ; `plugins.missing_requirements()`
  la remonte dans `tool_status`, la barre d'état de l'outil l'affiche. **On avertit sans
  bloquer** : bloquer empêcherait l'outil d'expliquer lui-même ce qui lui manque.
- Migration d'un inventaire vers un nouveau propriétaire : reprise NON destructive (on lit,
  on n'écrit jamais chez l'ancien), dédoublonnage sur l'ADRESSE, et surtout republication
  des **clés historiques** de chaque consommateur (`compat`) — plusieurs outils indexent
  leurs données persistées dessus (salvos, répertoires de sauvegardes). Cf. `nmos_parc`.

Le `ctx` in-process expose `{ user, store, setting(key), audit(action, detail), tool_dir }`.
Le `store` est scopé à l'outil (table `plugin_store`). Côté UI, le SDK `ctx` (cf.
`static/scripts.js`) expose `{ type, runtime, toast, t, api(path,opts), store{...} }`.

## SDK front (`static/scripts.js`, namespace `window.BT`)
- `BT.openTool(type, hostEl)` : monte l'UI d'un outil (gère la barre d'état + start/stop
  pour les outils Docker). Utilisé identiquement par `tool_section.html` et `tool_focus.html`.
- `BT.toast`, `BT.fetchJSON`, `BT.esc`.

## Permissions & audit
- Rôles MODIFIABLES (table `roles`, Réglages → Général → Rôles, API `/api/roles`) : `ROLES`
  / `ROLE_LABELS` sont rechargés EN PLACE par `auth.recharger_roles()` (boot, reload plugins,
  chaque modif). `admin` verrouillé (a tout, toujours). Semés une fois depuis `ROLES_DEFAUT`.
  Décorateurs `require_login` / `require_perm(p)` / `require_tool` (login + `can_access_tool`).
- **Droits fins des outils** (`app/droits.py`, cf. sa docstring) : un plugin déclare
  `permissions` (→ `<type>.<id>`, distribuées UNE fois à leurs `default_roles`, réglage
  `perms_seen`) et `acl` {`resources`, `resolve`, `routes`} qui relie chaque route
  d'écriture à une permission + une ressource. Le CŒUR vérifie avant le proxy (Docker
  compris). Règles de PÉRIMÈTRE (table `access_rules`, Réglages → Outils → Périmètres) :
  écriture seule ; aucune règle citant une permission = le rôle suffit. Trois modes :
  `deny` (retire le geste à ceux qu'elle nomme ; l'emporte sur tout), `reserve` (ressources
  réservées à leurs bénéficiaires, reste inchangé) et `allow` (liste blanche sur tout l'outil).
  L'inconnu (ressource non désignée, attribut indisponible) tranche toujours vers le refus. L'écran chiffre l'effet avant
  d'enregistrer (`/api/access-rules/impact`).
  Les contrôleurs Ember+/SW-P-08 sont des appelants (`droits.who_controller(ip)`, passé en
  `principal=` à `tools.call`) soumis aux mêmes règles ; ils se DÉCLARENT une fois (table
  `controllers`, Réglages → Protocoles → Contrôleurs : nom + adresses/plages) et une règle les
  cite par `ctl:<id>` (une adresse brute reste acceptée). Les écritures du `store` générique
  passent aussi par le contrôle (chemins `store`, `store/{id}`, payload + `scope`). SDK : `ctx.can(perm, resource)`
  (in-process), `ctx.rights()` / `ctx.can([{perm, resource}])` (front). Le proxy transmet
  `X-BT-User`/`X-BT-Role`/`X-BT-Perms` au conteneur (informatif). Exemple complet :
  `plugins/switch_ports/plugin.json` + son `acl/resolve`.
- Accès par outil : setting JSON `tool_access = {type: {roles, users, groups}}`. admin → tout ;
  défaut (absent) = `DEFAULT_TOOL_ROLES` (admin+operator). POST partiel : une clé absente du
  corps garde sa valeur (l'écran de réglages n'envoie que `roles`+`groups`).
- **Groupes d'utilisateurs** (tables `user_groups` / `user_group_members`, API `/api/groups`,
  Réglages → Utilisateurs) : regroupement NOMMÉ, sans permission propre — un destinataire
  commun aux politiques de partage. Lecture ouverte à tout compte connecté (un outil doit
  pouvoir proposer « partager avec le groupe X ») ; écriture réservée à `settings.edit`.
  Côté plugin : `ctx.users()`, `ctx.groups()`, `ctx.my_groups()`, `ctx.has_perm(p)`.
- Audit : table `audit`, helper `audit_log`. Le dispatch journalise auto les appels non-GET.
  Un secret ne doit JAMAIS transiter par un chemin non-GET sans passer par `_redact`
  (masque token/password/secret/api_key, récursif) — cf. plugin `coffre`.

## Conventions de nommage
Convention **française** dans le code applicatif (commentaires, helpers). Le **système de
plugins** (`plugins.py`, SDK front) est en **anglais**, par dérogation — comme dans Studio.
SPDX / GPL-3.0-or-later en tête de chaque fichier.

## Vues / URLs
`/` lanceur · `/t/<type>` focus bookmarkable · `/tools/<section>` mode test · `/settings`
· `/aide` · `/setup` (création du 1er admin). API outils sous `/api/tools/<type>/…`
(`ui/{html,js,css}`, `lifecycle`, `store`, `access`, puis dispatch `<path>`).

## Réutilisé verbatim de Bobi.Studio
`app/i18n.py`, `static/css/{base,nav,theme-light,theme-studio}.css`, `LICENSE`. Le reste est
adapté (métier broadcast retiré). Référence d'origine : archive Bobi.Studio.
