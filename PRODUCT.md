## Register

product

## Users

Administrateurs, ingénieurs et techniciens qui hébergent et utilisent des **outils** sur une
instance Bobi.Tools partagée. Trois profils, calqués sur les rôles applicatifs
(`admin / operator / viewer`) :

- **Administrateur** : installe, active et configure les outils (paquets `.bobitool`), gère les
  accès par outil, l'apparence et les sauvegardes. Sessions de configuration, pas d'antenne.
- **Opérateur** : se sert des outils au quotidien (switch_ports, monitoring 2110…). Veut
  ouvrir un outil et travailler immédiatement, sans chrome qui distrait. Souvent plusieurs
  outils dans la journée.
- **Viewer** : accès en lecture à un sous-ensemble d'outils, via le portail. Consulte, ne
  configure pas.

Usage LAN interne, souvent en HTTP clair. Plusieurs utilisateurs simultanés. Un outil peut
être in-process (Flask) ou un conteneur Docker local démarré à la demande.

## Product Purpose

Héberger des outils sous forme de **plugins versionnés** derrière une seule charte, une seule
authentification et un seul système d'accès. L'interface répond à trois questions, par ordre
de priorité décroissante :

1. **Quel outil je veux, et est-il prêt ?** (lanceur : trouver l'outil, voir son état) — priorité 1.
2. **Travailler dans l'outil sans friction.** (vue focus plein écran, démarrage Docker transparent).
3. **Installer, configurer, donner accès.** (réglages, paquets, accès par outil — moins fréquent).

Succès = un opérateur ouvre le bon outil et travaille en moins de trois secondes, sans se
soucier du runtime (in-process ou Docker). Le chrome de la plateforme s'efface derrière l'outil.

## Brand Personality

Plateforme d'outils sobre et professionnelle, héritée de **Bobi.Studio** : un utilisateur de
Studio se retrouve immédiatement chez lui. Famille de référence : **Linear, Grafana, Raycast,
Tailscale** — dense mais lisible, typographie soignée, états qui ressortent, zéro décoration
qui distrait du travail.

Trois mots : **fiable, précis, réactif**.

Voix UX française cohérente avec le code (dérogation anglaise pour le seul système de plugins).
Vocabulaire technique assumé (runtime, conteneur, in-process, paquet). Messages d'erreur
actionnables.

## Anti-references

- **SaaS générique** : cream + violet, grandes cards arrondies, hero-metrics à gradient,
  grilles de cartes identiques icône + titre + texte à l'infini.
- **Bootstrap admin template** : sidebar bleu corporate, badges multicolores partout, tableaux
  zébrés.
- **Gamer / cyberpunk neon** : noir profond + cyan/magenta, glow, esthétique « hacker movie ».
- **Glassmorphism** : surfaces translucides, `backdrop-filter: blur`, gradients radiaux. Studio
  a déjà retiré son thème glass (Aurora) ; Bobi.Tools ne le réintroduit pas.
- **Marketplace grand public** : le lanceur n'est pas une vitrine à convertir un visiteur, c'est
  un poste de travail pour un utilisateur authentifié qui sait ce qu'il cherche.

## Design Principles

1. **L'outil prime sur le chrome.** En vue focus (`/t/<type>`) le chrome se réduit à un titre +
   retour menu ; l'outil occupe tout l'écran. Le lanceur amène à l'outil le plus vite possible.
2. **Trois lumières, un seul produit.** Les thèmes `classic` (dark neutre, défaut), `light`
   (Daylight, indigo) et `studio` (dark warm, amber) sont le même produit dans trois éclairages.
   Tokens structurels partagés ; seules surface et accent changent. Repris verbatim de Studio.
3. **Densité lisible.** Information structurée par la hiérarchie typographique et le rythme
   d'espacement. Jamais un écran qui suffoque, jamais un écran qui cache l'état d'un outil.
4. **Zéro ambiguïté sur l'état.** Un outil Docker arrêté/démarré n'est jamais identifiable par
   la couleur seule : pastille **couplée à un label texte** (« actif / arrêté »). Lisible en N&B.
5. **Cohérence française.** Labels, statuts, actions, messages d'erreur — une seule langue,
   alignée sur la convention du code applicatif.
6. **Marque produit + personnalisation client.** « Bobi.Tools » reste affichée partout (nav,
   footer, login) ; la personnalisation client (nom système, organisation, logo, lieu) s'ajoute
   par-dessus via Réglages → Apparence, sans masquer la marque produit.

## Accessibility & Inclusion

- WCAG AA visé sur le contraste texte et les états de focus.
- Statuts critiques toujours doublés d'un label texte (couleur seule insuffisante).
- Cibles cliquables franches ; une carte d'outil = une action primaire claire (pas d'ancres
  imbriquées, pas d'affordance ambiguë).
- Aucune animation décorative ; le réflexe `prefers-reduced-motion` s'applique même sans
  contrainte formelle.
