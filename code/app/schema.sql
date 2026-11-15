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
    communaute_snmp         TEXT NOT NULL,          -- SNMP v2c uniquement
    intervalle_polling      INTEGER,                -- NULL = valeur globale (parametres_generaux)
    duree_conservation      INTEGER                 -- mesures, en jours ; NULL = valeur par défaut
                                                    -- (parametres_generaux.duree_conservation_agregee, 30, le max)
                                CHECK (duree_conservation IS NULL
                                       OR (duree_conservation > 0 AND duree_conservation <= 30)),
    statut                  TEXT NOT NULL DEFAULT 'EN_ATTENTE'
                                CHECK (statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE')),
    nb_echecs_consecutifs   INTEGER NOT NULL DEFAULT 0,
    description_systeme     TEXT,                   -- sysDescr
    date_ajout              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    derniere_collecte       TEXT,
    groupe_id               INTEGER REFERENCES groupe(id) ON DELETE SET NULL,
    modele_supervision_id   INTEGER NOT NULL REFERENCES modele_supervision(id)
);

-- Une seule ligne : réglages globaux par défaut.
CREATE TABLE IF NOT EXISTS parametres_generaux (
    id                              INTEGER PRIMARY KEY CHECK (id = 1),
    intervalle_polling_defaut       INTEGER NOT NULL DEFAULT 60,
    nb_echecs_avant_indisponibilite INTEGER NOT NULL DEFAULT 3,
    -- Mesures : brutes pendant duree_conservation_detail jours, puis moyennées par
    -- demi-heure (mesure_agregee) jusqu'à duree_conservation_agregee jours (= durée de
    -- conservation par défaut et maximale d'un équipement), puis purgées.
    duree_conservation_detail       INTEGER NOT NULL DEFAULT 15,
    duree_conservation_agregee      INTEGER NOT NULL DEFAULT 30,
    -- Logs : changements de statut et alertes (historique utile, conservé longtemps),
    -- erreurs de collecte (traces techniques bruyantes, conservées moins longtemps).
    duree_conservation_evenements   INTEGER NOT NULL DEFAULT 365,
    duree_conservation_erreurs      INTEGER NOT NULL DEFAULT 30
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

-- Volume de stockage réel d'un équipement (hrStorageTable, type "disque fixe" : malgré le
-- nom SNMP, ce sont des points de montage, pas des disques physiques). On ne garde que les
-- points de montage qui représentent un vrai espace de stockage (racine "/", ou "/volumeN"
-- façon NAS Synology) : les montages internes (Docker, pseudo-systèmes de fichiers comme
-- /proc ou /tmp...) ne sont même pas insérés ici, cf. app/collecte.py. Comme pour les
-- interfaces : plusieurs volumes possibles par équipement, chacun supervisable
-- individuellement.
CREATE TABLE IF NOT EXISTS volume (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id       INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    index_snmp          INTEGER NOT NULL,   -- hrStorageIndex
    nom                 TEXT NOT NULL,      -- nom convivial (ex. "Volume 1"), pas le chemin SNMP
    taille_octets       INTEGER,            -- hrStorageSize * hrStorageAllocationUnits
    utilise_octets      INTEGER,            -- hrStorageUsed * hrStorageAllocationUnits
    pourcentage_utilise REAL,               -- dernier relevé connu (NULL = jamais relevé)
    visible             INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
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

-- Mesures de plus de duree_conservation_detail jours, résumées par tranche de 30 minutes
-- (classe MesureAgregee du diagramme). horodatage = début de la tranche. Pour un compteur
-- de trafic par interface, ce sont les débits (par seconde) qui sont résumés, pas le
-- compteur brut. moyenne/minimum/maximum à NULL et nb_echantillons à 0 : aucune valeur
-- relevée pendant la tranche (équipement injoignable), le trou est conservé.
CREATE TABLE IF NOT EXISTS mesure_agregee (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id   INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    metrique_id     INTEGER NOT NULL REFERENCES metrique(id),
    interface_id    INTEGER REFERENCES interface(id) ON DELETE CASCADE,
    horodatage      TEXT NOT NULL,
    moyenne         REAL,
    minimum         REAL,
    maximum         REAL,
    nb_echantillons INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mesure_agregee_lecture ON mesure_agregee (equipement_id, metrique_id, horodatage);

-- Logs (changement_statut, erreur_collecte, alerte) : quand un équipement est archivé, il
-- est supprimé mais ses logs restent (equipement_id passe à NULL) ; son nom est alors
-- recopié dans nom_equipement_archive pour rester affichable. Une suppression simple
-- (sans archivage) efface les logs explicitement, cf. db.supprimer_equipement.

-- Historique des changements de statut d'un équipement.
CREATE TABLE IF NOT EXISTS changement_statut (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id  INTEGER REFERENCES equipement(id) ON DELETE SET NULL,
    nom_equipement_archive TEXT,
    horodatage     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    ancien_statut  TEXT NOT NULL
        CHECK (ancien_statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE')),
    nouveau_statut TEXT NOT NULL
        CHECK (nouveau_statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE'))
);

-- Journal des erreurs de collecte : n'interrompt jamais la supervision du reste du parc.
CREATE TABLE IF NOT EXISTS erreur_collecte (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id INTEGER REFERENCES equipement(id) ON DELETE SET NULL,
    nom_equipement_archive TEXT,
    horodatage    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    type_erreur   TEXT NOT NULL CHECK (type_erreur IN
        ('INJOIGNABLE', 'ECHEC_AUTHENTIFICATION', 'INDICATEUR_NON_SUPPORTE', 'VALEUR_ABERRANTE')),
    message       TEXT
);

-- Seuil global par métrique (au plus un par métrique, pas de surcharge par équipement :
-- cf. cahier des charges). Au-delà, les équipements qui mesurent cette métrique sont en alerte.
CREATE TABLE IF NOT EXISTS seuil (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    metrique_id   INTEGER NOT NULL UNIQUE REFERENCES metrique(id),
    operateur     TEXT NOT NULL CHECK (operateur IN
        ('SUPERIEUR', 'SUPERIEUR_OU_EGAL', 'INFERIEUR', 'INFERIEUR_OU_EGAL')),
    valeur_seuil  REAL NOT NULL
);

-- Alerte visuelle uniquement (jamais d'e-mail/SMS) : dépassement d'un seuil global, ou
-- interface supervisée tombée (DOWN). Une alerte active fait passer l'équipement en DEGRADE.
CREATE TABLE IF NOT EXISTS alerte (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id      INTEGER REFERENCES equipement(id) ON DELETE SET NULL,
    nom_equipement_archive TEXT,
    type_alerte        TEXT NOT NULL CHECK (type_alerte IN ('SEUIL_DEPASSE', 'INTERFACE_INDISPONIBLE')),
    etat               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (etat IN ('ACTIVE', 'CLOTUREE')),
    date_declenchement TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    date_cloture       TEXT,
    valeur_mesuree     REAL,
    message            TEXT NOT NULL,
    seuil_id           INTEGER REFERENCES seuil(id) ON DELETE SET NULL,      -- ssi SEUIL_DEPASSE
    interface_id       INTEGER REFERENCES interface(id) ON DELETE SET NULL, -- interface DOWN, ou seuil
                                                                            -- d'utilisation d'interface
    volume_id          INTEGER REFERENCES volume(id) ON DELETE SET NULL     -- ssi seuil sur un volume
);
