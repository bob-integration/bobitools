# Déploiement de Bobi.Tools sur Debian

Ce guide décrit l'installation de Bobi.Tools sur un serveur **Debian 12 (bookworm)** ou
ultérieur, à partir du **paquet `bobitools.zip`** téléchargé depuis une instance existante
(Réglages → Déploiement → *Télécharger le paquet*).

Le paquet est **autonome** : il contient le cœur, tous les plugins/outils et les services
inclus au moment du build. Aucun accès git n'est requis sur le serveur cible — contrairement
au clone décrit dans le `README.md`, les outils sont déjà embarqués dans le zip.

L'application s'installe dans `/opt/bobitools` et écoute sur le **port 5000**.

---

## 1. Prérequis système

En root (ou via `sudo`) :

```bash
apt update
apt install -y python3 python3-venv unzip
```

- **Docker** est optionnel mais recommandé : les outils en `runtime: docker` ne sont
  disponibles que s'il est présent. Les outils `inprocess` fonctionnent sans.

  ```bash
  apt install -y docker.io
  systemctl enable --now docker
  ```

---

## 2. Déposer et extraire le paquet

Copier `bobitools.zip` sur le serveur (scp, clé USB, etc.), puis :

```bash
mkdir -p /opt/bobitools
unzip bobitools.zip -d /opt/bobitools
cd /opt/bobitools
```

> **Mise à jour d'une instance existante** : voir la section 7 — n'écrasez pas la base de
> données ni `config_local.py`.

---

## 3. Installation automatique

Le script `install.sh` (livré dans le paquet) crée le venv, installe les dépendances,
pose le service systemd et initialise la base. À lancer **en root** :

```bash
cd /opt/bobitools
bash install.sh
```

Le script :

1. ignore l'étape submodules (pas de dépôt git dans le paquet — c'est normal, les outils
   sont déjà présents) ;
2. crée `./venv` et installe `requirements.txt` ;
3. signale si Docker est absent ;
4. crée `config_local.py` depuis le modèle (les défauts conviennent) ;
5. installe et active le service systemd `bobitools` ;
6. initialise `db_bobitools.db` si elle n'existe pas.

---

## 4. Créer le premier administrateur

Deux possibilités, au choix :

```bash
# En ligne de commande :
./venv/bin/python tools/create_admin.py
```

ou bien laisser **l'assistant web `/setup`** créer le premier compte admin au premier accès
(voir étape 6).

---

## 5. Démarrer le service

```bash
systemctl start bobitools
systemctl status bobitools     # vérifier que le service est actif
```

Le service est déjà activé au démarrage (`systemctl enable`) par `install.sh`.

---

## 6. Accéder à l'application

```
http://<adresse-du-serveur>:5000
```

Au premier accès, si aucun admin n'existe, l'assistant `/setup` vous invite à en créer un.

---

## 7. Mettre à jour une instance existante

Le paquet ne contient **jamais** de secret ni d'état local (base `*.db`, `config_local.py`,
`static/uploads/` sont exclus du zip). Pour mettre à jour sans rien perdre :

```bash
systemctl stop bobitools
# Extraire par-dessus : remplace le code, préserve db/config/uploads (absents du zip)
unzip -o bobitools.zip -d /opt/bobitools
cd /opt/bobitools
./venv/bin/pip install -r requirements.txt --quiet   # si les dépendances ont changé
systemctl start bobitools
```

> **Alternative sans fichier** : la mise à jour **inter-instances** (Réglages → Déploiement)
> permet de tirer (`pull`) ou pousser (`push`) une release entre deux instances qui partagent
> le même *token de mise à jour*, avec backup et rollback automatiques. Voir `app/updater.py`.

---

## 8. Configuration optionnelle

Surcharges dans `config_local.py` (non versionné, préservé par les mises à jour) :

```python
HTTP_PORT    = 5000                              # port d'écoute
DB_PATH      = "/opt/bobitools/db_bobitools.db"  # emplacement de la base
DOCKER_LABEL = "bobitool"                         # préfixe des conteneurs d'outils
```

Redémarrer après modification : `systemctl restart bobitools`.

---

## 9. Exploitation

```bash
systemctl restart bobitools          # redémarrer
systemctl stop bobitools             # arrêter
journalctl -u bobitools -f           # journal systemd en direct
tail -f /opt/bobitools/bobitools.log # journal applicatif
```

- La base SQLite est sauvegardée automatiquement chaque jour (thread de backup au lancement).
- Le port 5000 doit être joignable depuis les postes clients (ouvrir le pare-feu si besoin).

---

## Dépannage

| Symptôme | Piste |
|---|---|
| `python3 introuvable` | `apt install -y python3 python3-venv` |
| Outils `docker` indisponibles | Docker absent → `apt install -y docker.io && systemctl enable --now docker`, puis redémarrer le service |
| `Internal Server Error` après une mise à jour | Redémarrer le service (`systemctl restart bobitools`) : du code obsolète peut rester chargé en mémoire alors que les templates sont déjà à jour |
| Service inactif | `journalctl -u bobitools -e` pour lire l'erreur de démarrage |
| Port 5000 occupé | Définir `HTTP_PORT` dans `config_local.py` |

---

Licence : GNU GPL v3 (ou ultérieure) — voir `LICENSE`.
