# Sécurité — Bobi.Tools

Bobi.Tools est un outil d'**exploitation sur réseau local** (régie, car, plateau). Il pilote du
matériel de production. Ce document dit ce qu'il protège, ce qu'il ne protège pas, et comment
l'installer pour que ces limites ne deviennent pas des failles.

## Signaler une vulnérabilité

Écrire à **contact@bob-i.tv** (objet : « Bobi.Tools — sécurité »). Merci de ne pas ouvrir de
ticket public avant notre réponse.

## Modèle d'exploitation

- **HTTP en clair par défaut** (port 5000). Hors d'un réseau de confiance, placer Bobi.Tools
  derrière un reverse-proxy TLS et régler `trusted_proxies` (cf. `main.py`, ProxyFix) : sans lui,
  l'adresse cliente n'est pas crue, et c'est voulu.
- **Comptes et rôles** : sessions signées, rôles modifiables, droits fins par outil et règles de
  périmètre (Réglages → Général → Rôles, Réglages → Outils → Périmètres). Le CŒUR vérifie chaque
  écriture avant de la transmettre à un outil.
- **Outils en conteneur** : ils écoutent sur `127.0.0.1` et ne sont joignables qu'à travers le
  cœur. Ils n'ont pas d'authentification propre : ne publiez jamais leur port sur le réseau.

## Protocoles de contrôle sans authentification

**Ember+**, **SW-P-08** et **TSL 5.0** ne transportent aucune identité : c'est la nature de ces
protocoles. Bobi.Tools les sert pour les contrôleurs broadcast (VSM…), qui peuvent ainsi ÉCRIRE
sur le matériel.

- Renseignez les **listes d'adresses autorisées à écrire** (`emberplus_write_allow`,
  `swp08_vlan_write_allow`). Une liste vide n'impose aucune restriction ; l'interface le signale.
- Déclarez les contrôleurs (Réglages → Protocoles → Contrôleurs) : les règles de périmètre
  s'appliquent alors aussi à leurs ordres.
- Placez ces ports sur un **VLAN de contrôle isolé**.

## Mise à jour entre instances

`/api/update/ping` est public : il ne livre qu'une identité (nom, version), nécessaire à la
découverte. Le code (`manifest`, `download`) et l'ordre de mise à jour (`apply`) exigent le
**mode serveur** et le **jeton** de l'instance. Le zip est vérifié (sha256) avant d'être appliqué.

## Secrets conservés

- **Coffre** (plugin `coffre`) : AES-256-GCM, clé dans `coffre.key` (créée en 0600, jamais
  versionnée ni distribuée). **Sauvegardez cette clé à part** : la perdre rend les secrets
  illisibles ; la voler avec la base les rend lisibles.
- **Mots de passe SMTP, BMC et autres identifiants d'équipements** : stockés en clair dans la
  base SQLite, masqués dans l'interface. Protégez la base et ses sauvegardes (`backups/`) comme
  des secrets.
- **Journal d'audit** : les valeurs sensibles (token, password, secret, api_key) sont masquées
  avant journalisation.

## Outils à usage puissant

Certains outils donnent volontairement accès au matériel au plus bas niveau : CLI brute sur les
switchs, console de commandes des caméras, relais HTTP de l'explorateur d'API (garde contre la
boucle locale, le link-local et les métadonnées ; le réseau local reste joignable par conception).
Réservez-les, par les rôles et les périmètres, à qui en a l'usage.
