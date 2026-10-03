# Changelog — Bobi.Tools

## 0.1.0 — 2026-06-20

Première version : socle d'hébergement d'outils dérivé de Bobi.Studio.

### Socle
- Application Flask (port 5000), SQLite, sessions, secret persistée.
- Auth : rôles `admin` / `operator` / `viewer` + **contrôle d'accès par outil** + **journal d'audit**.
- i18n FR/EN repris de Studio (catalogues fichiers + surcouche DB éditable).
- Thèmes `classic` / `studio` / `light` (CSS repris verbatim de Studio), marque client.
- Sauvegarde SQLite quotidienne + manuelle.

### Système d'outils-plugins (modèle hybride)
- Registre d'outils versionnés (`plugins/<type>/`), import/export `.bobitool`, activation de versions.
- `runtime: "inprocess"` (backend.py en process ou front-pur) **et** `runtime: "docker"`
  (conteneur local, build depuis Dockerfile, proxy).
- Dispatch unifié `/api/tools/<type>/<path>` ; stockage générique par outil (`plugin_store`).
- Vues : lanceur, mode test par section, vue focus plein écran **bookmarkable** (`/t/<type>`).

### Outils d'exemple
- `notes` — front-pur (gabarit minimal).
- `api_explorer` — in-process avec `backend.py` (requêtes HTTP côté serveur).
- `switch_ports` — `runtime: docker` (Dockerfile + service embarqué + simulateur), gabarit Docker.
