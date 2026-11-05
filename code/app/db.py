"""Accès à la base SQLite : connexion, création du schéma, fonctions d'accès aux données.

Pas d'ORM : le SQL est écrit à la main. Chaque fonction ouvre sa propre
connexion et la referme ; pour le volume de ce projet, c'est plus simple à suivre qu'un pool
de connexions partagées.
"""
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app import config

# Structure de la base, créée au démarrage si elle n'existe pas encore : ne modifie jamais une base
# existante. Une colonne ajoutée à une table existante doit aussi l'être dans _migrer.
# Noms en snake_case, d'après le modèle de classes UML validé.
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
    description TEXT,
    prefixe_sys_object_id TEXT              -- détection automatique du modèle à l'ajout
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
    sys_object_id           TEXT,                   -- identifie le constructeur et le modèle
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

-- Seuils de performance, globaux à tout le parc (ex. cpu > 90 %).
CREATE TABLE IF NOT EXISTS seuils_config (
    metrique_id INTEGER PRIMARY KEY REFERENCES metrique(id) ON DELETE CASCADE,
    valeur      REAL NOT NULL CHECK (valeur BETWEEN 0 AND 100),
    actif       INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1))
);

-- Alertes : EQUIPEMENT = ne répond plus au ping ; SERVICE = joignable, mais relevé SNMP en erreur
-- ou seuil dépassé. cause_racine_id relie une alerte à celle de l'équipement parent en panne.
CREATE TABLE IF NOT EXISTS alerte (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipement_id   INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,
    categorie       TEXT NOT NULL CHECK (categorie IN ('EQUIPEMENT', 'SERVICE')),
    code            TEXT NOT NULL
                        CHECK (code IN ('INJOIGNABLE', 'SNMP_EN_ECHEC', 'RELEVE_EN_ECHEC', 'SEUIL_DEPASSE')),
    metrique_id     INTEGER REFERENCES metrique(id) ON DELETE CASCADE,
    message         TEXT NOT NULL,
    statut          TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (statut IN ('ACTIVE', 'CLOTUREE')),
    date_debut      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    date_fin        TEXT,
    cause_racine_id INTEGER REFERENCES alerte(id) ON DELETE SET NULL
);
-- Une seule alerte active par équipement, type d'alerte et métrique.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerte_active
    ON alerte (equipement_id, code, COALESCE(metrique_id, 0)) WHERE statut = 'ACTIVE';
"""


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
    """Crée les tables si besoin et pré-remplit le catalogue de base (appelé au démarrage)."""
    with get_connection() as connexion:
        connexion.executescript(SCHEMA)
        _migrer(connexion)
        _seed(connexion)


def _migrer(connexion: sqlite3.Connection) -> None:
    """Ajoute aux bases existantes les colonnes que CREATE TABLE IF NOT EXISTS ne crée pas."""
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
    for table, colonne in (("equipement", "sys_object_id"), ("modele_supervision", "prefixe_sys_object_id")):
        if colonne not in {ligne["name"] for ligne in connexion.execute(f"PRAGMA table_info({table})")}:
            connexion.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} TEXT")
    if "visible" in colonnes:  # ancienne colonne de masquage : sa décision est reprise une fois
        connexion.execute("UPDATE interface SET suivi = 'NON', visible = 1 WHERE visible = 0")


def _seed(connexion: sqlite3.Connection) -> None:
    """Insère les données de référence si elles n'existent pas encore (idempotent)."""
    connexion.execute("INSERT OR IGNORE INTO parametres_generaux (id) VALUES (1)")

    metriques = [
        ("uptime", "Disponibilité (uptime)", None, "COMPTEUR", 0),
        ("cpu", "Charge CPU", "%", "JAUGE", 0),
        ("ram", "Mémoire utilisée", "%", "JAUGE", 0),
        ("if_in_octets", "Trafic entrant", "octets", "COMPTEUR", 1),
        ("if_out_octets", "Trafic sortant", "octets", "COMPTEUR", 1),
    ]
    connexion.executemany(
        "INSERT OR IGNORE INTO metrique (code, libelle, unite, type_valeur, par_interface) "
        "VALUES (?, ?, ?, ?, ?)",
        metriques,
    )

    elements_communs = [
        ("uptime", "1.3.6.1.2.1.1.3.0"),           # sysUpTime, scalaire
        ("if_in_octets", "1.3.6.1.2.1.2.2.1.10"),  # ifInOctets, ifTable (une valeur par interface)
        ("if_out_octets", "1.3.6.1.2.1.2.2.1.16"), # ifOutOctets, ifTable (une valeur par interface)
    ]

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(1, 'Équipement générique', "
        "'Basé sur la MIB-II (groupe system et ifTable) et HOST-RESOURCES-MIB pour cpu/ram ; "
        "cpu/ram ne fonctionnent que sur les agents qui implémentent cette dernière')"
    )
    _inserer_elements_surveilles(connexion, 1, elements_communs + [
        # HOST-RESOURCES-MIB : marche sur un agent générique (Linux...), pas forcément sur
        # un équipement réseau avec un OS propriétaire -> à surveiller côté erreur_collecte.
        ("cpu", "1.3.6.1.2.1.25.3.3.1.2"),  # hrProcessorLoad, table à moyenner (plusieurs cœurs)
        ("ram", "1.3.6.1.2.1.25.2.3.1.6"),  # hrStorageUsed, à combiner avec hrStorageSize
                                             # et filtrer sur hrStorageType = RAM (côté collecte)
    ])

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(2, 'Switch Cisco', "
        "'Basé sur la MIB-II (groupe system et ifTable), et sur CISCO-PROCESS-MIB / "
        "CISCO-MEMORY-POOL-MIB pour cpu/ram (IOS ne supporte pas HOST-RESOURCES-MIB)')"
    )
    _inserer_elements_surveilles(connexion, 2, elements_communs + [
        ("cpu", "1.3.6.1.4.1.9.9.109.1.1.1.1.8"),  # cpmCPUTotal5minRev, déjà en %
        ("ram", "1.3.6.1.4.1.9.9.48.1.1.1.5"),     # ciscoMemoryPoolUsed, à combiner avec
                                                     # ciscoMemoryPoolFree (côté collecte)
    ])

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(3, 'Serveur ou NAS Linux', "
        "'Basé sur la MIB-II (groupe system et ifTable), et sur UCD-SNMP-MIB pour cpu/ram "
        "(agent Net-SNMP)')"
    )
    _inserer_elements_surveilles(connexion, 3, elements_communs + [
        ("cpu", "1.3.6.1.4.1.2021.11.11.0"),  # ssCpuIdle, à soustraire de 100 (côté collecte)
        ("ram", "1.3.6.1.4.1.2021.4.5.0"),    # memTotalReal, à combiner avec memAvailReal (côté collecte)
    ])

    # Préfixes de sysObjectID (identifiant d'entreprise) qui désignent automatiquement un modèle
    connexion.executemany(
        "UPDATE modele_supervision SET prefixe_sys_object_id = ? WHERE id = ?",
        [("1.3.6.1.4.1.9", 2), ("1.3.6.1.4.1.8072", 3)],
    )
    for code in ("cpu", "ram"):
        connexion.execute(
            "INSERT OR IGNORE INTO seuils_config (metrique_id, valeur) "
            "SELECT id, 90 FROM metrique WHERE code = ?",
            (code,),
        )


def _inserer_elements_surveilles(connexion: sqlite3.Connection, modele_supervision_id: int,
                                  elements: list[tuple[str, str]]) -> None:
    """Associe une liste de (code métrique, oid) à un modèle de supervision."""
    for code, oid in elements:
        metrique_id = connexion.execute(
            "SELECT id FROM metrique WHERE code = ?", (code,)
        ).fetchone()["id"]
        connexion.execute(
            "INSERT OR IGNORE INTO element_surveille (modele_supervision_id, metrique_id, oid) "
            "VALUES (?, ?, ?)",
            (modele_supervision_id, metrique_id, oid),
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


def modifier_intervalle_polling(id_equipement: int, intervalle_polling: int | None) -> None:
    """Change l'intervalle de polling d'un équipement (None = valeur par défaut)."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET intervalle_polling = ? WHERE id = ?",
            (intervalle_polling, id_equipement),
        )


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
            "ORDER BY horodatage DESC LIMIT ?",
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
                    depuis: str | None = None, jusqu_a: str | None = None) -> list[sqlite3.Row]:
    """Mesures d'un équipement pour une métrique donnée (et, si fournie, une interface
    donnée), triées par horodatage croissant."""
    condition = "mesure.equipement_id = ? AND metrique.code = ?"
    parametres: list = [equipement_id, code_metrique]
    if interface_id is not None:
        condition += " AND mesure.interface_id = ?"
        parametres.append(interface_id)
    if depuis:
        condition += " AND mesure.horodatage >= ?"
        parametres.append(depuis)
    if jusqu_a:
        condition += " AND mesure.horodatage <= ?"
        parametres.append(jusqu_a)
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT mesure.horodatage, mesure.valeur, mesure.interface_id "
            "FROM mesure JOIN metrique ON metrique.id = mesure.metrique_id "
            f"WHERE {condition} ORDER BY mesure.horodatage",
            parametres,
        ).fetchall()


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


def trouver_modele_pour(sys_object_id: str | None) -> int:
    """Modèle dont le préfixe correspond le mieux au sysObjectID, sinon le modèle générique."""
    meilleur_id, meilleure_longueur = 1, 0
    for modele in lister_modeles():
        prefixe = modele["prefixe_sys_object_id"]
        if prefixe and sys_object_id and (sys_object_id + ".").startswith(prefixe + ".") \
                and len(prefixe) > meilleure_longueur:
            meilleur_id, meilleure_longueur = modele["id"], len(prefixe)
    return meilleur_id


# ---------------------------------------------------------------- groupes

def lister_groupes() -> list[sqlite3.Row]:
    """Renvoie tous les groupes, triés par nom."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM groupe ORDER BY nom").fetchall()


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
            "SELECT element_surveille.oid, metrique.id AS metrique_id, metrique.code, "
            "metrique.type_valeur, metrique.par_interface "
            "FROM element_surveille JOIN metrique ON metrique.id = element_surveille.metrique_id "
            "WHERE element_surveille.modele_supervision_id = ? AND element_surveille.actif = 1",
            (modele_supervision_id,),
        ).fetchall()


def enregistrer_mesure(equipement_id: int, metrique_id: int, valeur: float | None,
                        interface_id: int | None = None) -> None:
    """Enregistre un relevé. valeur = None si l'équipement n'a pas répondu pour cet indicateur."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO mesure (equipement_id, metrique_id, interface_id, horodatage, valeur) "
            "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now'), ?)",
            (equipement_id, metrique_id, interface_id, valeur),
        )


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

def lister_seuils() -> list[sqlite3.Row]:
    """Renvoie les seuils configurés, avec le code et le libellé de leur métrique."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT seuils_config.*, metrique.code, metrique.libelle "
            "FROM seuils_config JOIN metrique ON metrique.id = seuils_config.metrique_id "
            "ORDER BY metrique.libelle"
        ).fetchall()


def modifier_seuil(metrique_id: int, valeur: float, actif: bool) -> bool:
    """Met à jour un seuil ; renvoie False s'il n'existe pas."""
    with get_connection() as connexion:
        curseur = connexion.execute(
            "UPDATE seuils_config SET valeur = ?, actif = ? WHERE metrique_id = ?",
            (valeur, int(actif), metrique_id),
        )
        return curseur.rowcount > 0


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
                   metrique_id: int | None = None) -> None:
    """Ouvre une alerte, ou actualise son message si elle est déjà active."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT OR IGNORE INTO alerte (equipement_id, categorie, code, metrique_id, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (equipement_id, categorie, code, metrique_id, message),
        )
        connexion.execute(
            "UPDATE alerte SET message = ? WHERE equipement_id = ? AND code = ? "
            "AND COALESCE(metrique_id, 0) = ? AND statut = 'ACTIVE'",
            (message, equipement_id, code, metrique_id or 0),
        )
        _recalculer_correlation(connexion)


def _cloturer(connexion: sqlite3.Connection, condition: str, parametres: tuple) -> None:
    connexion.execute(
        "UPDATE alerte SET statut = 'CLOTUREE', date_fin = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
        f"WHERE statut = 'ACTIVE' AND {condition}",
        parametres,
    )
    _recalculer_correlation(connexion)


def cloturer_alerte(equipement_id: int, code: str, metrique_id: int | None = None) -> None:
    """Clôture une alerte active précise, si elle existe."""
    with get_connection() as connexion:
        _cloturer(connexion, "equipement_id = ? AND code = ? AND COALESCE(metrique_id, 0) = ?",
                  (equipement_id, code, metrique_id or 0))


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
    """Alertes les plus récentes d'abord, avec l'équipement concerné, le nom de leur cause
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
            f"WHERE {' AND '.join(conditions)} ORDER BY alerte.date_debut DESC, alerte.id DESC LIMIT ?",
            (*parametres, limite),
        ).fetchall()
