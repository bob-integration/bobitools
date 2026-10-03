---
name: Bobi.Tools
description: Plateforme d'hébergement d'outils-plugins (in-process ou Docker local), trois lumières (classic, light, studio). Charte partagée verbatim avec Bobi.Studio.
colors:
  slate-signal: "#7aa2c8"
  slate-signal-hover: "#95b7d6"
  daylight-indigo: "#4f46e5"
  daylight-indigo-hover: "#6366f1"
  studio-amber: "#f59e0b"
  studio-amber-hover: "#fbbf24"
  default-bg: "#14161a"
  default-bg-elev: "#1b1e23"
  default-bg-input: "#181a1f"
  default-bg-hover: "#23262c"
  default-bg-selected: "#262a31"
  default-border: "#2a2d34"
  default-border-soft: "#23262c"
  default-text: "#d4d6da"
  default-text-muted: "#8a8d94"
  default-text-strong: "#f0f1f3"
  light-bg: "#f6f7f9"
  light-bg-elev: "#ffffff"
  light-border: "#e5e7eb"
  light-text: "#1f2937"
  light-text-muted: "#6b7280"
  light-text-strong: "#111827"
  studio-bg: "#18181b"
  studio-bg-elev: "#23232a"
  studio-text-strong: "#fafafa"
  status-running-fg: "#7ab98a"
  status-running-bg: "#1d2e23"
  status-stopped-fg: "#d07a82"
  status-stopped-bg: "#322023"
  status-warning-fg: "#c4a667"
  status-warning-bg: "#2e2a1e"
  status-unknown-fg: "#8a8d94"
  status-unknown-bg: "#25272c"
  node-source: "oklch(0.72 0.16 60)"
  node-switch: "oklch(0.72 0.15 260)"
  node-rx: "oklch(0.70 0.15 150)"
  sdn-warn: "oklch(0.65 0.20 25)"
  port-trunk: "oklch(0.62 0.19 300)"
typography:
  display:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "1.35em"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "0.95em"
    fontWeight: 600
    lineHeight: 1.4
  label:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "0.78em"
    fontWeight: 600
    letterSpacing: "1px"
  body:
    fontFamily: "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace"
    fontSize: "0.88em"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  xs: "3px"
  sm: "4px"
  md: "6px"
  lg: "8px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "6px"
  md: "12px"
  lg: "16px"
  xl: "20px"
components:
  card:
    backgroundColor: "{colors.default-bg-elev}"
    textColor: "{colors.default-text}"
    rounded: "{rounded.md}"
    padding: "16px"
  input:
    backgroundColor: "{colors.default-bg-input}"
    textColor: "{colors.default-text}"
    rounded: "{rounded.sm}"
    padding: "6px 9px"
  badge-running:
    backgroundColor: "{colors.status-running-bg}"
    textColor: "{colors.status-running-fg}"
    rounded: "3px"
    padding: "2px 8px"
  badge-stopped:
    backgroundColor: "{colors.status-stopped-bg}"
    textColor: "{colors.status-stopped-fg}"
    rounded: "3px"
    padding: "2px 8px"
---

# Design System: Bobi.Tools

Bobi.Tools partage **la charte de Bobi.Studio, verbatim** : `base.css`, `nav.css`,
`theme-light.css`, `theme-studio.css` sont identiques au octet près. Mêmes tokens, mêmes
thèmes, même grammaire. Objectif explicite : un utilisateur de Studio se retrouve
immédiatement chez lui. Le présent DESIGN.md reprend donc le système de Studio et ne
documente en propre que les **surfaces spécifiques à Bobi.Tools** (lanceur, shell d'outils,
barre d'état Docker) — qui n'utilisent **que** les tokens existants, aucun token nouveau.

## 1. Overview

**Creative North Star : « Le poste d'outils »**

Une plateforme qui s'efface derrière l'outil. Le lanceur amène à l'outil en un clic ; la vue
focus donne tout l'écran à l'outil ; le runtime (in-process ou conteneur Docker local) est un
détail technique discret, jamais une étape. Le design SERT l'accès à l'outil, il ne se met
jamais devant.

Trois lumières, un seul produit. **classic** (dark neutre, accent Slate Signal) est le poste
standard. **light** (Daylight, indigo) pour le bureau et la lumière du jour. **studio** (dark
warm, amber) réchauffe le dark. Tokens structurels partagés ; seules surface et accent varient.

Famille de référence : **Linear, Grafana, Raycast, Tailscale**. Rejets : SaaS générique
cream/violet, Bootstrap admin, gamer/cyberpunk neon, glassmorphism.

**Caractéristiques clés :**
- Trois thèmes, un seul jeu de tokens structurels (partagés avec Studio).
- États d'outil robustes (Docker `running` / `stopped` / inconnu), jamais codés par la couleur seule.
- Densité d'information aérée par l'espacement et la hiérarchie typographique.
- Monospace réservée aux valeurs techniques (runtime, ports, états), jamais pour la prose.
- Vocabulaire d'interface entièrement français (système de plugins en anglais, par dérogation).

## 2. Colors

Hérité de Studio sans modification. Trois rôles d'accent (un par thème), un vocabulaire de
statuts partagé. Bobi.Tools n'a **pas** les couleurs de mode broadcast de Studio
(`mode-receiver`, etc.) : un outil se distingue par son **badge** propre (`badge {label,
class, oklch}` déclaré dans son manifeste), dont le bloc `<style>` est généré dans
`layout.html` en OKLCH.

- **Accent** : Slate Signal `#7aa2c8` (classic) · Daylight Indigo `#4f46e5` (light) · Studio
  Amber `#f59e0b` (studio). Liens, focus, état actif, carte d'outil survolée. ≤10 % de la surface.
- **Statuts** : Running vert (`#7ab98a`/`#1d2e23`), Stopped rouge (`#d07a82`/`#322023`),
  Warning amber (`#c4a667`/`#2e2a1e`), Unknown gris. Rôles fixes dans les trois thèmes.
- **Couleurs métier `monitoring_2110`** (topologie multicast, OKLCH, chroma mesuré — analogue
  aux `mode-*` de Studio) : `node-source` ambre `oklch(0.72 0.16 60)`, `node-switch` bleu
  `oklch(0.72 0.15 260)`, `node-rx` vert `oklch(0.70 0.15 150)`, `sdn-warn` rouge
  `oklch(0.65 0.20 25)`. Utilisées en **bordure pleine + teinte de fond** (`color-mix` sur
  `--bg-input`) pour typer les nœuds — **jamais en side-stripe**. `node-source` sert aussi à
  l'état *unknown*.
- **Couleur métier `switch_ports`** : `port-trunk` violet `oklch(0.62 0.19 300)` — marque un port
  **trunk** (membre tagué) sur la faceplate / vue photo / liste, distinct du vert « actif ». Même
  grammaire que les `node-*` (bordure pleine + badge teinté `color-mix`, jamais side-stripe).
  Déclarée en var locale `--port-trunk` dans `plugins/switch_ports/page.css`.

### Named Rules

**La règle Statut > Couleur.** L'état d'un outil Docker (actif/arrêté) est toujours couplé à
un label texte, jamais à la pastille seule. Lisible en N&B.

**La règle Trois Lumières.** Toute couleur structurelle existe dans les trois thèmes avec le
même rôle. Pas de couleur propre à un seul thème, sauf l'accent.

**La règle OKLCH pour les ajouts.** Les badges d'outils sont définis en OKLCH (chroma réduit).
Tout nouveau token suit la même règle.

## 3. Typography

**UI Font :** Inter (`'Segoe UI', system-ui, -apple-system, sans-serif`). **Mono :**
`ui-monospace` (`'JetBrains Mono', 'SF Mono', Menlo, Consolas`). Une seule famille porte tout
l'UI ; la monospace est confinée aux valeurs techniques (runtime, ports, états, timestamps).

### Hierarchy

- **Display** (h1, 600, `1.35em`, letter-spacing `-0.01em`) : titre de page (nom du lanceur, titre d'outil en focus).
- **Title de section** (h2, 600, `0.78em`, UPPERCASE, letter-spacing `1px`, `text-muted`) :
  rubriques du lanceur. C'est la convention Studio, pas un eyebrow décoratif.
- **Body** (400, `13.5–14px`, line-height `1.5`) : base. Cap à 65–75ch sur la prose (aide).
- **Mono** (400, `0.88em`) : valeurs techniques uniquement.

**La règle Monospace Confinée.** Le runtime, les ports, les états sont en mono ; les titres,
labels, descriptions et boutons sont en Inter.

## 4. Elevation

Majoritairement **flat**. La profondeur passe par le contraste de fond (`bg` → `bg-elev` →
`bg-input`) et les borders 1px, pas par les ombres. Pas de glow, pas de relief 3D, **pas de
glassmorphism** (`backdrop-filter: blur` proscrit, y compris sur la carte de login). L'ombre
n'existe que via `var(--shadow)` (none en classic, discrète en light/studio).

## 5. Components (spécifiques Bobi.Tools)

Tout le reste (boutons `.btn-*`, formulaires, alertes, onglets, topnav, badges de statut) suit
**à l'identique** le système de Studio. Surfaces propres à Bobi.Tools :

### Carte d'outil — lanceur (`.tool-card`)
- **Conteneur :** `<article>` (pas un `<a>`), `bg-elev` + border 1px `border-soft` + radius `md`,
  padding `16px`, `position: relative`. **Une seule action primaire** : le titre est un lien
  étiré (`::after` en `inset: 0`) qui couvre toute la carte → ouvre `/t/<type>`.
- **Pas d'ancres imbriquées.** Les actions secondaires (« Tester ») sont des liens posés
  au-dessus (`position: relative; z-index: 1`), jamais imbriqués dans le lien primaire.
- **Hover :** border-color → `accent`, fond → `bg-hover`. Transition 120ms. **Pas de
  `translateY`, pas de scale** (règle Flat, cohérence avec les boutons Studio).
- **Runtime :** petit tag mono (`tc-rt`), `text-muted`, border soft. Valeur technique discrète.
- **Grille :** `repeat(auto-fill, minmax(240px, 1fr))`, gap `14px`.

### Shell d'outil & vue focus (`.focus-head`, `.bt-tool-host`)
- **Focus head :** barre fine `bg`/border-bottom, retour menu `text-muted` → accent au hover,
  titre `1.1em`, tag runtime mono. Chrome minimal : l'outil prend la suite de l'écran.
- **Zone de montage :** vide = `border dashed border-soft`, `text-muted`, centré.

### Barre d'état Docker (`.bt-tool-bar`)
- `bg-soft` + border-soft + radius small, font `0.88em`. État en mono `text-muted`.
- **Pastille `.bt-dot` + label texte** : `running` (vert) / `stopped` (rouge) / défaut (gris).
  Jamais la couleur seule.

### Toasts (`.toast`)
- `info` = `accent-soft`/`accent` · `warning` = tokens warning · `error` = tokens stopped.
  Radius small, ombre `var(--shadow)`, en bas à droite.

### Cases à cocher & interrupteurs (`static/css/controls.css`)
**Jamais de case à cocher blanche native.** Deux apparences, choisies par la sémantique :
- **Feature-toggle** (option binaire « Activer… / Vérifier… / Inclure… / Mode… ») →
  **interrupteur iOS** : classe `.ios-toggle` sur l'`<input type="checkbox">` (track + thumb,
  défini verbatim dans `base.css`, partagé avec Studio). Pour un groupe horizontal, label
  `.toggle-inline`.
- **Sélection multiple** (listes, matrices, lignes à cocher — ex. choix de switchs, lignes à
  approuver, matrice de rôles par outil) → **case restylée** (carré arrondi + coche accent).
  C'est le **défaut global** : `controls.css` restyle toute `<input type="checkbox">` sans
  `.ios-toggle`, sur toutes les pages et tous les plugins. `controls.css` est chargé par
  `layout.html` après `base.css` (qui reste verbatim Studio) ; il n'utilise que des tokens
  existants. Un interrupteur sur une liste de sélection suggérerait un effet immédiat : on
  réserve `.ios-toggle` aux vrais réglages on/off.

## 6. Do's and Don'ts

### Do
- **Do** n'utiliser **que les tokens existants** dans `tools.css` — aucun token nouveau, pour
  rester cohérent sous les trois thèmes.
- **Do** une **seule action primaire** par carte d'outil (lien étiré), actions secondaires au-dessus.
- **Do** coupler tout état d'outil à un **label texte** (pastille + « actif / arrêté »).
- **Do** réserver la **monospace** aux valeurs techniques (runtime, ports, états).
- **Do** garder le vocabulaire d'interface **français**.
- **Do** poser `.ios-toggle` sur tout feature-toggle (option binaire) ; laisser les cases de
  sélection multiple au défaut restylé de `controls.css`.

### Don't
- **Don't** imbriquer des ancres (`<a>` dans `<a>`) dans les cartes — HTML invalide, cible ambiguë.
- **Don't** animer une carte par `translateY`/scale au hover : la grammaire est flat (border + fond).
- **Don't** introduire de **glassmorphism** (`backdrop-filter: blur`) — y compris la carte de login.
- **Don't** définir des **styles inline volumineux** dans les templates : les styles du shell
  d'outils vivent dans `static/css/tools.css`.
- **Don't** dépendre de la **couleur seule** pour un état ; toujours un label texte.
- **Don't** introduire de **token nouveau** dans `tools.css` ni de couleur propre à un seul thème.
- **Don't** laisser une **case à cocher blanche native** : le défaut de `controls.css` la
  remplace partout. Ne pas mettre `.ios-toggle` sur une case de sélection multiple (l'interrupteur
  y suggère un effet immédiat au lieu d'une sélection).
