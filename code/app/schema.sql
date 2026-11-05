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

-- Interface réseau d'un équipement (ifTable SNMP).
CREATE TABLE IF NOT EXISTS interface (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id   INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    index_snmp      INTEGER NOT NULL,   -- ifIndex
    nom             TEXT NOT NULL,
    vitesse         INTEGER,            -- ifSpeed, en bits/s, si connue
    statut          TEXT NOT NULL DEFAULT 'INCONNU' CHECK (statut IN ('UP', 'DOWN', 'INCONNU')),
    visible         INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),  -- affichage uniquement
    UNIQUE (equipement_id, index_snmp)
);

-- Relevé d'une métrique à un instant donné, pour un équipement (et son interface si la
-- métrique est par_interface). valeur = NULL signifie que l'équipement n'a pas répondu à cet
-- instant (un trou dans les courbes), pas une valeur nulle.
CREATE TABLE IF NOT EXISTS mesure (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    metrique_id   INTEGER NOT NULL REFERENCES metrique(id),
    interface_id  INTEGER REFERENCES interface(id) ON DELETE CASCADE,
    horodatage    TEXT NOT NULL,
    valeur        REAL
);
CREATE INDEX IF NOT EXISTS idx_mesure_lecture ON mesure (equipement_id, metrique_id, horodatage);

-- Historique des changements de statut d'un équipement.
CREATE TABLE IF NOT EXISTS changement_statut (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id  INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    horodatage     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    ancien_statut  TEXT NOT NULL
        CHECK (ancien_statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE')),
    nouveau_statut TEXT NOT NULL
        CHECK (nouveau_statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE'))
);

-- Journal des erreurs de collecte : n'interrompt jamais la supervision du reste du parc.
CREATE TABLE IF NOT EXISTS erreur_collecte (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    horodatage    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    type_erreur   TEXT NOT NULL CHECK (type_erreur IN
        ('INJOIGNABLE', 'ECHEC_AUTHENTIFICATION', 'INDICATEUR_NON_SUPPORTE', 'VALEUR_ABERRANTE')),
    message       TEXT
);
