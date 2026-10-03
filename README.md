# Bobi.Tools

Hébergement web d'**outils**, partagés et installés sous forme de **plugins**. Même socle,
même charte et même i18n que **Bobi.Studio** — un utilisateur de l'un se retrouve
immédiatement sur l'autre — mais des fonctions différentes : chaque outil (pilotage de
switch, explorateur d'API, utilitaires…) arrive au fil du temps comme un paquet `.bobitool`
qu'on dépose à la demande.

## Concept

Un **outil** est un dossier dans `plugins/<type>/` avec un manifeste `plugin.json` et une UI.
Le manifeste déclare son **runtime** :

- **`inprocess`** — l'outil tourne dans l'application : front-pur, ou avec un petit
  `backend.py` Python chargé en process. Idéal pour les utilitaires légers.
- **`docker`** — l'outil tourne dans un **conteneur Docker isolé** (dépendances bundlées).
  L'app sert son UI et **proxifie** les appels vers le conteneur. Idéal pour les outils
  réseau/lourds ou nécessitant des secrets côté serveur.

Quel que soit le runtime, l'UI parle toujours à `/api/tools/<type>/<path>` — elle ignore
où tourne la logique.

## Démarrage

**En une ligne**, sur une Debian ou une Ubuntu vierge (en root) :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh)
```

Le script installe ce qui manque (git, python3-venv, et Docker si vous l'acceptez), clone
Bobi.Tools avec ses plugins dans `/opt/bobitools`, installe et démarre le service, puis indique
l'adresse de l'assistant de premier accès. Options : `--dir`, `--ref`, `--docker` / `--no-docker`,
`-y` (cf. `get.sh --help`).

**À la main** :

Chaque plugin et chaque service est un **dépôt séparé**, agrégé ici en **submodule git**
(cf. `.gitmodules`) — comme Bobi.Studio. Cloner **avec les submodules** :

```bash
git clone --recurse-submodules https://github.com/bob-integration/bobitools /opt/bobitools
cd /opt/bobitools
bash install.sh                       # init submodules + venv + dépendances + service (+ Docker)
./venv/bin/python tools/create_admin.py   # (ou via l'assistant /setup au 1er accès web)
systemctl start bobitools             # ou : ./venv/bin/python main.py
```

Sur un clone existant sans submodules : `git submodule update --init --recursive`
(fait automatiquement par `install.sh`). Accès : `http://<hôte>:5000`.

## Structure

```
main.py            ← point d'entrée Flask (port 5000) + backup quotidien
app/               ← socle : routes, plugins (registre d'outils), auth, settings, i18n,
                     database, backup, docker_driver, deploy (cycle de vie conteneur)
templates/         ← layout, login, home (lanceur), tool_section (test), tool_focus (plein
                     écran bookmarkable), settings, aide
static/            ← scripts.js (SDK window.BT), css/ (base, nav, themes, tools), uploads/
plugins/<type>/    ← UN SUBMODULE PAR OUTIL (bobitools-plugin-<type> : notes, switch_ports…)
services/<id>/     ← UN SUBMODULE PAR SERVICE (bobitools-service-<id> : emberplus)
i18n/              ← catalogues fr.json / en.json (clés cœur)
tools/             ← create_admin.py, build_dist.py
```

Les plugins/services ne sont **plus dans ce dépôt** : ils vivent dans
`bob-integration/bobitools-plugin-<type>` et `bobitools-service-<id>`, versionnés et
distribués indépendamment. Le builder (`tools/build_dist.py`) et l'updater agrègent les
fichiers présents sur disque (un submodule initialisé est un dossier normal).

## Vues & URLs

- `/` — **lanceur** : cartes des outils accessibles, groupées par section.
- `/t/<type>` — **vue focus** (bookmarkable) : l'outil en plein écran, chrome minimal.
- `/tools/<section>` — **mode test** : onglets pour basculer entre outils d'une section.

## Permissions

Trois rôles — `admin` / `operator` / `viewer` (permissions `tools.manage` / `tools.use` /
`settings.edit`) — **plus** un contrôle d'accès **par outil** (qui peut ouvrir quoi) et un
**journal d'audit** par outil (qui a fait quoi). Réglages → Plugins / Journal.

## Créer un outil

Voir **Aide** dans l'app, et `plugins/switch_ports/help.md` (gabarit Docker complet).
Le minimum : `plugin.json` + `page.html` + `page.js` (`window.BTTools["<type>"] =
{ mount(el, ctx), unmount() }`). Voir `plugins/notes/` (front-pur) et
`plugins/api_explorer/` (avec `backend.py`).

## Licence

Copyright (C) 2026 BOBI SAS, France — Auteur : Cyril Mazouer, pour le compte de BOBI SAS.
Logiciel libre sous **GNU GPL v3** (ou ultérieure) ; voir [`LICENSE`](LICENSE).

Développé avec l'assistance de Claude (Anthropic) comme outil de génération de code, sous
la direction et la supervision de Cyril Mazouer.
