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

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


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
        connexion.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _seed(connexion)


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

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(1, 'Équipement générique', "
        "'Basé sur la MIB-II (groupe system et ifTable) et HOST-RESOURCES-MIB pour cpu/ram ; "
        "cpu/ram ne fonctionnent que sur les agents qui implémentent cette dernière')"
    )
    elements_modele_generique = [
        ("uptime", "1.3.6.1.2.1.1.3.0"),          # sysUpTime, scalaire
        ("if_in_octets", "1.3.6.1.2.1.2.2.1.10"),  # ifInOctets, ifTable (une valeur par interface)
        ("if_out_octets", "1.3.6.1.2.1.2.2.1.16"), # ifOutOctets, ifTable (une valeur par interface)
        # HOST-RESOURCES-MIB : marche sur un agent générique (Linux...), pas forcément sur
        # un équipement réseau avec un OS propriétaire -> à surveiller côté erreur_collecte.
        ("cpu", "1.3.6.1.2.1.25.3.3.1.2"),  # hrProcessorLoad, table à moyenner (plusieurs cœurs)
        ("ram", "1.3.6.1.2.1.25.2.3.1.6"),  # hrStorageUsed, à combiner avec hrStorageSize
                                             # et filtrer sur hrStorageType = RAM (côté collecte)
    ]
    for code, oid in elements_modele_generique:
        metrique_id = connexion.execute(
            "SELECT id FROM metrique WHERE code = ?", (code,)
        ).fetchone()["id"]
        connexion.execute(
            "INSERT OR IGNORE INTO element_surveille (modele_supervision_id, metrique_id, oid) "
            "VALUES (1, ?, ?)",
            (metrique_id, oid),
        )


# ---------------------------------------------------------------- équipements

def lister_equipements() -> list[sqlite3.Row]:
    """Renvoie tous les équipements (avec le nom de leur groupe), triés par groupe (alphabétique,
    les équipements sans groupe en dernier) puis par nom."""
    with get_connection() as connexion:
        return connexion.execute(
            # "~" ne sert qu'au tri : il vient après toutes les lettres, donc les équipements
            # sans groupe se retrouvent toujours en dernier plutôt que mélangés par ordre alphabétique.
            "SELECT equipement.*, COALESCE(groupe.nom, '~') AS groupe_nom "
            "FROM equipement LEFT JOIN groupe ON groupe.id = equipement.groupe_id "
            "ORDER BY groupe_nom, equipement.nom"
        ).fetchall()


def creer_equipement(nom: str, adresse: str, communaute_snmp: str,
                      description: str | None = None,
                      description_systeme: str | None = None,
                      modele_supervision_id: int = 1,
                      groupe_id: int | None = None) -> int:
    """Insère un équipement (statut initial EN_ATTENTE) et renvoie son id."""
    with get_connection() as connexion:
        curseur = connexion.execute(
            "INSERT INTO equipement "
            "(nom, adresse, communaute_snmp, description, description_systeme, "
            " modele_supervision_id, groupe_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nom, adresse, communaute_snmp, description, description_systeme,
             modele_supervision_id, groupe_id),
        )
        return curseur.lastrowid


def supprimer_equipement(id_equipement: int) -> None:
    """Supprime un équipement et tout son historique (clés étrangères en cascade)."""
    with get_connection() as connexion:
        connexion.execute("DELETE FROM equipement WHERE id = ?", (id_equipement,))


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
                         description_systeme: str | None) -> None:
    """Met à jour les informations d'un équipement existant."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET nom = ?, adresse = ?, communaute_snmp = ?, description = ?, "
            "groupe_id = ?, description_systeme = ? WHERE id = ?",
            (nom, adresse, communaute_snmp, description, groupe_id, description_systeme,
             id_equipement),
        )


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
    """Crée l'interface si elle est nouvelle, sinon met à jour son nom/vitesse/statut."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO interface (equipement_id, index_snmp, nom, vitesse, statut) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (equipement_id, index_snmp) DO UPDATE SET "
            "nom = excluded.nom, vitesse = excluded.vitesse, statut = excluded.statut",
            (equipement_id, index_snmp, nom, vitesse, statut),
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


def enregistrer_reussite_collecte(equipement_id: int) -> None:
    """Après une collecte réussie (l'équipement a répondu) : échecs à zéro, horodatage, et
    passage à OPERATIONNEL si l'équipement ne l'était pas déjà."""
    with get_connection() as connexion:
        ancien_statut = connexion.execute(
            "SELECT statut FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()["statut"]
        connexion.execute(
            "UPDATE equipement SET nb_echecs_consecutifs = 0, "
            "derniere_collecte = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
            (equipement_id,),
        )
        if ancien_statut != "OPERATIONNEL":
            _changer_statut(connexion, equipement_id, ancien_statut, "OPERATIONNEL")


def enregistrer_echec_collecte(equipement_id: int, nb_echecs_avant_indisponibilite: int) -> None:
    """Après un échec (équipement injoignable) : incrémente les échecs consécutifs, et bascule
    en INDISPONIBLE si le seuil est atteint (jamais sur un seul échec isolé)."""
    with get_connection() as connexion:
        ligne = connexion.execute(
            "SELECT statut, nb_echecs_consecutifs FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()
        nouveaux_echecs = ligne["nb_echecs_consecutifs"] + 1
        connexion.execute(
            "UPDATE equipement SET nb_echecs_consecutifs = ? WHERE id = ?",
            (nouveaux_echecs, equipement_id),
        )
        if nouveaux_echecs >= nb_echecs_avant_indisponibilite and ligne["statut"] != "INDISPONIBLE":
            _changer_statut(connexion, equipement_id, ligne["statut"], "INDISPONIBLE")
