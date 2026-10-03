# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""
Base SQLite de Bobi.Tools. Un seul fichier (`DB_PATH`) porte tout l'état : réglages,
utilisateurs, surcouche i18n, stockage générique des outils (`plugin_store`) et journal
d'audit. Une connexion fraîche par appel (`get_db()`) → sûr depuis les threads de fond.

Dérivé du socle générique de Bobi.Studio (tables containers/proxmox retirées).
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from .config import DB_PATH

log = logging.getLogger(__name__)

AUDIT_RETENTION = 5000   # nb max de lignes d'audit rendues par défaut


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Anti « database is locked » : attendre le verrou jusqu'à 5 s (plusieurs threads
    # de fond ouvrent chacun leur connexion sur le même fichier).
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'viewer',
            created_at    TEXT,
            prenom        TEXT,
            nom           TEXT,
            email         TEXT,
            lang          TEXT DEFAULT 'fr'
        )''')
        # Migration douce : colonnes ajoutées sur une DB pré-existante.
        ucols = [r[1] for r in db.execute("PRAGMA table_info(users)")]
        for _c in ("prenom", "nom", "email"):
            if _c not in ucols:
                db.execute(f"ALTER TABLE users ADD COLUMN {_c} TEXT")
        if "lang" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'fr'")
        # Thème PERSONNEL (comme la langue). Vide = défaut du système (setting `theme`).
        # Jeton GitHub PERSONNEL (catalogue) : élargit le quota d'API pour qui en a besoin. Par
        # utilisateur, pas par site : un jeton est une identité, pas un secret partagé.
        if "gh_token" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN gh_token TEXT DEFAULT ''")
        if "theme" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT ''")

        # Groupes d'utilisateurs : un simple regroupement nommé, sans permission propre.
        # Un groupe ne DONNE rien par lui-même — il sert de destinataire commun aux
        # politiques de partage (accès par outil, coffres d'identifiants…), pour éviter
        # d'énumérer les utilisateurs un par un dans chaque politique.
        # Table nommée `user_groups` et non `groups` : GROUPS est un mot-clé SQL.
        db.execute('''CREATE TABLE IF NOT EXISTS user_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at  TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS user_group_members (
            group_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            PRIMARY KEY (group_id, user_id)
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_ugm_user ON user_group_members(user_id)")

        # Surcouche de traductions éditée via l'UI (i18n) : appliquée PAR-DESSUS les
        # catalogues fichiers, donc persistante à travers les sync git (qui écrasent
        # les *.json versionnés). PK (lang, key) → une valeur par couple.
        db.execute('''CREATE TABLE IF NOT EXISTS i18n_overrides (
            lang  TEXT NOT NULL,
            key   TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (lang, key)
        )''')

        # Stockage générique d'un outil (presets, configs, notes, historique…) sans
        # schéma DB dédié : (type, scope, name) → value JSON. Repris de Studio.
        db.execute('''CREATE TABLE IF NOT EXISTS plugin_store (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL,
            scope      TEXT NOT NULL DEFAULT '',
            name       TEXT NOT NULL,
            value      TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )''')

        # Journal d'audit : qui a fait quoi, dans quel outil. Une ligne par action
        # sensible (bascule de port, requête proxifiée, démarrage de conteneur…).
        db.execute('''CREATE TABLE IF NOT EXISTS audit (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT,
            user_id  INTEGER,
            username TEXT,
            tool     TEXT,
            action   TEXT,
            detail   TEXT
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit(tool)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit(user_id)")

        # Rôles MODIFIABLES (comme les « habilitations » de Bobi.Studio). `permissions` =
        # liste JSON EXPLICITE (jamais « tout sauf ») : une permission nouvelle n'est donnée
        # à personne, sauf les rôles par défaut que son plugin désigne (cf. auth.py). Semée
        # une seule fois depuis auth.ROLES_DEFAUT : une permission retirée ne revient pas.
        db.execute('''CREATE TABLE IF NOT EXISTS roles (
            id          TEXT PRIMARY KEY,
            label       TEXT,
            permissions TEXT NOT NULL DEFAULT '[]',
            builtin     INTEGER NOT NULL DEFAULT 0
        )''')
        # Règles de PÉRIMÈTRE : qui (users/groups/roles) peut exercer quelles permissions d'un
        # outil, sur quelles ressources (`scope`, défini par l'outil : switch, ports…). Elles
        # n'autorisent qu'en écriture et n'interdisent jamais rien par elles-mêmes : tant
        # qu'aucune règle ne cite une permission, le rôle suffit (cf. app/droits.py).
        db.execute('''CREATE TABLE IF NOT EXISTS access_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tool       TEXT NOT NULL,
            perms      TEXT NOT NULL DEFAULT '[]',
            principals TEXT NOT NULL DEFAULT '{}',
            scope      TEXT NOT NULL DEFAULT '{}',
            note       TEXT,
            created_at TEXT,
            updated_at TEXT
        )''')
        db.execute("CREATE INDEX IF NOT EXISTS idx_access_rules_tool ON access_rules(tool)")
        # Mode d'une règle : « allow » (autoriser seulement : liste blanche sur tout l'outil) ou
        # « reserve » (réserver des ressources à ses bénéficiaires, le reste inchangé).
        if "mode" not in [r[1] for r in db.execute("PRAGMA table_info(access_rules)")]:
            db.execute("ALTER TABLE access_rules ADD COLUMN mode TEXT NOT NULL DEFAULT 'allow'")
        # Contrôleurs broadcast (VSM…) qui pilotent par Ember+ / SW-P-08 : déclarés UNE fois
        # (nom + adresses), puis cités par leur nom dans les règles de périmètre. Le protocole
        # ne transporte aucune identité : l'adresse source EST l'identité.
        db.execute('''CREATE TABLE IF NOT EXISTS controllers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            addresses  TEXT NOT NULL DEFAULT '[]',
            protocols  TEXT NOT NULL DEFAULT '[]',
            note       TEXT,
            created_at TEXT
        )''')

        # Flotte : les autres instances Bobi.Tools connues de celle-ci (carnet d'adresses de
        # la mise à jour pull/push, cf. app/peers.py). `token` = le jeton du PAIR (vide → on
        # présente le nôtre, ce qui couvre le cas d'un jeton partagé par toute la flotte).
        # version/last_seen/deployed_at : dernier ping réussi, pour le tableau de l'UI.
        db.execute('''CREATE TABLE IF NOT EXISTS peers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            url         TEXT UNIQUE NOT NULL,
            token       TEXT DEFAULT '',
            version     TEXT,
            last_seen   TEXT,
            deployed_at TEXT,
            created_at  TEXT
        )''')
        db.commit()


# ─── Settings ───────────────────────────────────────────────

def db_get_setting(key, default=None):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default


def db_set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
        db.commit()


def db_get_all_settings():
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


# ─── Users ──────────────────────────────────────────────────

def db_get_user(username):
    with get_db() as db:
        r = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def db_get_user_by_id(uid):
    with get_db() as db:
        r = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None


def db_list_users():
    with get_db() as db:
        return [dict(r) for r in
                db.execute("SELECT id, username, role, created_at, prenom, nom, email, lang "
                           "FROM users ORDER BY id").fetchall()]


def db_create_user(username, password_hash, role, prenom=None, nom=None, email=None):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, created_at, prenom, nom, email) "
            "VALUES (?,?,?,?,?,?,?)",
            (username, password_hash, role, datetime.now().isoformat(timespec='seconds'),
             prenom, nom, email))
        db.commit()
        return cur.lastrowid


def db_update_user(uid, role=None, password_hash=None, prenom=None, nom=None,
                   email=None, lang=None, theme=None, gh_token=None):
    with get_db() as db:
        if role is not None:
            db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        if password_hash is not None:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, uid))
        # prenom/nom/email : "" autorisé (efface), None = ne pas toucher
        if prenom is not None:
            db.execute("UPDATE users SET prenom=? WHERE id=?", (prenom, uid))
        if nom is not None:
            db.execute("UPDATE users SET nom=? WHERE id=?", (nom, uid))
        if email is not None:
            db.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
        if lang is not None:
            db.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))
        if theme is not None:
            db.execute("UPDATE users SET theme=? WHERE id=?", (theme, uid))
        if gh_token is not None:
            db.execute("UPDATE users SET gh_token=? WHERE id=?", (gh_token, uid))
        db.commit()


def db_delete_user(uid):
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        # Sans ça, l'identifiant du compte supprimé resterait dans les groupes et un
        # futur utilisateur héritant du même id récupérerait ses appartenances.
        db.execute("DELETE FROM user_group_members WHERE user_id=?", (uid,))
        db.commit()


def db_count_users():
    with get_db() as db:
        return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ─── Groupes d'utilisateurs ─────────────────────────────────

def _group_row(r, members):
    return {"id": r["id"], "name": r["name"], "description": r["description"] or "",
            "created_at": r["created_at"], "members": members}


def db_list_groups():
    """Tous les groupes, membres inclus (une seule requête pour les appartenances)."""
    with get_db() as db:
        rows = db.execute("SELECT * FROM user_groups ORDER BY name").fetchall()
        members = {}
        for m in db.execute("SELECT group_id, user_id FROM user_group_members").fetchall():
            members.setdefault(m["group_id"], []).append(m["user_id"])
    return [_group_row(r, sorted(members.get(r["id"], []))) for r in rows]


def db_get_group(gid):
    with get_db() as db:
        r = db.execute("SELECT * FROM user_groups WHERE id=?", (int(gid),)).fetchone()
        if not r:
            return None
        mem = [m["user_id"] for m in db.execute(
            "SELECT user_id FROM user_group_members WHERE group_id=?", (int(gid),)).fetchall()]
    return _group_row(r, sorted(mem))


def db_create_group(name, description=""):
    """Crée un groupe. Renvoie son id, ou None si le nom est déjà pris."""
    name = (name or "").strip()
    if not name:
        raise ValueError("nom requis")
    with get_db() as db:
        if db.execute("SELECT 1 FROM user_groups WHERE name=?", (name,)).fetchone():
            return None
        cur = db.execute(
            "INSERT INTO user_groups (name, description, created_at) VALUES (?,?,?)",
            (name, description or "", datetime.now().isoformat(timespec="seconds")))
        db.commit()
        return cur.lastrowid


def db_update_group(gid, name=None, description=None, members=None):
    """Met à jour un groupe. `members` (liste d'ids) REMPLACE l'appartenance quand fourni.
    Renvoie False si le nom demandé est déjà porté par un autre groupe."""
    gid = int(gid)
    with get_db() as db:
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("nom requis")
            clash = db.execute("SELECT 1 FROM user_groups WHERE name=? AND id<>?",
                               (name, gid)).fetchone()
            if clash:
                return False
            db.execute("UPDATE user_groups SET name=? WHERE id=?", (name, gid))
        if description is not None:
            db.execute("UPDATE user_groups SET description=? WHERE id=?", (description, gid))
        if members is not None:
            db.execute("DELETE FROM user_group_members WHERE group_id=?", (gid,))
            # Ne retient que des comptes existants : une politique de partage ne doit pas
            # traîner d'identifiants fantômes.
            for uid in {int(u) for u in members}:
                if db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                    db.execute("INSERT OR IGNORE INTO user_group_members (group_id, user_id) "
                               "VALUES (?,?)", (gid, uid))
        db.commit()
    return True


def db_delete_group(gid):
    with get_db() as db:
        cur = db.execute("DELETE FROM user_groups WHERE id=?", (int(gid),))
        db.execute("DELETE FROM user_group_members WHERE group_id=?", (int(gid),))
        db.commit()
        return cur.rowcount > 0


def db_user_group_ids(uid):
    """Ids des groupes auxquels l'utilisateur appartient (set, vide si aucun)."""
    if uid is None:
        return set()
    with get_db() as db:
        return {r["group_id"] for r in db.execute(
            "SELECT group_id FROM user_group_members WHERE user_id=?", (int(uid),)).fetchall()}


# ─── Stockage générique des outils (plugin_store) ───────────

def _ps_row(r):
    return {"id": r["id"], "name": r["name"], "value": json.loads(r["value"]),
            "scope": r["scope"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def plugin_store_list(type_, scope=""):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM plugin_store WHERE type=? AND scope=? ORDER BY name",
            (type_, str(scope or ""))).fetchall()
    return [_ps_row(r) for r in rows]


def plugin_store_get(id_, type_=None):
    """Lit une entrée. `type_` BORNE la lecture à l'outil concerné : la table est commune à
    tous les outils et les identifiants y sont globaux, si bien qu'un id venu d'une URL peut
    désigner la ligne d'un AUTRE outil. Deux plugins qui emploient le même nom de scope
    (« device », p. ex.) se marchent alors dessus sans que rien ne le signale."""
    with get_db() as db:
        if type_ is None:
            r = db.execute("SELECT * FROM plugin_store WHERE id=?", (int(id_),)).fetchone()
        else:
            r = db.execute("SELECT * FROM plugin_store WHERE id=? AND type=?",
                           (int(id_), type_)).fetchone()
    return _ps_row(r) | {"type": r["type"]} if r else None


def plugin_store_create(type_, scope, name, value, unique_name=False):
    name = (name or "").strip()
    if not name:
        raise ValueError("nom requis")
    scope = str(scope or "")
    with get_db() as db:
        if unique_name:
            ex = db.execute("SELECT 1 FROM plugin_store WHERE type=? AND scope=? AND name=?",
                            (type_, scope, name)).fetchone()
            if ex:
                return None
        now = datetime.now().isoformat(timespec="seconds")
        cur = db.execute(
            "INSERT INTO plugin_store (type, scope, name, value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (type_, scope, name, json.dumps(value if value is not None else {}), now, now))
        db.commit()
        return cur.lastrowid


def plugin_store_update(id_, name=None, value=None, type_=None):
    """`type_` borne l'écriture à l'outil concerné (cf. plugin_store_get) : sans lui, un id
    d'URL suffit à réécrire la ligne d'un autre outil."""
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name.strip())
    if value is not None:
        sets.append("value=?"); vals.append(json.dumps(value))
    if not sets:
        return False
    sets.append("updated_at=?"); vals.append(datetime.now().isoformat(timespec="seconds"))
    vals.append(int(id_))
    where = "id=?"
    if type_ is not None:
        where += " AND type=?"; vals.append(type_)
    with get_db() as db:
        cur = db.execute(f"UPDATE plugin_store SET {', '.join(sets)} WHERE {where}", vals)
        db.commit()
        return cur.rowcount > 0


def plugin_store_delete(id_, type_=None):
    """`type_` borne la suppression à l'outil concerné (cf. plugin_store_get)."""
    with get_db() as db:
        if type_ is None:
            cur = db.execute("DELETE FROM plugin_store WHERE id=?", (int(id_),))
        else:
            cur = db.execute("DELETE FROM plugin_store WHERE id=? AND type=?",
                             (int(id_), type_))
        db.commit()
        return cur.rowcount > 0


# ─── Journal d'audit ────────────────────────────────────────

def audit_log(tool, action, detail="", user_id=None, username=None):
    """Enregistre une action. Tolérant : une erreur d'audit ne doit jamais casser
    l'action métier (l'appelant peut wrapper, mais on protège aussi ici)."""
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO audit (ts, user_id, username, tool, action, detail) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"),
                 user_id, username, tool, action,
                 detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)))
            db.commit()
    except Exception as e:
        log.warning("audit_log a échoué (%s/%s) : %s", tool, action, e)


def audit_list(tool=None, user_id=None, limit=AUDIT_RETENTION):
    q = "SELECT * FROM audit"
    cond, args = [], []
    if tool:
        cond.append("tool=?"); args.append(tool)
    if user_id is not None:
        cond.append("user_id=?"); args.append(int(user_id))
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
    with get_db() as db:
        return [dict(r) for r in db.execute(q, args).fetchall()]


def audit_search(tool=None, action=None, texte=None, depuis=None, limit=200, offset=0):
    """Journal filtré, page par page. Renvoie (lignes, total).

    Filtres cumulables : `tool`, `action` (préfixe), `texte` (cherché dans l'action ET le
    détail), `depuis` (ISO, borne basse sur `ts`). Le total est celui du filtre, pas de la
    table — sans quoi la pagination mentirait.

    Mesuré sur 98 701 lignes (2026-09-24) : filtre par outil 8 ms, recherche texte 53 ms,
    page récente 0 ms. L'index `idx_audit_tool` porte le cas courant ; la recherche texte
    balaie, ce qui reste sans conséquence à cette échelle et le resterait bien au-delà."""
    cond, args = [], []
    if tool:
        cond.append("tool=?"); args.append(tool)
    if action:
        cond.append("action LIKE ?"); args.append(str(action) + "%")
    if texte:
        cond.append("(detail LIKE ? OR action LIKE ? OR username LIKE ?)")
        args += ["%%%s%%" % texte] * 3
    if depuis:
        cond.append("ts >= ?"); args.append(str(depuis))
    ou = (" WHERE " + " AND ".join(cond)) if cond else ""
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM audit" + ou, args).fetchone()[0]
        lignes = [dict(r) for r in db.execute(
            "SELECT * FROM audit" + ou + " ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [int(limit), int(offset)]).fetchall()]
    return lignes, total


def audit_actions(tool=None):
    """Actions distinctes rencontrées, pour peupler un filtre sans le coder en dur."""
    q = "SELECT DISTINCT action FROM audit"
    args = []
    if tool:
        q += " WHERE tool=?"; args.append(tool)
    with get_db() as db:
        return sorted(r["action"] for r in db.execute(q + " ORDER BY action", args).fetchall()
                      if r["action"])


def audit_stats(tool=None):
    """Poids et étendue du journal : combien de lignes, depuis quand, quelle taille.

    La taille est ESTIMÉE (somme des longueurs des champs texte) et non lue du fichier : la
    base contient bien autre chose que l'audit, et annoncer les 48 Mo du fichier comme « la
    taille des logs » serait faux."""
    ou, args = ("", [])
    if tool:
        ou, args = " WHERE tool=?", [tool]
    with get_db() as db:
        r = db.execute(
            "SELECT COUNT(*) n, MIN(ts) plus_vieux, MAX(ts) plus_recent, "
            "COALESCE(SUM(LENGTH(COALESCE(detail,'')) + LENGTH(COALESCE(action,'')) + "
            "LENGTH(COALESCE(username,'')) + 40), 0) octets "
            "FROM audit" + ou, args).fetchone()
        return {"lignes": r["n"], "plus_vieux": r["plus_vieux"], "plus_recent": r["plus_recent"],
                "octets": r["octets"]}


def audit_stats_by_tool():
    """Poids du journal, OUTIL PAR OUTIL. Sert l'écran de réglages : c'est là qu'on voit
    lequel pèse, et donc lequel mérite une rétention propre."""
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT tool, COUNT(*) lignes, MIN(ts) plus_vieux, MAX(ts) plus_recent, "
            "COALESCE(SUM(LENGTH(COALESCE(detail,'')) + LENGTH(COALESCE(action,'')) + "
            "LENGTH(COALESCE(username,'')) + 40), 0) octets "
            "FROM audit GROUP BY tool ORDER BY lignes DESC").fetchall()]


def audit_purge(jours, tool=None):
    """Supprime les entrées plus vieilles que `jours`. Renvoie le nombre de lignes retirées.

    `jours` <= 0 ne purge RIEN et le dit : une rétention nulle effacerait tout le journal au
    premier passage, et ce n'est jamais ce qu'on veut dire en tapant 0."""
    try:
        jours = int(jours)
    except (TypeError, ValueError):
        return 0
    if jours <= 0:
        return 0
    limite = (datetime.now() - timedelta(days=jours)).isoformat(timespec="seconds")
    with get_db() as db:
        if tool:
            cur = db.execute("DELETE FROM audit WHERE ts < ? AND tool=?", [limite, tool])
        else:
            cur = db.execute("DELETE FROM audit WHERE ts < ?", [limite])
        return cur.rowcount or 0


def audit_tools():
    """Liste distincte des outils ayant au moins une entrée d'audit (pour le filtre UI)."""
    with get_db() as db:
        return [r["tool"] for r in
                db.execute("SELECT DISTINCT tool FROM audit ORDER BY tool").fetchall() if r["tool"]]


# ─── i18n : surcouche de traductions éditée via l'UI ─────────

def db_i18n_overrides():
    """Toutes les surcharges : { lang: { key: value } }."""
    with get_db() as db:
        rows = db.execute("SELECT lang, key, value FROM i18n_overrides").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["lang"], {})[r["key"]] = r["value"]
    return out


def db_i18n_overrides_for_lang(lang):
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM i18n_overrides WHERE lang=?", (lang,)).fetchall()
    return {r["key"]: r["value"] for r in rows}


def db_i18n_set_override(lang, key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO i18n_overrides (lang, key, value) VALUES (?,?,?) "
            "ON CONFLICT(lang, key) DO UPDATE SET value=excluded.value",
            (lang, key, value))
        db.commit()


def db_i18n_delete_override(lang, key):
    with get_db() as db:
        db.execute("DELETE FROM i18n_overrides WHERE lang=? AND key=?", (lang, key))
        db.commit()


def db_i18n_delete_lang(lang):
    """Supprime toutes les surcharges d'une langue (suppression d'une langue custom)."""
    with get_db() as db:
        db.execute("DELETE FROM i18n_overrides WHERE lang=?", (lang,))
        db.commit()


# ─── Rôles (modifiables) ────────────────────────────────────

def db_roles_list():
    with get_db() as db:
        rows = db.execute("SELECT id, label, permissions, builtin FROM roles ORDER BY builtin DESC, id").fetchall()
    out = []
    for r in rows:
        try:
            perms = json.loads(r["permissions"] or "[]")
        except ValueError:
            perms = []
        out.append({"id": r["id"], "label": r["label"] or "", "builtin": bool(r["builtin"]),
                    "permissions": [p for p in perms if isinstance(p, str)]})
    return out


def db_roles_seed(defaults):
    """Sème la table depuis `defaults` ({id: (label, perms)}) SEULEMENT si elle est vide."""
    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM roles").fetchone()[0]:
            return False
        for rid, (label, perms) in defaults.items():
            db.execute("INSERT INTO roles (id, label, permissions, builtin) VALUES (?,?,?,1)",
                       (rid, label, json.dumps(sorted(perms))))
        db.commit()
    return True


def db_role_upsert(rid, label=None, permissions=None, builtin=None):
    with get_db() as db:
        cur = db.execute("SELECT id FROM roles WHERE id=?", (rid,)).fetchone()
        if not cur:
            db.execute("INSERT INTO roles (id, label, permissions, builtin) VALUES (?,?,?,?)",
                       (rid, label or "", json.dumps(sorted(permissions or [])), 1 if builtin else 0))
        else:
            if label is not None:
                db.execute("UPDATE roles SET label=? WHERE id=?", (label, rid))
            if permissions is not None:
                db.execute("UPDATE roles SET permissions=? WHERE id=?",
                           (json.dumps(sorted(set(permissions))), rid))
        db.commit()


def db_role_delete(rid):
    with get_db() as db:
        db.execute("DELETE FROM roles WHERE id=?", (rid,))
        db.commit()


def db_count_users_by_role():
    with get_db() as db:
        return {r[0]: r[1] for r in db.execute("SELECT role, COUNT(*) FROM users GROUP BY role")}


# ─── Règles de périmètre ────────────────────────────────────

def _rule_row(r):
    def _j(v, d):
        try:
            return json.loads(v) if v else d
        except ValueError:
            return d
    return {"id": r["id"], "tool": r["tool"], "perms": _j(r["perms"], []),
            "principals": _j(r["principals"], {}), "scope": _j(r["scope"], {}),
            "note": r["note"] or "", "created_at": r["created_at"], "updated_at": r["updated_at"],
            "mode": (r["mode"] if "mode" in r.keys() else None) or "allow"}


def db_rules_list(tool=None):
    with get_db() as db:
        if tool:
            rows = db.execute("SELECT * FROM access_rules WHERE tool=? ORDER BY id", (tool,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM access_rules ORDER BY tool, id").fetchall()
    return [_rule_row(r) for r in rows]


def db_rule_get(rid):
    with get_db() as db:
        r = db.execute("SELECT * FROM access_rules WHERE id=?", (rid,)).fetchone()
    return _rule_row(r) if r else None


def db_rule_save(rule, rid=None):
    now = datetime.now().isoformat(timespec="seconds")
    vals = (rule["tool"], json.dumps(rule.get("perms") or []),
            json.dumps(rule.get("principals") or {}), json.dumps(rule.get("scope") or {}),
            rule.get("note") or "",
            rule.get("mode") if rule.get("mode") in ("reserve", "allow", "deny") else "allow")
    with get_db() as db:
        if rid is None:
            cur = db.execute("INSERT INTO access_rules (tool, perms, principals, scope, note, mode, "
                             "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", vals + (now, now))
            rid = cur.lastrowid
        else:
            db.execute("UPDATE access_rules SET tool=?, perms=?, principals=?, scope=?, note=?, mode=?, "
                       "updated_at=? WHERE id=?", vals + (now, rid))
        db.commit()
    return rid


def db_rule_delete(rid):
    with get_db() as db:
        db.execute("DELETE FROM access_rules WHERE id=?", (rid,))
        db.commit()


# ─── Contrôleurs broadcast ──────────────────────────────────

def db_controllers_list():
    with get_db() as db:
        rows = db.execute("SELECT * FROM controllers ORDER BY name").fetchall()
    out = []
    for r in rows:
        def _j(v):
            try:
                return json.loads(v or "[]")
            except ValueError:
                return []
        out.append({"id": r["id"], "name": r["name"], "addresses": _j(r["addresses"]),
                    "protocols": _j(r["protocols"]), "note": r["note"] or "",
                    "created_at": r["created_at"]})
    return out


def db_controller_save(c, cid=None):
    vals = (c["name"], json.dumps(c.get("addresses") or []), json.dumps(c.get("protocols") or []),
            c.get("note") or "")
    with get_db() as db:
        if cid is None:
            cur = db.execute("INSERT INTO controllers (name, addresses, protocols, note, created_at) "
                             "VALUES (?,?,?,?,?)", vals + (datetime.now().isoformat(timespec="seconds"),))
            cid = cur.lastrowid
        else:
            db.execute("UPDATE controllers SET name=?, addresses=?, protocols=?, note=? WHERE id=?",
                       vals + (cid,))
        db.commit()
    return cid


def db_controller_delete(cid):
    with get_db() as db:
        db.execute("DELETE FROM controllers WHERE id=?", (cid,))
        db.commit()
