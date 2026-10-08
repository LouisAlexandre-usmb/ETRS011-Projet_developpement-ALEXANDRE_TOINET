-- Schéma de la base de supervision SNMP.
-- Exécuté par app/db.py au démarrage, si les tables n'existent pas encore.
-- Noms en snake_case, d'après le modèle de classes UML validé.

-- Catalogue des métriques pouvant être collectées (cpu, ram, uptime...).
CREATE TABLE IF NOT EXISTS metrique (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,   -- ex. "cpu", "ram", "uptime", "if_in_octets"
    libelle     TEXT NOT NULL,
    unite       TEXT,
    type_valeur TEXT NOT NULL CHECK (type_valeur IN ('JAUGE', 'COMPTEUR')),
    par_interface INTEGER NOT NULL DEFAULT 0 CHECK (par_interface IN (0, 1))
);

-- Étiquette pour regrouper ou filtrer les équipements (par site, par type...).
CREATE TABLE IF NOT EXISTS groupe (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    nom  TEXT NOT NULL UNIQUE
);

-- Plan de surveillance : quelles métriques collecter, avec quels OID.
CREATE TABLE IF NOT EXISTS modele_supervision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nom         TEXT NOT NULL UNIQUE,
    description TEXT
);

-- Association modele_supervision <-> metrique, portant l'OID à interroger.
CREATE TABLE IF NOT EXISTS element_surveille (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    modele_supervision_id INTEGER NOT NULL REFERENCES modele_supervision(id) ON DELETE CASCADE,
    metrique_id           INTEGER NOT NULL REFERENCES metrique(id) ON DELETE CASCADE,
    oid                   TEXT NOT NULL,
    actif                 INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),
    UNIQUE (modele_supervision_id, metrique_id)
);

-- Équipement réseau supervisé.
CREATE TABLE IF NOT EXISTS equipement (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    nom                     TEXT NOT NULL,
    adresse                 TEXT NOT NULL UNIQUE,   -- IP ou nom DNS
    description             TEXT,
    version_snmp            TEXT NOT NULL DEFAULT 'V2C' CHECK (version_snmp IN ('V1', 'V2C')),
    communaute_snmp         TEXT NOT NULL,
    intervalle_polling      INTEGER,                -- NULL = valeur globale (parametres_generaux)
    actif                   INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),
    statut                  TEXT NOT NULL DEFAULT 'EN_ATTENTE'
                                CHECK (statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE')),
    nb_echecs_consecutifs   INTEGER NOT NULL DEFAULT 0,
    description_systeme     TEXT,                   -- sysDescr
    date_ajout              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    derniere_collecte       TEXT,
    groupe_id               INTEGER REFERENCES groupe(id) ON DELETE SET NULL,
    parent_id               INTEGER REFERENCES equipement(id) ON DELETE SET NULL,
    modele_supervision_id   INTEGER NOT NULL REFERENCES modele_supervision(id)
);

-- Une seule ligne : réglages globaux par défaut.
CREATE TABLE IF NOT EXISTS parametres_generaux (
    id                              INTEGER PRIMARY KEY CHECK (id = 1),
    intervalle_polling_defaut       INTEGER NOT NULL DEFAULT 60,
    nb_echecs_avant_indisponibilite INTEGER NOT NULL DEFAULT 3,
    delai_reponse_snmp              REAL NOT NULL DEFAULT 2,
    periode_rafraichissement        INTEGER NOT NULL DEFAULT 30,
    duree_conservation_detail       INTEGER NOT NULL DEFAULT 7,
    duree_conservation_agregee      INTEGER NOT NULL DEFAULT 365
);
