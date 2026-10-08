"""Accès à la base SQLite : connexion, création du schéma, fonctions d'accès aux données.

Pas d'ORM : le SQL est écrit à la main. Chaque fonction ouvre sa propre
connexion et la referme ; pour le volume de ce projet, c'est plus simple à suivre qu'un pool
de connexions partagées.
"""
import sqlite3
from pathlib import Path

from app import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    """Ouvre une connexion à la base, avec les clés étrangères activées."""
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    connexion = sqlite3.connect(config.DB_PATH)
    connexion.execute("PRAGMA foreign_keys = ON")
    connexion.row_factory = sqlite3.Row
    return connexion


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
        "'Basé sur la MIB-II (groupe system) ; cpu/ram dépendent du constructeur et viendront "
        "avec des modèles dédiés')"
    )
    uptime_id = connexion.execute(
        "SELECT id FROM metrique WHERE code = 'uptime'"
    ).fetchone()["id"]
    connexion.execute(
        "INSERT OR IGNORE INTO element_surveille (modele_supervision_id, metrique_id, oid) "
        "VALUES (1, ?, '1.3.6.1.2.1.1.3.0')",
        (uptime_id,),
    )


# ---------------------------------------------------------------- équipements

def lister_equipements() -> list[sqlite3.Row]:
    """Renvoie tous les équipements, triés par nom."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM equipement ORDER BY nom").fetchall()


def creer_equipement(nom: str, adresse: str, communaute_snmp: str,
                      description: str | None = None,
                      description_systeme: str | None = None,
                      modele_supervision_id: int = 1) -> int:
    """Insère un équipement (statut initial EN_ATTENTE) et renvoie son id."""
    with get_connection() as connexion:
        curseur = connexion.execute(
            "INSERT INTO equipement "
            "(nom, adresse, communaute_snmp, description, description_systeme, "
            " modele_supervision_id) VALUES (?, ?, ?, ?, ?, ?)",
            (nom, adresse, communaute_snmp, description, description_systeme,
             modele_supervision_id),
        )
        return curseur.lastrowid
