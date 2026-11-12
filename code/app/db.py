"""Accès à la base SQLite : connexion, création du schéma, fonctions d'accès aux données.

Pas d'ORM : le SQL est écrit à la main. Chaque fonction ouvre sa propre
connexion et la referme ; pour le volume de ce projet, c'est plus simple à suivre qu'un pool
de connexions partagées.
"""
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app import config

# Structure de la base, créée au démarrage si elle n'existe pas encore : ne modifie jamais une base
# existante. Une colonne ajoutée à une table existante doit aussi l'être dans _migrer.
# Noms en snake_case, d'après le modèle de classes UML validé.
TABLE_ALERTE = """
-- Alertes : EQUIPEMENT = ne répond plus au ping ; SERVICE = joignable, mais relevé SNMP en erreur,
-- seuil dépassé ou interface suivie en panne. cause_racine_id relie une alerte à celle de
-- l'équipement parent en panne.
CREATE TABLE IF NOT EXISTS alerte (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id   INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    categorie       TEXT NOT NULL CHECK (categorie IN ('EQUIPEMENT', 'SERVICE')),
    code            TEXT NOT NULL
                        CHECK (code IN ('INJOIGNABLE', 'SNMP_EN_ECHEC', 'RELEVE_EN_ECHEC', 'SEUIL_DEPASSE',
                                        'INTERFACE_DOWN')),
    metrique_id     INTEGER REFERENCES metrique(id) ON DELETE CASCADE,
    objet_id        INTEGER REFERENCES objet_detecte(id) ON DELETE CASCADE,   -- seuil d'un objet détecté
    interface_id    INTEGER REFERENCES interface(id) ON DELETE CASCADE,       -- interface en panne
    message         TEXT NOT NULL,
    criticite       TEXT NOT NULL DEFAULT 'AVERTISSEMENT' CHECK (criticite IN ('AVERTISSEMENT', 'CRITIQUE')),
    statut          TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (statut IN ('ACTIVE', 'CLOTUREE')),
    date_debut      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    date_fin        TEXT,
    cause_racine_id INTEGER REFERENCES alerte(id) ON DELETE SET NULL
);
"""
# Une seule alerte active par équipement, type d'alerte et service (mesure, objet ou interface).
INDEX_ALERTE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerte_active
    ON alerte (equipement_id, code, COALESCE(metrique_id, 0), COALESCE(objet_id, 0), COALESCE(interface_id, 0))
    WHERE statut = 'ACTIVE';
"""

SCHEMA = """
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
    methode               TEXT,    -- calcul d'un modèle de constructeur ; NULL = calcul propre à l'OID
    oid_secondaire        TEXT,    -- second OID d'un ratio
    diviseur              REAL,    -- valeur à diviser (dixièmes...) ; pour un ratio, unité des tailles
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
    nb_echecs_alerte        INTEGER,                -- NULL = valeur globale
    duree_conservation      INTEGER,                -- en jours, NULL = valeur globale
    duree_conservation_agregee INTEGER,             -- données agrégées, en jours, NULL = valeur globale
    actif                   INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),
    statut                  TEXT NOT NULL DEFAULT 'EN_ATTENTE'
                                CHECK (statut IN ('EN_ATTENTE', 'OPERATIONNEL', 'DEGRADE', 'INDISPONIBLE')),
    nb_echecs_consecutifs   INTEGER NOT NULL DEFAULT 0,
    description_systeme     TEXT,                   -- sysDescr
    sys_object_id           TEXT,                   -- identifie le constructeur et le modèle
    identification_requise  INTEGER NOT NULL DEFAULT 0, -- modèle à réidentifier (après une mise à jour)
    systeme                 TEXT,                   -- constructeur reconnu (modèle intégré)
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
    periode_rafraichissement        INTEGER NOT NULL DEFAULT 10,
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
    suivi           TEXT NOT NULL DEFAULT 'A_VALIDER' CHECK (suivi IN ('A_VALIDER', 'OUI', 'NON')),
    presente        INTEGER NOT NULL DEFAULT 1 CHECK (presente IN (0, 1)),  -- déclarée à la dernière découverte
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
    valeur        REAL,
    utilise       REAL,             -- RAM ou disque : espace utilisé, en octets
    total         REAL,             -- RAM ou disque : espace total, en octets
    detail        TEXT              -- disque : volume concerné (le plus rempli)
);
CREATE INDEX IF NOT EXISTS idx_mesure_lecture ON mesure (equipement_id, metrique_id, horodatage);

-- Détail de la capacité disque : chaque volume à chaque relevé (la mesure « disque » ne garde que
-- le plus rempli, qui sert aux seuils et aux alertes).
CREATE TABLE IF NOT EXISTS mesure_volume (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    horodatage    TEXT NOT NULL,
    nom           TEXT NOT NULL,
    valeur        REAL NOT NULL,    -- % utilisé
    utilise       REAL,             -- octets
    total         REAL              -- octets
);
CREATE INDEX IF NOT EXISTS idx_mesure_volume ON mesure_volume (equipement_id, horodatage);

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

-- Seuils de performance par service, globaux à tout le parc : au-dessus de `valeur`, l'alerte est
-- un avertissement ; au-dessus de `valeur_critique`, elle devient critique.
CREATE TABLE IF NOT EXISTS seuils_config (
    metrique_id     INTEGER PRIMARY KEY REFERENCES metrique(id) ON DELETE CASCADE,
    valeur          REAL NOT NULL CHECK (valeur BETWEEN 0 AND 100),
    valeur_critique REAL NOT NULL DEFAULT 95 CHECK (valeur_critique BETWEEN 0 AND 100)
);

-- Seuils d'un équipement, sur un service de mesure (metrique_id) ou un objet détecté (objet_id).
-- Limites dans l'unité du service (%, Mb/s, minutes, °C...). Sans ligne pour CPU, RAM ou disque,
-- les valeurs par défaut de seuils_config s'appliquent.
CREATE TABLE IF NOT EXISTS seuil (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    metrique_id   INTEGER REFERENCES metrique(id) ON DELETE CASCADE,
    objet_id      INTEGER REFERENCES objet_detecte(id) ON DELETE CASCADE,
    operateur     TEXT NOT NULL DEFAULT '>' CHECK (operateur IN ('>', '<', '=')),
    avertissement REAL,
    critique      REAL,
    actif         INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),
    CHECK ((metrique_id IS NULL) != (objet_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_seuil_cible
    ON seuil (equipement_id, COALESCE(metrique_id, 0), COALESCE(objet_id, 0));

-- Services de mesure qu'un équipement ne relève plus, au choix de l'admin (CPU, trafic...).
CREATE TABLE IF NOT EXISTS service_ignore (
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    metrique_id   INTEGER NOT NULL REFERENCES metrique(id) ON DELETE CASCADE,
    PRIMARY KEY (equipement_id, metrique_id)
);

CREATE TABLE IF NOT EXISTS modele_objets (
    nom       TEXT PRIMARY KEY,
    contenu   TEXT NOT NULL,
    ajoute_le TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

-- Objets d'un modèle d'objets auxquels un équipement a répondu lors de la détection.
CREATE TABLE IF NOT EXISTS objet_detecte (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    modele_nom    TEXT NOT NULL REFERENCES modele_objets(nom) ON DELETE CASCADE,
    mib           TEXT NOT NULL,
    objet         TEXT NOT NULL,
    oid           TEXT NOT NULL,
    table_nom     TEXT,            -- NULL pour une valeur simple
    nature        TEXT NOT NULL CHECK (nature IN ('MESURE', 'ETAT', 'COMPTEUR', 'TEXTE')),
    description   TEXT,
    etats         TEXT,            -- libellés des valeurs, au format JSON
    garde         INTEGER NOT NULL DEFAULT 1 CHECK (garde IN (0, 1)),  -- choisi par l'admin
    UNIQUE (equipement_id, mib, objet)
);

-- Dernière valeur de chaque instance : un disque, un volume...
CREATE TABLE IF NOT EXISTS valeur_objet (
    objet_id INTEGER NOT NULL REFERENCES objet_detecte(id) ON DELETE CASCADE,
    instance TEXT NOT NULL,
    valeur   TEXT,
    PRIMARY KEY (objet_id, instance)
);

-- Historique des objets numériques, pour leurs graphiques.
CREATE TABLE IF NOT EXISTS mesure_objet (
    objet_id   INTEGER NOT NULL REFERENCES objet_detecte(id) ON DELETE CASCADE,
    instance   TEXT NOT NULL,
    horodatage TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    valeur     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mesure_objet ON mesure_objet (objet_id, horodatage);

-- Moyennes par heure des mesures dont le détail a été purgé (durée : conservation agrégée).
-- interface_id vaut 0 quand la mesure ne concerne pas une interface. Pour un compteur d'octets,
-- le maximum de l'heure est le dernier relevé : le débit se recalcule entre deux heures.
CREATE TABLE IF NOT EXISTS mesure_agregee (
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    metrique_id   INTEGER NOT NULL REFERENCES metrique(id),
    interface_id  INTEGER NOT NULL DEFAULT 0,
    heure         TEXT NOT NULL,
    moyenne       REAL NOT NULL,
    minimum       REAL NOT NULL,
    maximum       REAL NOT NULL,
    utilise       REAL,
    total         REAL,
    nb            INTEGER NOT NULL,
    PRIMARY KEY (equipement_id, metrique_id, interface_id, heure)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS volume_agrege (
    equipement_id INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    nom           TEXT NOT NULL,
    heure         TEXT NOT NULL,
    moyenne       REAL NOT NULL,
    utilise       REAL,
    total         REAL,
    PRIMARY KEY (equipement_id, nom, heure)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS objet_agrege (
    objet_id INTEGER NOT NULL REFERENCES objet_detecte(id) ON DELETE CASCADE,
    instance TEXT NOT NULL,
    heure    TEXT NOT NULL,
    moyenne  REAL NOT NULL,
    PRIMARY KEY (objet_id, instance, heure)
) WITHOUT ROWID;

-- Criticité des alertes qui ne dépendent pas d'un seuil.
CREATE TABLE IF NOT EXISTS criticite_service (
    code      TEXT PRIMARY KEY,
    libelle   TEXT NOT NULL,
    criticite TEXT NOT NULL CHECK (criticite IN ('AVERTISSEMENT', 'CRITIQUE'))
);
""" + TABLE_ALERTE + INDEX_ALERTE


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Ouvre une connexion (clés étrangères activées), valide ou annule la transaction, puis
    la referme : `with sqlite3.connect(...)` seul gère la transaction mais ne ferme rien."""
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    connexion = sqlite3.connect(config.DB_PATH)
    try:
        connexion.execute("PRAGMA foreign_keys = ON")
        connexion.row_factory = sqlite3.Row
        with connexion:
            yield connexion
    finally:
        connexion.close()


def init_db() -> None:
    """Crée les tables si besoin et ajoute les modèles intégrés (appelé au démarrage)."""
    with get_connection() as connexion:
        _autoriser_alertes_interface(connexion)   # avant le schéma : il recrée l'index des alertes
        connexion.executescript(SCHEMA)
        _migrer(connexion)
        _seed(connexion)


def _autoriser_alertes_interface(connexion: sqlite3.Connection) -> None:
    """Bases d'avant les alertes d'interface : la table des alertes refuse ce nouveau type (contrainte
    sur le code) et ne sait pas désigner une interface. SQLite ne modifie pas une contrainte : la
    table est recréée, avec toutes ses alertes. Les colonnes ajoutées au fil des versions (criticité,
    objet) sont reprises, ou reçoivent leur valeur par défaut."""
    ligne = connexion.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'alerte'").fetchone()
    if ligne is None or "INTERFACE_DOWN" in ligne[0]:
        return
    anciennes = [colonne["name"] for colonne in connexion.execute("PRAGMA table_info(alerte)")]
    # Clés étrangères suspendues le temps de l'opération (comme le recommande SQLite), hors transaction :
    # les tables parentes (objets, interfaces) n'existent pas encore dans une très ancienne base
    connexion.execute("PRAGMA foreign_keys = OFF")
    try:
        connexion.execute("DROP INDEX IF EXISTS idx_alerte_active")
        connexion.execute("ALTER TABLE alerte RENAME TO alerte_ancienne")
        connexion.execute(TABLE_ALERTE.replace("IF NOT EXISTS ", ""))
        nouvelles = [colonne["name"] for colonne in connexion.execute("PRAGMA table_info(alerte)")]
        communes = ", ".join(colonne for colonne in nouvelles if colonne in anciennes)
        connexion.execute(f"INSERT INTO alerte ({communes}) SELECT {communes} FROM alerte_ancienne")
        connexion.execute("DROP TABLE alerte_ancienne")
        if "criticite" not in anciennes:   # toute première version : une panne était toujours critique
            connexion.execute("UPDATE alerte SET criticite = 'CRITIQUE' WHERE code IN ('INJOIGNABLE', 'SNMP_EN_ECHEC')")
        connexion.commit()
    finally:
        connexion.execute("PRAGMA foreign_keys = ON")


MODELE_GENERIQUE = 1        # disponibilité et trafic seulement
MODELE_HOTE_STANDARD = 3    # CPU, RAM et disques par les MIB standard (HOST-RESOURCES, UCD-SNMP)
MODELES_GENERIQUES = (MODELE_GENERIQUE, MODELE_HOTE_STANDARD)
FICHIER_MODELES_INTEGRES = Path(__file__).resolve().parent / "modeles_integres.json"
_integres: dict | None = None


def modeles_integres() -> dict:
    """Contenu de modeles_integres.json : modèles d'objets génériques et modèles des constructeurs."""
    global _integres
    if _integres is None:
        _integres = json.loads(FICHIER_MODELES_INTEGRES.read_text(encoding="utf-8"))
    return _integres


def constructeurs() -> list[dict]:
    """Modèles des constructeurs (Cisco, Aruba, QNAP, Synology) : règles de reconnaissance, indicateurs
    CPU / mémoire / disque et capteurs."""
    return modeles_integres()["constructeurs"]


OID_HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"


def _migrer(connexion: sqlite3.Connection) -> None:
    """Ajoute aux bases existantes les colonnes que CREATE TABLE IF NOT EXISTS ne crée pas."""
    # Base d'avant la reconnaissance des constructeurs : ses équipements seront tous identifiés une fois
    sans_constructeur = "systeme" not in {ligne["name"] for ligne in connexion.execute("PRAGMA table_info(equipement)")}
    colonnes = {ligne["name"] for ligne in connexion.execute("PRAGMA table_info(interface)")}
    if "suivi" not in colonnes:
        connexion.execute(
            "ALTER TABLE interface ADD COLUMN suivi TEXT NOT NULL DEFAULT 'A_VALIDER' "
            "CHECK (suivi IN ('A_VALIDER', 'OUI', 'NON'))"
        )
    if "presente" not in colonnes:
        connexion.execute(
            "ALTER TABLE interface ADD COLUMN presente INTEGER NOT NULL DEFAULT 1 "
            "CHECK (presente IN (0, 1))"
        )
    for table, colonne, type_colonne in (
        ("equipement", "sys_object_id", "TEXT"),
        ("equipement", "nb_echecs_alerte", "INTEGER"),
        ("equipement", "duree_conservation", "INTEGER"),
        ("equipement", "duree_conservation_agregee", "INTEGER"),
        ("element_surveille", "methode", "TEXT"),
        ("element_surveille", "oid_secondaire", "TEXT"),
        ("equipement", "identification_requise", "INTEGER NOT NULL DEFAULT 0"),
        ("element_surveille", "diviseur", "REAL"),
        ("equipement", "systeme", "TEXT"),
        ("mesure", "utilise", "REAL"),
        ("mesure", "total", "REAL"),
        ("mesure", "detail", "TEXT"),
        ("objet_detecte", "garde", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if colonne not in {ligne["name"] for ligne in connexion.execute(f"PRAGMA table_info({table})")}:
            connexion.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_colonne}")
    if "valeur_critique" not in {ligne["name"] for ligne in connexion.execute("PRAGMA table_info(seuils_config)")}:
        connexion.execute(
            "ALTER TABLE seuils_config ADD COLUMN valeur_critique REAL NOT NULL DEFAULT 95 "
            "CHECK (valeur_critique BETWEEN 0 AND 100)"
        )
        # Un seuil déjà réglé au-dessus de 95 % ne doit pas dépasser son niveau critique
        connexion.execute("UPDATE seuils_config SET valeur_critique = MAX(valeur, valeur_critique)")
    # Un seuil propre décoché revient désormais au réglage global : il n'a plus lieu d'exister
    if "actif" in {ligne["name"] for ligne in connexion.execute("PRAGMA table_info(seuil_equipement)")}:
        connexion.execute("DELETE FROM seuil_equipement WHERE actif = 0")
    # Ancienne valeur par défaut, trop lente pour suivre les alertes (aucun écran ne la modifie)
    connexion.execute("UPDATE parametres_generaux SET periode_rafraichissement = 10 WHERE periode_rafraichissement = 30")
    # Ce modèle n'existe plus (l'id 3 est réservé au modèle Linux) : ses équipements passent au générique
    for modele in connexion.execute(
        "SELECT id FROM modele_supervision WHERE nom = 'Équipement réseau basique'"
    ).fetchall():
        connexion.execute(
            "UPDATE equipement SET modele_supervision_id = ? WHERE modele_supervision_id = ?",
            (MODELE_GENERIQUE, modele["id"]),
        )
        connexion.execute("DELETE FROM modele_supervision WHERE id = ?", (modele["id"],))
    if sans_constructeur:
        connexion.execute("UPDATE equipement SET identification_requise = 1")
    # Les réglages par service (intervalle, échecs avant alerte) n'existent plus : chaque service
    # suit les réglages de son équipement
    connexion.execute("DROP TABLE IF EXISTS reglage_service")
    # Les seuils propres d'avant deviennent des seuils « supérieur à » de l'équipement
    if connexion.execute("SELECT 1 FROM sqlite_master WHERE name = 'seuil_equipement'").fetchone():
        connexion.execute(
            "INSERT OR IGNORE INTO seuil (equipement_id, metrique_id, operateur, avertissement, critique) "
            "SELECT equipement_id, metrique_id, '>', valeur, valeur_critique FROM seuil_equipement"
        )
        connexion.execute("DROP TABLE seuil_equipement")
    # Le générique ne relève plus CPU, RAM ni disque : c'est le rôle de l'hôte standard
    if connexion.execute(
        "SELECT 1 FROM element_surveille JOIN metrique ON metrique.id = element_surveille.metrique_id "
        "WHERE modele_supervision_id = ? AND metrique.code IN ('cpu', 'ram', 'disque')", (MODELE_GENERIQUE,),
    ).fetchone():
        connexion.execute("UPDATE equipement SET identification_requise = 1 WHERE modele_supervision_id = ?",
                          (MODELE_GENERIQUE,))
        connexion.execute(
            "DELETE FROM element_surveille WHERE modele_supervision_id = ? AND metrique_id IN "
            "(SELECT id FROM metrique WHERE code IN ('cpu', 'ram', 'disque'))", (MODELE_GENERIQUE,),
        )
    # L'hôte standard lit d'abord HOST-RESOURCES-MIB, puis UCD-SNMP-MIB en secours (côté collecte)
    for code, oid in (("cpu", OID_HR_PROCESSOR_LOAD), ("ram", OID_HR_STORAGE_USED)):
        connexion.execute(
            "UPDATE element_surveille SET oid = ?, methode = NULL, oid_secondaire = NULL "
            "WHERE modele_supervision_id = ? AND metrique_id = (SELECT id FROM metrique WHERE code = ?)",
            (oid, MODELE_HOTE_STANDARD, code),
        )
    # Colonnes des versions précédentes que plus rien ne lit : ancienne détection des modèles, catalogue,
    # versions multiples d'un modèle, activation des seuils globaux
    for table, colonne in (("modele_supervision", "prefixe_sys_object_id"), ("modele_supervision", "oid_signature"),
                           ("modele_supervision", "empreinte"), ("parametres_generaux", "catalogue_initialise"),
                           ("seuils_config", "actif"), ("modele_supervision", "source"),
                           ("modele_supervision", "modele_source"), ("modele_objets", "integre")):
        if colonne in {ligne["name"] for ligne in connexion.execute(f"PRAGMA table_info({table})")}:
            connexion.execute(f"ALTER TABLE {table} DROP COLUMN {colonne}")
    if "visible" in colonnes:  # ancienne colonne de masquage : sa décision est reprise une fois
        connexion.execute("UPDATE interface SET suivi = 'NON', visible = 1 WHERE visible = 0")


ELEMENTS_COMMUNS = [
    ("uptime", "1.3.6.1.2.1.1.3.0"),           # sysUpTime, scalaire
    ("if_in_octets", "1.3.6.1.2.1.2.2.1.10"),  # ifInOctets, ifTable (une valeur par interface)
    ("if_out_octets", "1.3.6.1.2.1.2.2.1.16"), # ifOutOctets, ifTable (une valeur par interface)
]


def _seed(connexion: sqlite3.Connection) -> None:
    """Insère les données de référence si elles n'existent pas encore (idempotent)."""
    connexion.execute("INSERT OR IGNORE INTO parametres_generaux (id) VALUES (1)")

    metriques = [
        ("uptime", "Disponibilité (uptime)", None, "COMPTEUR", 0),
        ("cpu", "Charge CPU", "%", "JAUGE", 0),
        ("ram", "Mémoire utilisée", "%", "JAUGE", 0),
        ("disque", "Capacité disque", "%", "JAUGE", 0),
        ("if_in_octets", "Trafic entrant", "octets", "COMPTEUR", 1),
        ("if_out_octets", "Trafic sortant", "octets", "COMPTEUR", 1),
    ]
    connexion.executemany(
        "INSERT OR IGNORE INTO metrique (code, libelle, unite, type_valeur, par_interface) "
        "VALUES (?, ?, ?, ?, ?)",
        metriques,
    )

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom) VALUES (?, 'Équipement générique')", (MODELE_GENERIQUE,),
    )
    connexion.execute(
        "UPDATE modele_supervision SET nom = 'Équipement générique', description = ? WHERE id = ?",
        ("Disponibilité, uptime et trafic des interfaces (MIB-II) : convient à tout équipement SNMP.", MODELE_GENERIQUE),
    )
    _inserer_elements_surveilles(connexion, MODELE_GENERIQUE, ELEMENTS_COMMUNS)

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom) VALUES (?, 'Hôte standard')", (MODELE_HOTE_STANDARD,),
    )
    connexion.execute(
        "UPDATE modele_supervision SET nom = 'Hôte standard', description = ? WHERE id = ?",
        ("CPU, mémoire et disques par HOST-RESOURCES-MIB, ou UCD-SNMP-MIB en secours : serveurs Linux "
         "et Windows, NAS, routeurs MikroTik, pfSense...", MODELE_HOTE_STANDARD),
    )
    _inserer_elements_surveilles(connexion, MODELE_HOTE_STANDARD, ELEMENTS_COMMUNS + [
        ("cpu", OID_HR_PROCESSOR_LOAD),   # hrProcessorLoad, moyenne des cœurs
        ("ram", OID_HR_STORAGE_USED),     # ligne « mémoire vive » de hrStorageTable
        ("disque", OID_HR_STORAGE_USED),  # disques fixes de hrStorageTable
    ])

    # Modèles des constructeurs : indicateurs CPU / mémoire / disque, à numéro fixe
    for constructeur in constructeurs():
        id_modele = constructeur["id_modele"]
        connexion.execute("INSERT OR IGNORE INTO modele_supervision (id, nom) VALUES (?, ?)", (id_modele, constructeur["nom"]))
        connexion.execute("UPDATE modele_supervision SET nom = ?, description = ? WHERE id = ?",
                          (constructeur["nom"], constructeur["description"], id_modele))
        connexion.execute("DELETE FROM element_surveille WHERE modele_supervision_id = ?", (id_modele,))
        _inserer_elements_surveilles(connexion, id_modele, ELEMENTS_COMMUNS + [tuple(e) for e in constructeur["elements"]])

    # Modèles d'objets : capteurs génériques, et ceux de chaque constructeur (limités à ses équipements)
    modeles = list(modeles_integres()["modeles"]) + [
        {**c["objets"], "systeme": c["systeme"],
         "description": f"Cherchés seulement sur les équipements reconnus comme « {c['systeme']} »"} for c in constructeurs()]
    for modele in modeles:
        connexion.execute(
            "INSERT INTO modele_objets (nom, contenu) VALUES (?, ?) ON CONFLICT (nom) DO UPDATE SET contenu = excluded.contenu",
            (modele["nom"], json.dumps(modele, ensure_ascii=False)),
        )

    for code, avertissement, critique in (("cpu", 90, 95), ("ram", 90, 95), ("disque", 85, 95)):
        connexion.execute(
            "INSERT OR IGNORE INTO seuils_config (metrique_id, valeur, valeur_critique) "
            "SELECT id, ?, ? FROM metrique WHERE code = ?",
            (avertissement, critique, code),
        )
    connexion.executemany(
        "INSERT OR IGNORE INTO criticite_service (code, libelle, criticite) VALUES (?, ?, ?)",
        [
            ("INJOIGNABLE", "Équipement injoignable (ping)", "CRITIQUE"),
            ("SNMP_EN_ECHEC", "Service SNMP sans réponse", "CRITIQUE"),
            ("RELEVE_EN_ECHEC", "Indicateur qui ne répond plus", "AVERTISSEMENT"),
            ("INTERFACE_DOWN", "Interface suivie en panne (down)", "AVERTISSEMENT"),
        ],
    )


def _inserer_elements_surveilles(connexion: sqlite3.Connection, modele_supervision_id: int,
                                  elements: list[tuple]) -> None:
    """Associe une liste de (code métrique, oid[, méthode, oid secondaire[, diviseur]]) à un modèle."""
    for code, oid, *calcul in elements:
        methode, oid_secondaire, diviseur = (list(calcul) + [None, None, None])[:3]
        metrique_id = connexion.execute(
            "SELECT id FROM metrique WHERE code = ?", (code,)
        ).fetchone()["id"]
        connexion.execute(
            "INSERT OR IGNORE INTO element_surveille "
            "(modele_supervision_id, metrique_id, oid, methode, oid_secondaire, diviseur) VALUES (?, ?, ?, ?, ?, ?)",
            (modele_supervision_id, metrique_id, oid, methode, oid_secondaire, diviseur),
        )


# ---------------------------------------------------------------- équipements

def lister_equipements() -> list[sqlite3.Row]:
    """Renvoie tous les équipements (avec le nom de leur groupe), triés par groupe (alphabétique,
    les équipements sans groupe en dernier) puis par nom."""
    with get_connection() as connexion:
        return connexion.execute(
            # "~" ne sert qu'au tri : il vient après toutes les lettres, donc les équipements
            # sans groupe se retrouvent toujours en dernier plutôt que mélangés par ordre alphabétique.
            "SELECT equipement.*, COALESCE(groupe.nom, '~') AS groupe_nom, "
            "(SELECT count(*) FROM interface WHERE interface.equipement_id = equipement.id "
            " AND interface.suivi = 'A_VALIDER' AND interface.presente = 1) AS nb_interfaces_a_valider "
            "FROM equipement LEFT JOIN groupe ON groupe.id = equipement.groupe_id "
            "ORDER BY groupe_nom, equipement.nom"
        ).fetchall()


def creer_equipement(nom: str, adresse: str, communaute_snmp: str,
                      description: str | None = None,
                      description_systeme: str | None = None,
                      modele_supervision_id: int = 1,
                      groupe_id: int | None = None,
                      version_snmp: str = "V2C",
                      parent_id: int | None = None,
                      sys_object_id: str | None = None) -> int:
    """Insère un équipement (statut initial EN_ATTENTE) et renvoie son id."""
    with get_connection() as connexion:
        curseur = connexion.execute(
            "INSERT INTO equipement "
            "(nom, adresse, communaute_snmp, description, description_systeme, "
            " modele_supervision_id, groupe_id, version_snmp, parent_id, sys_object_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nom, adresse, communaute_snmp, description, description_systeme,
             modele_supervision_id, groupe_id, version_snmp, parent_id, sys_object_id),
        )
        return curseur.lastrowid


REGLAGES_EQUIPEMENT = ("intervalle_polling", "nb_echecs_alerte", "duree_conservation", "duree_conservation_agregee")


def modifier_reglage_equipement(id_equipement: int, champ: str, valeur: int | None) -> None:
    """Change un réglage propre à un équipement (None = retour à la valeur globale)."""
    if champ not in REGLAGES_EQUIPEMENT:  # le nom de colonne ne peut pas être passé en paramètre SQL
        raise ValueError(champ)
    with get_connection() as connexion:
        connexion.execute(f"UPDATE equipement SET {champ} = ? WHERE id = ?", (valeur, id_equipement))


def services_de_l_equipement(equipement_id: int) -> list[sqlite3.Row]:
    """Services relevés sur un équipement : ceux de son modèle, sauf ceux que l'admin a écartés."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT metrique.id AS metrique_id, metrique.code, metrique.libelle "
            "FROM equipement "
            "JOIN element_surveille ON element_surveille.modele_supervision_id = equipement.modele_supervision_id "
            " AND element_surveille.actif = 1 "
            "JOIN metrique ON metrique.id = element_surveille.metrique_id "
            "WHERE equipement.id = ? AND metrique.id NOT IN "
            "(SELECT metrique_id FROM service_ignore WHERE service_ignore.equipement_id = equipement.id) "
            "ORDER BY metrique.libelle",
            (equipement_id,),
        ).fetchall()


def modifier_parametres(intervalle_polling: int, nb_echecs: int, duree_conservation: int,
                        duree_conservation_agregee: int) -> None:
    """Met à jour les réglages globaux de collecte et de conservation."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE parametres_generaux SET intervalle_polling_defaut = ?, "
            "nb_echecs_avant_indisponibilite = ?, duree_conservation_detail = ?, "
            "duree_conservation_agregee = ? WHERE id = 1",
            (intervalle_polling, nb_echecs, duree_conservation, duree_conservation_agregee),
        )


def compter_reglages_propres() -> dict[str, int]:
    """Nombre d'équipements qui ont leur propre réglage, pour chaque réglage de collecte."""
    with get_connection() as connexion:
        return {champ: connexion.execute(f"SELECT count(*) FROM equipement WHERE {champ} IS NOT NULL").fetchone()[0]
                for champ in REGLAGES_EQUIPEMENT}


HEURE = "strftime('%Y-%m-%dT%H:00:00', horodatage)"
DEBUT_HEURE = "strftime('%Y-%m-%dT%H:00:00', 'now', '-' || ? || ' days')"


def _agreger_avant_purge(connexion: sqlite3.Connection, equipement_id: int, duree: int) -> None:
    """Remplace le détail de plus de `duree` jours par ses moyennes par heure. Seules les heures
    entières sont agrégées (le détail est donc gardé jusqu'à une heure de plus), et l'insertion
    ignore une heure déjà agrégée : rejouée après une interruption, la purge ne fausse rien."""
    connexion.execute(
        "INSERT OR IGNORE INTO mesure_agregee (equipement_id, metrique_id, interface_id, heure, moyenne, "
        "minimum, maximum, utilise, total, nb) "
        f"SELECT equipement_id, metrique_id, COALESCE(interface_id, 0), {HEURE}, AVG(valeur), MIN(valeur), "
        "MAX(valeur), AVG(utilise), AVG(total), COUNT(valeur) FROM mesure "
        f"WHERE equipement_id = ? AND valeur IS NOT NULL AND horodatage < {DEBUT_HEURE} "
        "GROUP BY 1, 2, 3, 4", (equipement_id, duree))
    connexion.execute(
        "INSERT OR IGNORE INTO volume_agrege (equipement_id, nom, heure, moyenne, utilise, total) "
        f"SELECT equipement_id, nom, {HEURE}, AVG(valeur), AVG(utilise), AVG(total) FROM mesure_volume "
        f"WHERE equipement_id = ? AND horodatage < {DEBUT_HEURE} GROUP BY 1, 2, 3", (equipement_id, duree))
    connexion.execute(
        "INSERT OR IGNORE INTO objet_agrege (objet_id, instance, heure, moyenne) "
        f"SELECT objet_id, instance, {HEURE}, AVG(valeur) FROM mesure_objet "
        "WHERE objet_id IN (SELECT id FROM objet_detecte WHERE equipement_id = ?) "
        f"AND horodatage < {DEBUT_HEURE} GROUP BY 1, 2, 3", (equipement_id, duree))


def purger_donnees_anciennes() -> int:
    """Agrège puis supprime les mesures plus anciennes que la durée de conservation du détail de
    chaque équipement (la sienne, sinon la globale) ; supprime les moyennes plus anciennes que sa
    durée de conservation agrégée ; supprime aussi erreurs, changements de statut et alertes
    clôturées de plus de la durée du détail. Une alerte encore active n'est jamais supprimée.
    Agrégation et suppression se font dans la même transaction : tout ou rien. Renvoie le nombre
    de lignes de détail supprimées."""
    limite = "strftime('%Y-%m-%dT%H:%M:%S', 'now', '-' || ? || ' days')"
    total = 0
    with get_connection() as connexion:
        defaut = connexion.execute(
            "SELECT duree_conservation_detail, duree_conservation_agregee FROM parametres_generaux WHERE id = 1"
        ).fetchone()
        for equipement in connexion.execute(
                "SELECT id, duree_conservation, duree_conservation_agregee FROM equipement").fetchall():
            duree = equipement["duree_conservation"] or defaut[0]
            duree_agregee = equipement["duree_conservation_agregee"] or defaut[1]
            _agreger_avant_purge(connexion, equipement["id"], duree)
            for requete in (
                f"DELETE FROM mesure WHERE equipement_id = ? AND horodatage < {DEBUT_HEURE}",
                f"DELETE FROM mesure_volume WHERE equipement_id = ? AND horodatage < {DEBUT_HEURE}",
                "DELETE FROM mesure_objet WHERE objet_id IN (SELECT id FROM objet_detecte WHERE equipement_id = ?) "
                f"AND horodatage < {DEBUT_HEURE}",
                f"DELETE FROM erreur_collecte WHERE equipement_id = ? AND horodatage < {limite}",
                f"DELETE FROM changement_statut WHERE equipement_id = ? AND horodatage < {limite}",
                f"DELETE FROM alerte WHERE equipement_id = ? AND statut = 'CLOTUREE' AND date_fin < {limite}",
            ):
                total += connexion.execute(requete, (equipement["id"], duree)).rowcount
            for requete in (
                f"DELETE FROM mesure_agregee WHERE equipement_id = ? AND heure < {limite}",
                f"DELETE FROM volume_agrege WHERE equipement_id = ? AND heure < {limite}",
                "DELETE FROM objet_agrege WHERE objet_id IN (SELECT id FROM objet_detecte WHERE equipement_id = ?) "
                f"AND heure < {limite}",
            ):
                connexion.execute(requete, (equipement["id"], duree_agregee))
    return total


def changer_modele_supervision(id_equipement: int, modele_supervision_id: int) -> None:
    """Change le modèle de supervision d'un équipement existant."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET modele_supervision_id = ? WHERE id = ?",
            (modele_supervision_id, id_equipement),
        )


def supprimer_equipement(id_equipement: int) -> None:
    """Supprime un équipement et tout son historique (clés étrangères en cascade)."""
    with get_connection() as connexion:
        connexion.execute("DELETE FROM equipement WHERE id = ?", (id_equipement,))
        _recalculer_correlation(connexion)


def obtenir_equipement(id_equipement: int) -> sqlite3.Row | None:
    """Renvoie un équipement par son id, ou None s'il n'existe pas."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM equipement WHERE id = ?", (id_equipement,)
        ).fetchone()


def obtenir_equipement_detail(id_equipement: int) -> sqlite3.Row | None:
    """Équipement avec le nom de son groupe et de son parent, pour la fiche équipement."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT equipement.*, groupe.nom AS groupe_nom, parent.nom AS parent_nom, "
            "modele_supervision.nom AS modele_nom "
            "FROM equipement "
            "LEFT JOIN groupe ON groupe.id = equipement.groupe_id "
            "LEFT JOIN equipement AS parent ON parent.id = equipement.parent_id "
            "JOIN modele_supervision ON modele_supervision.id = equipement.modele_supervision_id "
            "WHERE equipement.id = ?",
            (id_equipement,),
        ).fetchone()


def lister_enfants(id_equipement: int) -> list[sqlite3.Row]:
    """Équipements dont le parent est celui-ci."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT id, nom, statut FROM equipement WHERE parent_id = ? ORDER BY nom",
            (id_equipement,),
        ).fetchall()


def lister_interfaces(equipement_id: int) -> list[sqlite3.Row]:
    """Interfaces d'un équipement, triées par index SNMP."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM interface WHERE equipement_id = ? ORDER BY index_snmp",
            (equipement_id,),
        ).fetchall()


def interfaces_a_mesurer(equipement_id: int) -> dict[int, int]:
    """Interfaces cochées et encore déclarées par l'équipement : {ifIndex: id de l'interface}."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT id, index_snmp FROM interface WHERE equipement_id = ? AND suivi = 'OUI' AND presente = 1",
            (equipement_id,),
        ).fetchall()
    return {ligne["index_snmp"]: ligne["id"] for ligne in lignes}


def enregistrer_choix_interfaces(equipement_id: int, affichees: list[int], surveillees: list[int]) -> None:
    """Applique le formulaire, uniquement aux interfaces affichées : une interface découverte
    entre l'affichage et l'envoi reste à valider au lieu d'être ignorée sans avoir été vue."""
    cochees = set(surveillees)
    with get_connection() as connexion:
        connexion.executemany(
            "UPDATE interface SET suivi = ? WHERE id = ? AND equipement_id = ?",
            [("OUI" if id_itf in cochees else "NON", id_itf, equipement_id) for id_itf in affichees],
        )


def marquer_interfaces_absentes(equipement_id: int, index_declares: list[int]) -> None:
    """Marque comme disparues les interfaces que l'équipement ne déclare plus."""
    marques = ", ".join("?" for _ in index_declares)
    with get_connection() as connexion:
        connexion.execute(
            f"UPDATE interface SET presente = 0 WHERE equipement_id = ? AND index_snmp NOT IN ({marques})",
            (equipement_id, *index_declares),
        )


def supprimer_interface_absente(equipement_id: int, interface_id: int) -> bool:
    """Supprime une interface disparue et ses mesures. Refuse une interface encore déclarée :
    elle reviendrait à la découverte suivante (pour ne plus la suivre, il faut la décocher)."""
    with get_connection() as connexion:
        curseur = connexion.execute(
            "DELETE FROM interface WHERE id = ? AND equipement_id = ? AND presente = 0",
            (interface_id, equipement_id),
        )
        return curseur.rowcount > 0


def lister_changements_statut(equipement_id: int, limite: int = 20) -> list[sqlite3.Row]:
    """Historique des changements de statut d'un équipement, le plus récent en premier."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM changement_statut WHERE equipement_id = ? "
            "ORDER BY horodatage DESC, id DESC LIMIT ?",
            (equipement_id, limite),
        ).fetchall()


def lister_erreurs_collecte(equipement_id: int, limite: int = 20) -> list[sqlite3.Row]:
    """Dernières erreurs de collecte d'un équipement, la plus récente en premier."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM erreur_collecte WHERE equipement_id = ? "
            "ORDER BY horodatage DESC LIMIT ?",
            (equipement_id, limite),
        ).fetchall()


def lister_mesures(equipement_id: int, code_metrique: str, interface_id: int | None = None,
                    depuis: str | None = None, jusqu_a: str | None = None,
                    agrege: bool = False) -> list[sqlite3.Row]:
    """Mesures d'un équipement pour une métrique donnée (et, si fournie, une interface
    donnée), triées par horodatage croissant. Avec `agrege`, les moyennes par heure du détail
    déjà purgé sont placées avant les mesures détaillées (pour un compteur d'octets, le dernier
    relevé de l'heure)."""
    condition = "mesure.equipement_id = ? AND metrique.code = ?"
    parametres: list = [equipement_id, code_metrique]
    condition_agregat = "a.equipement_id = ? AND metrique.code = ?"
    parametres_agregat: list = [equipement_id, code_metrique]
    if interface_id is not None:
        condition += " AND mesure.interface_id = ?"
        parametres.append(interface_id)
        condition_agregat += " AND a.interface_id = ?"
        parametres_agregat.append(interface_id)
    if depuis:
        condition += " AND mesure.horodatage >= ?"
        parametres.append(depuis)
        condition_agregat += " AND a.heure >= ?"
        parametres_agregat.append(depuis)
    if jusqu_a:
        condition += " AND mesure.horodatage <= ?"
        parametres.append(jusqu_a)
        condition_agregat += " AND a.heure <= ?"
        parametres_agregat.append(jusqu_a)
    with get_connection() as connexion:
        anciennes = connexion.execute(
            "SELECT a.heure AS horodatage, "
            "CASE WHEN metrique.type_valeur = 'COMPTEUR' THEN a.maximum ELSE a.moyenne END AS valeur, "
            "NULLIF(a.interface_id, 0) AS interface_id, a.utilise, a.total, NULL AS detail "
            "FROM mesure_agregee a JOIN metrique ON metrique.id = a.metrique_id "
            f"WHERE {condition_agregat} ORDER BY a.heure", parametres_agregat,
        ).fetchall() if agrege else []
        recentes = connexion.execute(
            "SELECT mesure.horodatage, mesure.valeur, mesure.interface_id, mesure.utilise, mesure.total, mesure.detail "
            "FROM mesure JOIN metrique ON metrique.id = mesure.metrique_id "
            f"WHERE {condition} ORDER BY mesure.horodatage",
            parametres,
        ).fetchall()
    return anciennes + recentes


def trouver_equipement(recherche: str, exact: bool = False) -> sqlite3.Row | None:
    """Trouve un équipement par nom ou adresse exacte, sinon (sauf si `exact`) par correspondance
    partielle. Renvoie None si rien ne correspond ou si plusieurs équipements correspondent."""
    with get_connection() as connexion:
        resultats = connexion.execute(
            "SELECT * FROM equipement WHERE nom = ? OR adresse = ?", (recherche, recherche)
        ).fetchall()
        if not resultats and not exact:
            # % et _ sont des jokers pour LIKE : on les échappe pour les chercher tels quels
            motif = recherche.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            resultats = connexion.execute(
                "SELECT * FROM equipement WHERE nom LIKE ? ESCAPE '\\' OR adresse LIKE ? ESCAPE '\\'",
                (f"%{motif}%", f"%{motif}%"),
            ).fetchall()
        return resultats[0] if len(resultats) == 1 else None


def modifier_equipement(id_equipement: int, nom: str, adresse: str, communaute_snmp: str,
                         description: str | None, groupe_id: int | None,
                         description_systeme: str | None, version_snmp: str = "V2C",
                         parent_id: int | None = None, sys_object_id: str | None = None) -> None:
    """Met à jour les informations d'un équipement existant."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET nom = ?, adresse = ?, communaute_snmp = ?, description = ?, "
            "groupe_id = ?, description_systeme = ?, version_snmp = ?, parent_id = ?, "
            "sys_object_id = ? WHERE id = ?",
            (nom, adresse, communaute_snmp, description, groupe_id, description_systeme,
             version_snmp, parent_id, sys_object_id, id_equipement),
        )
        _recalculer_correlation(connexion)


def parent_creerait_un_cycle(id_equipement: int, parent_id: int) -> bool:
    """Vrai si l'équipement se retrouverait parmi ses propres ancêtres."""
    with get_connection() as connexion:
        parents = {ligne["id"]: ligne["parent_id"]
                   for ligne in connexion.execute("SELECT id, parent_id FROM equipement")}
    vus = set()
    courant = parent_id
    while courant is not None and courant not in vus:
        if courant == id_equipement:
            return True
        vus.add(courant)
        courant = parents.get(courant)
    return False


# ---------------------------------------------------------------- modèles de supervision

def lister_modeles() -> list[sqlite3.Row]:
    """Renvoie tous les modèles de supervision."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM modele_supervision ORDER BY id").fetchall()


def lister_modeles_detail() -> list[sqlite3.Row]:
    """Modèles avec leur nombre d'équipements et leurs services, pour la section Modèles."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT modele_supervision.*, "
            "(SELECT count(*) FROM equipement WHERE equipement.modele_supervision_id = modele_supervision.id) "
            "AS nb_equipements, "
            "(SELECT group_concat(nom, ', ') FROM (SELECT equipement.nom FROM equipement "
            " WHERE equipement.modele_supervision_id = modele_supervision.id ORDER BY equipement.nom)) AS noms_equipements, "
            "(SELECT group_concat(metrique.code, ', ') FROM element_surveille "
            " JOIN metrique ON metrique.id = element_surveille.metrique_id "
            " WHERE element_surveille.modele_supervision_id = modele_supervision.id AND metrique.code IN "
            " ('cpu', 'ram', 'disque')) AS indicateurs "
            "FROM modele_supervision ORDER BY id"
        ).fetchall()


def lister_modeles_objets() -> list[dict]:
    """Modèles d'objets, avec le nombre d'objets détectés et d'équipements concernés (les génériques
    d'abord, puis ceux des constructeurs)."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT contenu, "
            "(SELECT count(*) FROM objet_detecte WHERE objet_detecte.modele_nom = modele_objets.nom) AS nb_objets, "
            # Équipements de ce constructeur, ou sur lesquels ses objets ont été détectés
            # (noms qualifiés : « nom » désignerait sinon celui de l'équipement)
            "(SELECT count(*) FROM equipement WHERE equipement.systeme = json_extract(modele_objets.contenu, '$.systeme') "
            " OR equipement.id IN (SELECT objet_detecte.equipement_id FROM objet_detecte "
            "  WHERE objet_detecte.modele_nom = modele_objets.nom)) AS nb_equipements, "
            "(SELECT group_concat(nom, ', ') FROM (SELECT equipement.nom FROM equipement "
            " WHERE equipement.systeme = json_extract(modele_objets.contenu, '$.systeme') "
            " OR equipement.id IN (SELECT objet_detecte.equipement_id FROM objet_detecte "
            "  WHERE objet_detecte.modele_nom = modele_objets.nom) ORDER BY equipement.nom)) AS noms_equipements "
            "FROM modele_objets ORDER BY json_extract(contenu, '$.systeme') IS NOT NULL, nom"
        ).fetchall()
    return [{**json.loads(ligne["contenu"]), "nb_objets": ligne["nb_objets"], "nb_equipements": ligne["nb_equipements"],
             "noms_equipements": ligne["noms_equipements"]} for ligne in lignes]


def definir_systeme(id_equipement: int, systeme: str | None) -> None:
    with get_connection() as connexion:
        connexion.execute("UPDATE equipement SET systeme = ? WHERE id = ?", (systeme, id_equipement))


def identification_terminee(id_equipement: int) -> None:
    with get_connection() as connexion:
        connexion.execute("UPDATE equipement SET identification_requise = 0 WHERE id = ?", (id_equipement,))


def identification_a_refaire(id_equipement: int) -> None:
    with get_connection() as connexion:
        connexion.execute("UPDATE equipement SET identification_requise = 1 WHERE id = ?", (id_equipement,))


def equipements_a_identifier() -> list[int]:
    with get_connection() as connexion:
        return [ligne["id"] for ligne in connexion.execute(
            "SELECT id FROM equipement WHERE identification_requise = 1 AND actif = 1"
        )]


def remplacer_objets_detectes(equipement_id: int, modele_nom: str, objets: list[dict],
                              mibs_interrogees: set[str]) -> dict[tuple[str, str], int]:
    """Enregistre le résultat d'une détection : un objet déjà connu garde son historique. Un objet
    absent n'est retiré que si sa MIB a répondu : une panne passagère n'efface donc pas l'historique.
    Renvoie {(mib, objet): id}."""
    with get_connection() as connexion:
        cles = set()
        for objet in objets:
            cles.add((objet["mib"], objet["objet"]))
            connexion.execute(
                "INSERT INTO objet_detecte (equipement_id, modele_nom, mib, objet, oid, table_nom, nature, description, etats) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (equipement_id, mib, objet) DO UPDATE SET "
                "oid = excluded.oid, table_nom = excluded.table_nom, nature = excluded.nature, "
                "description = excluded.description, etats = excluded.etats",
                (equipement_id, modele_nom, objet["mib"], objet["objet"], objet["oid"], objet["table"],
                 objet["nature"], objet["description"], json.dumps(objet["etats"])),
            )
        identifiants = {}
        for ligne in connexion.execute(
            "SELECT id, mib, objet FROM objet_detecte WHERE equipement_id = ? AND modele_nom = ?",
            (equipement_id, modele_nom),
        ).fetchall():
            if (ligne["mib"], ligne["objet"]) in cles:
                identifiants[(ligne["mib"], ligne["objet"])] = ligne["id"]
            elif ligne["mib"] in mibs_interrogees:
                connexion.execute("DELETE FROM objet_detecte WHERE id = ?", (ligne["id"],))
        return identifiants


def objets_ignores(equipement_id: int) -> set[int]:
    with get_connection() as connexion:
        return {ligne[0] for ligne in connexion.execute(
            "SELECT id FROM objet_detecte WHERE equipement_id = ? AND garde = 0", (equipement_id,))}


def choisir_objets_gardes(equipement_id: int, gardes: set[int]) -> None:
    """Garde les objets cochés ; les autres ne sont plus affichés ni relevés, et leur historique est effacé."""
    with get_connection() as connexion:
        identifiants = [ligne[0] for ligne in connexion.execute(
            "SELECT id FROM objet_detecte WHERE equipement_id = ?", (equipement_id,))]
        for identifiant in identifiants:
            garde = int(identifiant in gardes)
            connexion.execute("UPDATE objet_detecte SET garde = ? WHERE id = ?", (garde, identifiant))
            if not garde:
                connexion.execute("DELETE FROM valeur_objet WHERE objet_id = ?", (identifiant,))
                connexion.execute("DELETE FROM mesure_objet WHERE objet_id = ?", (identifiant,))


def lister_objets_detectes(equipement_id: int) -> list[sqlite3.Row]:
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM objet_detecte WHERE equipement_id = ? ORDER BY mib, table_nom, id", (equipement_id,),
        ).fetchall()


def objets_avec_historique(equipement_id: int) -> set[int]:
    with get_connection() as connexion:
        return {ligne[0] for ligne in connexion.execute(
            "SELECT DISTINCT objet_id FROM mesure_objet WHERE objet_id IN "
            "(SELECT id FROM objet_detecte WHERE equipement_id = ?)", (equipement_id,),
        )}


def enregistrer_releve_objets(valeurs: list[tuple], mesures: list[tuple]) -> None:
    """Enregistre en une transaction les dernières valeurs et l'historique d'un relevé. Les valeurs
    précédentes de ces objets sont remplacées : un disque retiré disparaît de la liste."""
    with get_connection() as connexion:
        connexion.executemany("DELETE FROM valeur_objet WHERE objet_id = ?", {(valeur[0],) for valeur in valeurs})
        connexion.executemany(
            "INSERT OR REPLACE INTO valeur_objet (objet_id, instance, valeur) VALUES (?, ?, ?)", valeurs,
        )
        connexion.executemany(
            "INSERT INTO mesure_objet (objet_id, instance, valeur) VALUES (?, ?, ?)", mesures,
        )


def lister_valeurs_objets(equipement_id: int) -> list[sqlite3.Row]:
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT valeur_objet.* FROM valeur_objet JOIN objet_detecte ON objet_detecte.id = valeur_objet.objet_id "
            "WHERE objet_detecte.equipement_id = ?", (equipement_id,),
        ).fetchall()


def lister_mesures_objet(equipement_id: int, objet_id: int, agrege: bool = False) -> list[sqlite3.Row]:
    requete = ("SELECT mesure_objet.instance, mesure_objet.horodatage, mesure_objet.valeur FROM mesure_objet "
               "JOIN objet_detecte ON objet_detecte.id = mesure_objet.objet_id "
               "WHERE mesure_objet.objet_id = ? AND objet_detecte.equipement_id = ?")
    parametres: tuple = (objet_id, equipement_id)
    if agrege:
        requete = ("SELECT a.instance, a.heure AS horodatage, a.moyenne AS valeur FROM objet_agrege a "
                   "JOIN objet_detecte ON objet_detecte.id = a.objet_id "
                   "WHERE a.objet_id = ? AND objet_detecte.equipement_id = ? UNION ALL " + requete)
        parametres = parametres + parametres
    with get_connection() as connexion:
        return connexion.execute(requete + " ORDER BY horodatage", parametres).fetchall()


# ---------------------------------------------------------------- groupes

def lister_groupes() -> list[sqlite3.Row]:
    """Renvoie tous les groupes, triés par nom, avec leur nombre d'équipements."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT groupe.*, (SELECT count(*) FROM equipement WHERE equipement.groupe_id = groupe.id) "
            "AS nb_equipements FROM groupe ORDER BY nom"
        ).fetchall()


def supprimer_groupe(id_groupe: int) -> bool:
    """Supprime un groupe ; ses équipements restent, sans groupe. Renvoie False s'il n'existe pas."""
    with get_connection() as connexion:
        return connexion.execute("DELETE FROM groupe WHERE id = ?", (id_groupe,)).rowcount > 0


def creer_groupe(nom: str) -> int:
    """Insère un groupe et renvoie son id."""
    with get_connection() as connexion:
        curseur = connexion.execute("INSERT INTO groupe (nom) VALUES (?)", (nom,))
        return curseur.lastrowid


# ---------------------------------------------------------------- collecte

def lister_equipements_actifs() -> list[sqlite3.Row]:
    """Renvoie les équipements à collecter (actif = 1)."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM equipement WHERE actif = 1").fetchall()


def obtenir_parametres_generaux() -> sqlite3.Row:
    """Renvoie l'unique ligne de réglages globaux."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM parametres_generaux WHERE id = 1").fetchone()


def lister_elements_actifs(modele_supervision_id: int) -> list[sqlite3.Row]:
    """Éléments surveillés actifs d'un modèle, avec le code et les propriétés de leur métrique."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT element_surveille.oid, element_surveille.methode, element_surveille.oid_secondaire, "
            "element_surveille.diviseur, "
            "metrique.id AS metrique_id, metrique.code, "
            "metrique.type_valeur, metrique.par_interface "
            "FROM element_surveille JOIN metrique ON metrique.id = element_surveille.metrique_id "
            "WHERE element_surveille.modele_supervision_id = ? AND element_surveille.actif = 1",
            (modele_supervision_id,),
        ).fetchall()


def enregistrer_mesure(equipement_id: int, metrique_id: int, valeur: float | None,
                        interface_id: int | None = None, utilise: float | None = None,
                        total: float | None = None, detail: str | None = None) -> None:
    """Enregistre un relevé. valeur = None si l'équipement n'a pas répondu pour cet indicateur.
    Pour la RAM et le disque, `utilise` et `total` donnent les tailles en octets."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO mesure (equipement_id, metrique_id, interface_id, horodatage, valeur, utilise, total, detail) "
            "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), ?, ?, ?, ?)",
            (equipement_id, metrique_id, interface_id, valeur, utilise, total, detail),
        )


def enregistrer_volumes(equipement_id: int, volumes: list[tuple]) -> None:
    """Enregistre chaque volume d'un relevé disque : (nom, %, octets utilisés, octets au total)."""
    with get_connection() as connexion:
        connexion.executemany(
            "INSERT INTO mesure_volume (equipement_id, horodatage, nom, valeur, utilise, total) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), ?, ?, ?, ?)",
            [(equipement_id, *volume) for volume in volumes],
        )


def lister_volumes(equipement_id: int, depuis: str | None = None, jusqu_a: str | None = None,
                   agrege: bool = False) -> list[sqlite3.Row]:
    condition, parametres = "equipement_id = ?", [equipement_id]
    if depuis:
        condition += " AND horodatage >= ?"
        parametres.append(depuis)
    if jusqu_a:
        condition += " AND horodatage <= ?"
        parametres.append(jusqu_a)
    requete = (f"SELECT nom, horodatage, valeur, utilise, total FROM mesure_volume WHERE {condition}")
    if agrege:
        requete = ("SELECT nom, heure AS horodatage, moyenne AS valeur, utilise, total FROM volume_agrege "
                   f"WHERE {condition.replace('horodatage', 'heure')} UNION ALL " + requete)
        parametres = parametres + parametres
    with get_connection() as connexion:
        return connexion.execute(requete + " ORDER BY horodatage, nom", parametres).fetchall()


def derniers_volumes(equipement_id: int) -> list[sqlite3.Row]:
    """Volumes du dernier relevé disque, du plus rempli au moins rempli."""
    with get_connection() as connexion:
        return connexion.execute(
            # Le dernier relevé de chaque volume encore présent au relevé le plus récent (deux relevés
            # peuvent tomber dans la même seconde)
            "SELECT nom, valeur, utilise, total, horodatage FROM mesure_volume WHERE id IN "
            "(SELECT max(id) FROM mesure_volume WHERE equipement_id = ? AND "
            " horodatage = (SELECT max(horodatage) FROM mesure_volume WHERE equipement_id = ?) GROUP BY nom) "
            "ORDER BY valeur DESC",
            (equipement_id, equipement_id),
        ).fetchall()


def dernieres_mesures(equipement_id: int) -> dict[str, sqlite3.Row]:
    """Dernier relevé de chaque indicateur global (CPU, RAM, disque, uptime) : {code: ligne}."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT metrique.code, mesure.valeur, mesure.utilise, mesure.total, mesure.detail, mesure.horodatage "
            "FROM mesure JOIN metrique ON metrique.id = mesure.metrique_id "
            "WHERE mesure.id IN (SELECT max(id) FROM mesure WHERE equipement_id = ? AND interface_id IS NULL "
            "GROUP BY metrique_id)", (equipement_id,),
        ).fetchall()
    return {ligne["code"]: ligne for ligne in lignes}


def upsert_interface(equipement_id: int, index_snmp: int, nom: str,
                      vitesse: int | None, statut: str) -> int:
    """Crée ou met à jour une interface détectée. Une interface déjà connue garde sa décision ;
    une nouvelle reprend celle d'une interface de même nom (l'index SNMP peut changer au
    redémarrage de l'équipement), sinon elle reste à valider."""
    with get_connection() as connexion:
        connue = connexion.execute(
            "SELECT nom, suivi FROM interface WHERE equipement_id = ? AND index_snmp = ?",
            (equipement_id, index_snmp),
        ).fetchone()
        if connue and connue["nom"] == nom:
            suivi = connue["suivi"]
        else:
            meme_nom = connexion.execute(
                "SELECT suivi FROM interface WHERE equipement_id = ? AND nom = ? AND index_snmp != ? "
                "AND suivi != 'A_VALIDER' ORDER BY id DESC LIMIT 1",
                (equipement_id, nom, index_snmp),
            ).fetchone()
            suivi = meme_nom["suivi"] if meme_nom else "A_VALIDER"
        connexion.execute(
            "INSERT INTO interface (equipement_id, index_snmp, nom, vitesse, statut, suivi, presente) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT (equipement_id, index_snmp) DO UPDATE SET "
            "nom = excluded.nom, vitesse = excluded.vitesse, statut = excluded.statut, "
            "suivi = excluded.suivi, presente = 1",
            (equipement_id, index_snmp, nom, vitesse, statut, suivi),
        )
        return connexion.execute(
            "SELECT id FROM interface WHERE equipement_id = ? AND index_snmp = ?",
            (equipement_id, index_snmp),
        ).fetchone()["id"]


def enregistrer_erreur_collecte(equipement_id: int, type_erreur: str, message: str | None = None) -> None:
    """Journalise une erreur de collecte, sans jamais interrompre la collecte des autres équipements."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO erreur_collecte (equipement_id, type_erreur, message) VALUES (?, ?, ?)",
            (equipement_id, type_erreur, message),
        )


def _changer_statut(connexion: sqlite3.Connection, equipement_id: int,
                     ancien_statut: str, nouveau_statut: str) -> None:
    """Met à jour equipement.statut et trace le changement. Appelée à l'intérieur d'une
    connexion déjà ouverte, pour que la mise à jour et la trace soient dans la même transaction."""
    connexion.execute("UPDATE equipement SET statut = ? WHERE id = ?", (nouveau_statut, equipement_id))
    connexion.execute(
        "INSERT INTO changement_statut (equipement_id, ancien_statut, nouveau_statut) VALUES (?, ?, ?)",
        (equipement_id, ancien_statut, nouveau_statut),
    )


def enregistrer_reussite_collecte(equipement_id: int, statut: str = "OPERATIONNEL") -> None:
    """Après une collecte réussie (l'équipement a répondu) : échecs à zéro, horodatage, et
    mise à jour du statut (OPERATIONNEL, ou DEGRADE si un service est en alerte)."""
    with get_connection() as connexion:
        ancien_statut = connexion.execute(
            "SELECT statut FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()["statut"]
        connexion.execute(
            "UPDATE equipement SET nb_echecs_consecutifs = 0, "
            "derniere_collecte = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
            (equipement_id,),
        )
        if ancien_statut != statut:
            _changer_statut(connexion, equipement_id, ancien_statut, statut)


def enregistrer_echec_collecte(equipement_id: int, seuil_echecs: int, statut: str) -> int:
    """Après un échec SNMP : incrémente les échecs consécutifs, applique le statut une fois le
    seuil atteint (jamais sur un seul échec isolé) et renvoie le nombre d'échecs."""
    with get_connection() as connexion:
        ligne = connexion.execute(
            "SELECT statut, nb_echecs_consecutifs FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()
        nouveaux_echecs = ligne["nb_echecs_consecutifs"] + 1
        connexion.execute(
            "UPDATE equipement SET nb_echecs_consecutifs = ? WHERE id = ?",
            (nouveaux_echecs, equipement_id),
        )
        if nouveaux_echecs >= seuil_echecs and ligne["statut"] != statut:
            _changer_statut(connexion, equipement_id, ligne["statut"], statut)
        return nouveaux_echecs


def derniere_valeur(equipement_id: int, metrique_id: int) -> float | None:
    """Dernier relevé d'un indicateur ; None s'il n'y en a pas ou si le dernier a échoué."""
    with get_connection() as connexion:
        ligne = connexion.execute(
            "SELECT valeur FROM mesure WHERE equipement_id = ? AND metrique_id = ? ORDER BY id DESC LIMIT 1",
            (equipement_id, metrique_id),
        ).fetchone()
    return ligne["valeur"] if ligne else None


def actualiser_statut_service(equipement_id: int) -> None:
    """Fait suivre au statut d'un équipement joignable ses alertes de service encore actives."""
    with get_connection() as connexion:
        ancien_statut = connexion.execute(
            "SELECT statut FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()["statut"]
        degrade = connexion.execute(
            "SELECT 1 FROM alerte WHERE equipement_id = ? AND statut = 'ACTIVE' AND categorie = 'SERVICE' LIMIT 1",
            (equipement_id,),
        ).fetchone() is not None
        statut = "DEGRADE" if degrade else "OPERATIONNEL"
        if ancien_statut in ("OPERATIONNEL", "DEGRADE") and ancien_statut != statut:
            _changer_statut(connexion, equipement_id, ancien_statut, statut)


def releve_en_echec_persistant(equipement_id: int, metrique_id: int, nb_releves: int) -> bool:
    """Vrai si un indicateur déjà relevé par le passé échoue depuis nb_releves collectes. Un
    indicateur que l'équipement n'a jamais fourni n'est pas une panne : il n'est pas supporté."""
    with get_connection() as connexion:
        recents = connexion.execute(
            "SELECT valeur FROM mesure WHERE equipement_id = ? AND metrique_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (equipement_id, metrique_id, nb_releves),
        ).fetchall()
        deja_releve = connexion.execute(
            "SELECT 1 FROM mesure WHERE equipement_id = ? AND metrique_id = ? AND valeur IS NOT NULL LIMIT 1",
            (equipement_id, metrique_id),
        ).fetchone()
    return bool(deja_releve) and len(recents) == nb_releves and all(r["valeur"] is None for r in recents)


# ---------------------------------------------------------------- seuils

def seuils_par_defaut() -> dict[int, sqlite3.Row]:
    """Seuils appliqués à CPU, RAM et disque tant qu'un équipement n'a pas réglé les siens."""
    with get_connection() as connexion:
        return {ligne["metrique_id"]: ligne for ligne in connexion.execute("SELECT * FROM seuils_config")}


def seuils_enregistres(equipement_id: int) -> dict[tuple[str, int], sqlite3.Row]:
    """Seuils réglés sur un équipement : {("m", metrique_id) ou ("o", objet_id): seuil}."""
    with get_connection() as connexion:
        lignes = connexion.execute("SELECT * FROM seuil WHERE equipement_id = ?", (equipement_id,)).fetchall()
    return {("m", ligne["metrique_id"]) if ligne["metrique_id"] else ("o", ligne["objet_id"]): ligne for ligne in lignes}


def enregistrer_seuil(equipement_id: int, metrique_id: int | None, objet_id: int | None, operateur: str,
                      avertissement: float | None, critique: float | None, actif: bool) -> None:
    """Crée ou remplace le seuil d'un service de mesure ou d'un objet détecté de cet équipement."""
    with get_connection() as connexion:
        connexion.execute(
            "DELETE FROM seuil WHERE equipement_id = ? AND COALESCE(metrique_id, 0) = ? AND COALESCE(objet_id, 0) = ?",
            (equipement_id, metrique_id or 0, objet_id or 0),
        )
        connexion.execute(
            "INSERT INTO seuil (equipement_id, metrique_id, objet_id, operateur, avertissement, critique, actif) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (equipement_id, metrique_id, objet_id, operateur, avertissement, critique, int(actif)),
        )


def supprimer_seuils_equipement(equipement_id: int) -> None:
    """Retire tous les seuils réglés sur un équipement (remise à zéro)."""
    with get_connection() as connexion:
        connexion.execute("DELETE FROM seuil WHERE equipement_id = ?", (equipement_id,))


def supprimer_seuil(equipement_id: int, metrique_id: int | None, objet_id: int | None) -> None:
    """Retire le seuil réglé : CPU, RAM et disque reviennent à leur valeur par défaut, les autres
    services n'ont plus de seuil."""
    with get_connection() as connexion:
        connexion.execute(
            "DELETE FROM seuil WHERE equipement_id = ? AND COALESCE(metrique_id, 0) = ? AND COALESCE(objet_id, 0) = ?",
            (equipement_id, metrique_id or 0, objet_id or 0),
        )


def metriques_ignorees(equipement_id: int) -> set[int]:
    with get_connection() as connexion:
        return {ligne[0] for ligne in connexion.execute(
            "SELECT metrique_id FROM service_ignore WHERE equipement_id = ?", (equipement_id,))}


def choisir_metriques_gardees(equipement_id: int, gardees: set[int]) -> None:
    """Les services de mesure du modèle non cochés ne sont plus relevés (leur historique est gardé)."""
    with get_connection() as connexion:
        connexion.execute("DELETE FROM service_ignore WHERE equipement_id = ?", (equipement_id,))
        connexion.executemany(
            "INSERT INTO service_ignore (equipement_id, metrique_id) "
            "SELECT ?, element_surveille.metrique_id FROM equipement JOIN element_surveille "
            " ON element_surveille.modele_supervision_id = equipement.modele_supervision_id "
            "WHERE equipement.id = ? AND element_surveille.metrique_id = ?",
            [(equipement_id, equipement_id, m) for m in _metriques_du_modele(connexion, equipement_id) - gardees],
        )


def _metriques_du_modele(connexion: sqlite3.Connection, equipement_id: int) -> set[int]:
    return {ligne[0] for ligne in connexion.execute(
        "SELECT element_surveille.metrique_id FROM equipement JOIN element_surveille "
        " ON element_surveille.modele_supervision_id = equipement.modele_supervision_id AND element_surveille.actif = 1 "
        "WHERE equipement.id = ?", (equipement_id,))}


def metriques_du_modele(equipement_id: int) -> list[sqlite3.Row]:
    """Services de mesure du modèle de l'équipement, avec leur choix (gardé ou non)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT metrique.id AS metrique_id, metrique.code, metrique.libelle, metrique.par_interface, "
            "NOT EXISTS (SELECT 1 FROM service_ignore WHERE service_ignore.equipement_id = equipement.id "
            " AND service_ignore.metrique_id = metrique.id) AS garde "
            "FROM equipement JOIN element_surveille ON element_surveille.modele_supervision_id = equipement.modele_supervision_id "
            " AND element_surveille.actif = 1 "
            "JOIN metrique ON metrique.id = element_surveille.metrique_id WHERE equipement.id = ? ORDER BY metrique.id",
            (equipement_id,),
        ).fetchall()


def deux_dernieres_mesures(equipement_id: int, metrique_id: int, interface_id: int) -> list[sqlite3.Row]:
    """Les deux derniers relevés d'un compteur d'interface, du plus ancien au plus récent (débit)."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT horodatage, valeur FROM mesure WHERE equipement_id = ? AND metrique_id = ? AND interface_id = ? "
            "ORDER BY id DESC LIMIT 2", (equipement_id, metrique_id, interface_id),
        ).fetchall()
    return list(reversed(lignes))


# ---------------------------------------------------------------- alertes

def _recalculer_correlation(connexion: sqlite3.Connection) -> None:
    """Relie chaque panne active à la panne de son ancêtre le plus haut, ou à rien. Tout est
    recalculé à chaque changement : le résultat ne dépend donc pas de l'équipement détecté en
    premier, et il suit les changements de relation parent-enfant."""
    pannes = {ligne["equipement_id"]: ligne["id"] for ligne in connexion.execute(
        "SELECT id, equipement_id FROM alerte WHERE statut = 'ACTIVE' AND code = 'INJOIGNABLE'"
    )}
    parents = {ligne["id"]: ligne["parent_id"]
               for ligne in connexion.execute("SELECT id, parent_id FROM equipement")}
    for equipement_id, alerte_id in pannes.items():
        racine = None
        vus = {equipement_id}
        courant = parents.get(equipement_id)
        while courant is not None and courant not in vus:
            vus.add(courant)
            if courant in pannes:
                racine = pannes[courant]
            courant = parents.get(courant)
        connexion.execute("UPDATE alerte SET cause_racine_id = ? WHERE id = ?", (racine, alerte_id))


def ouvrir_alerte(equipement_id: int, categorie: str, code: str, message: str,
                   metrique_id: int | None = None, criticite: str | None = None, objet_id: int | None = None,
                   interface_id: int | None = None) -> None:
    """Ouvre une alerte, ou actualise son message et sa criticité si elle est déjà active. Sans
    criticité fournie, c'est celle configurée pour ce type d'alerte qui s'applique."""
    with get_connection() as connexion:
        if criticite is None:
            criticite = connexion.execute(
                "SELECT criticite FROM criticite_service WHERE code = ?", (code,)
            ).fetchone()["criticite"]
        connexion.execute(
            "INSERT OR IGNORE INTO alerte (equipement_id, categorie, code, metrique_id, objet_id, interface_id, "
            "message, criticite) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (equipement_id, categorie, code, metrique_id, objet_id, interface_id, message, criticite),
        )
        connexion.execute(
            "UPDATE alerte SET message = ?, criticite = ? WHERE equipement_id = ? AND code = ? "
            "AND COALESCE(metrique_id, 0) = ? AND COALESCE(objet_id, 0) = ? AND COALESCE(interface_id, 0) = ? "
            "AND statut = 'ACTIVE'",
            (message, criticite, equipement_id, code, metrique_id or 0, objet_id or 0, interface_id or 0),
        )
        _recalculer_correlation(connexion)


def _cloturer(connexion: sqlite3.Connection, condition: str, parametres: tuple) -> None:
    connexion.execute(
        "UPDATE alerte SET statut = 'CLOTUREE', date_fin = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
        f"WHERE statut = 'ACTIVE' AND {condition}",
        parametres,
    )
    _recalculer_correlation(connexion)


def cloturer_alerte(equipement_id: int, code: str, metrique_id: int | None = None,
                    objet_id: int | None = None, interface_id: int | None = None) -> None:
    """Clôture une alerte active précise, si elle existe."""
    with get_connection() as connexion:
        _cloturer(connexion, "equipement_id = ? AND code = ? AND COALESCE(metrique_id, 0) = ? "
                  "AND COALESCE(objet_id, 0) = ? AND COALESCE(interface_id, 0) = ?",
                  (equipement_id, code, metrique_id or 0, objet_id or 0, interface_id or 0))


def cloturer_alertes_sauf(equipement_id: int, code: str) -> None:
    """Clôture les autres alertes d'un équipement quand on ne peut plus les vérifier (panne)."""
    with get_connection() as connexion:
        _cloturer(connexion, "equipement_id = ? AND code != ?", (equipement_id, code))


def a_alerte_service_active(equipement_id: int) -> bool:
    """Vrai si un service de l'équipement est en alerte (donc équipement dégradé)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT 1 FROM alerte WHERE equipement_id = ? AND statut = 'ACTIVE' AND categorie = 'SERVICE' LIMIT 1",
            (equipement_id,),
        ).fetchone() is not None


def lister_alertes(statut: str | None = None, equipement_id: int | None = None,
                    limite: int = 200) -> list[sqlite3.Row]:
    """Alertes critiques puis les plus récentes d'abord, avec l'équipement concerné, le nom de leur cause
    racine et la liste des équipements impactés en aval."""
    conditions, parametres = ["1 = 1"], []
    if statut:
        conditions.append("alerte.statut = ?")
        parametres.append(statut)
    if equipement_id is not None:
        conditions.append("alerte.equipement_id = ?")
        parametres.append(equipement_id)
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT alerte.*, equipement.nom AS equipement_nom, racine_eq.nom AS racine_nom, "
            "(SELECT group_concat(aval.nom, ', ') FROM alerte AS impact "
            " JOIN equipement AS aval ON aval.id = impact.equipement_id "
            " WHERE impact.cause_racine_id = alerte.id AND impact.statut = 'ACTIVE') AS impactes "
            "FROM alerte JOIN equipement ON equipement.id = alerte.equipement_id "
            "LEFT JOIN alerte AS racine ON racine.id = alerte.cause_racine_id AND alerte.statut = 'ACTIVE' "
            "LEFT JOIN equipement AS racine_eq ON racine_eq.id = racine.equipement_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY alerte.criticite = 'CRITIQUE' DESC, alerte.date_debut DESC, alerte.id DESC LIMIT ?",
            (*parametres, limite),
        ).fetchall()


CATEGORIES_LOGS = ("STATUT", "ERREUR", "AVERTISSEMENT", "CRITIQUE")


def _requete_logs(equipement_id: int | None, depuis: str | None, jusqu_a: str | None,
                  categorie: str | None, recherche: str | None) -> tuple[str, list]:
    """Fil unifié des logs : changements de statut, erreurs de collecte et alertes (catégorie =
    leur criticité), avec les filtres demandés. Renvoie (requête, paramètres)."""
    conditions, parametres = ["1 = 1"], []
    if equipement_id is not None:
        conditions.append("equipement_id = ?")
        parametres.append(equipement_id)
    if depuis:
        conditions.append("horodatage >= ?")
        parametres.append(depuis)
    if jusqu_a:
        conditions.append("horodatage <= ?")
        parametres.append(jusqu_a)
    if categorie in CATEGORIES_LOGS:
        conditions.append("categorie = ?")
        parametres.append(categorie)
    if recherche:
        # % et _ sont des jokers pour LIKE : on les échappe pour les chercher tels quels
        motif = recherche.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            "(equipement_nom LIKE ? ESCAPE '\\' OR adresse LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\')"
        )
        parametres += [f"%{motif}%"] * 3
    requete = (
        "SELECT * FROM ("
        " SELECT 'STATUT' AS categorie, c.horodatage, e.id AS equipement_id, e.nom AS equipement_nom, e.adresse, "
        " c.ancien_statut || ' → ' || c.nouveau_statut AS detail, NULL AS date_fin "
        " FROM changement_statut AS c JOIN equipement AS e ON e.id = c.equipement_id "
        " UNION ALL "
        " SELECT 'ERREUR', r.horodatage, e.id, e.nom, e.adresse, r.type_erreur || COALESCE(' : ' || r.message, ''), NULL "
        " FROM erreur_collecte AS r JOIN equipement AS e ON e.id = r.equipement_id "
        " UNION ALL "
        " SELECT a.criticite, a.date_debut, e.id, e.nom, e.adresse, a.message, a.date_fin "
        " FROM alerte AS a JOIN equipement AS e ON e.id = a.equipement_id"
        f") WHERE {' AND '.join(conditions)}"
    )
    return requete, parametres


def compter_logs(equipement_id: int | None = None, depuis: str | None = None, jusqu_a: str | None = None,
                 categorie: str | None = None, recherche: str | None = None) -> int:
    """Nombre de logs correspondant aux filtres (pour la pagination)."""
    requete, parametres = _requete_logs(equipement_id, depuis, jusqu_a, categorie, recherche)
    with get_connection() as connexion:
        return connexion.execute(f"SELECT count(*) FROM ({requete})", parametres).fetchone()[0]


def lister_logs(equipement_id: int | None = None, depuis: str | None = None, jusqu_a: str | None = None,
                categorie: str | None = None, recherche: str | None = None, limite: int = 50,
                decalage: int = 0) -> list[sqlite3.Row]:
    """Logs du plus récent au plus ancien, filtrés et paginés."""
    requete, parametres = _requete_logs(equipement_id, depuis, jusqu_a, categorie, recherche)
    with get_connection() as connexion:
        return connexion.execute(f"{requete} ORDER BY horodatage DESC LIMIT ? OFFSET ?",
                                 (*parametres, limite, decalage)).fetchall()
