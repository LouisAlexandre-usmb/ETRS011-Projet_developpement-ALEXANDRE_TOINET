"""Accès à la base SQLite : connexion, création du schéma, fonctions d'accès aux données.

Pas d'ORM : le SQL est écrit à la main. Chaque fonction ouvre sa propre
connexion et la referme ; pour le volume de ce projet, c'est plus simple à suivre qu'un pool
de connexions partagées.
"""
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from itertools import groupby
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
        _migrer(connexion)
        _seed(connexion)


def _migrer(connexion: sqlite3.Connection) -> None:
    """Met une base existante au niveau de schema.sql (pas d'outil de migration dans ce
    projet). Règle unique : toute table dont les colonnes diffèrent de celles décrites dans
    schema.sql est reconstruite ; les colonnes ajoutées prennent leur valeur par défaut, les
    colonnes retirées sont abandonnées. Les colonnes attendues sont lues en créant le schéma
    dans une base temporaire en mémoire : schema.sql reste la seule description des tables."""
    modele = sqlite3.connect(":memory:")
    modele.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    tables = [ligne[0] for ligne in modele.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    a_reconstruire = [table for table in tables
                      if set(_colonnes(connexion, table)) != set(_colonnes(modele, table))]
    modele.close()

    # Base d'avant l'agrégation des mesures : 30 j de détail / 365 j agrégés (inutilisé)
    # deviennent 15 j de détail / 30 j au total.
    avant_agregation = "duree_conservation_evenements" not in _colonnes(connexion, "parametres_generaux")

    if a_reconstruire:
        _reconstruire_tables(connexion, a_reconstruire)
    if avant_agregation:
        connexion.execute("UPDATE parametres_generaux "
                          "SET duree_conservation_detail = 15, duree_conservation_agregee = 30")


def _reconstruire_tables(connexion: sqlite3.Connection, tables: list[str]) -> None:
    """Aligne des tables sur schema.sql en les recréant, car ALTER TABLE ne sait ni retirer une
    colonne qui porte une clé étrangère ou un CHECK, ni modifier une contrainte. Procédure
    recommandée par la documentation SQLite : les clés étrangères sont désactivées le temps de
    l'opération (sinon supprimer l'ancienne table « equipement » supprimerait en cascade
    mesures, interfaces, logs...) et legacy_alter_table est activé (sinon renommer
    « equipement » redirigerait les références des autres tables vers l'ancienne table). On
    renomme les anciennes tables, schema.sql recrée les nouvelles, on recopie les colonnes
    communes, puis on vérifie l'intégrité avant de réactiver les clés étrangères."""
    connexion.commit()
    connexion.execute("PRAGMA foreign_keys = OFF")
    connexion.execute("PRAGMA legacy_alter_table = ON")
    try:
        for table in tables:
            connexion.execute(f"ALTER TABLE {table} RENAME TO {table}_ancienne")
        connexion.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table in tables:
            nouvelles = _colonnes(connexion, table)
            communes = ", ".join(c for c in _colonnes(connexion, f"{table}_ancienne") if c in nouvelles)
            connexion.execute(f"INSERT INTO {table} ({communes}) SELECT {communes} FROM {table}_ancienne")
            connexion.execute(f"DROP TABLE {table}_ancienne")
        connexion.commit()
        incoherences = connexion.execute("PRAGMA foreign_key_check").fetchall()
        if incoherences:
            raise RuntimeError(f"Migration : clés étrangères incohérentes : {incoherences[:5]}")
    finally:
        connexion.execute("PRAGMA legacy_alter_table = OFF")
        connexion.execute("PRAGMA foreign_keys = ON")


TABLES_LOGS = ("changement_statut", "erreur_collecte", "alerte")


def _colonnes(connexion: sqlite3.Connection, table: str) -> list[str]:
    return [ligne[1] for ligne in connexion.execute(f"PRAGMA table_info({table})").fetchall()]


def _seed(connexion: sqlite3.Connection) -> None:
    """Insère les données de référence si elles n'existent pas encore (idempotent)."""
    connexion.execute("INSERT OR IGNORE INTO parametres_generaux (id) VALUES (1)")

    metriques = [
        ("uptime", "Disponibilité (uptime)", None, "COMPTEUR", 0),
        ("cpu", "Charge CPU", "%", "JAUGE", 0),
        ("ram", "Mémoire utilisée", "%", "JAUGE", 0),
        ("if_in_octets", "Trafic entrant", "o/s", "COMPTEUR", 1),
        ("if_out_octets", "Trafic sortant", "o/s", "COMPTEUR", 1),
        ("volume", "Stockage", "%", "JAUGE", 0),
        # Pas d'OID associé (pas d'element_surveille pour cette métrique) : calculée à la
        # collecte à partir du débit déjà mesuré (if_in_octets/if_out_octets) et de la vitesse
        # de l'interface, cf. app/collecte.py::_calculer_utilisation_interfaces.
        ("utilisation_interface", "Utilisation d'interface", "%", "JAUGE", 1),
    ]
    connexion.executemany(
        "INSERT OR IGNORE INTO metrique (code, libelle, unite, type_valeur, par_interface) "
        "VALUES (?, ?, ?, ?, ?)",
        metriques,
    )

    # Les modèles par défaut ne sont créés que sur une base vierge : ensuite, ils sont
    # modifiables depuis l'interface, et les recréer à chaque démarrage réintroduirait les
    # métriques que l'utilisateur en a retirées.
    if connexion.execute("SELECT COUNT(*) FROM modele_supervision").fetchone()[0] > 0:
        return

    elements_communs = [
        ("uptime", "1.3.6.1.2.1.1.3.0"),           # sysUpTime, scalaire
        ("if_in_octets", "1.3.6.1.2.1.2.2.1.10"),  # ifInOctets, ifTable (une valeur par interface)
        ("if_out_octets", "1.3.6.1.2.1.2.2.1.16"), # ifOutOctets, ifTable (une valeur par interface)
    ]

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(1, 'Équipement générique', "
        "'Supervise la disponibilité, le trafic entrant/sortant par interface, le CPU, la "
        "mémoire et le stockage (via HOST-RESOURCES-MIB). Convient à un équipement "
        "avec un OS généraliste (serveur Linux, NAS...) ; ces indicateurs ne remonteront rien "
        "sur un équipement réseau à OS propriétaire (switch, routeur constructeur)')"
    )
    _inserer_elements_surveilles(connexion, 1, elements_communs + [
        # HOST-RESOURCES-MIB : marche sur un agent générique (Linux...), pas forcément sur
        # un équipement réseau avec un OS propriétaire -> à surveiller côté erreur_collecte.
        ("cpu", "1.3.6.1.2.1.25.3.3.1.2"),  # hrProcessorLoad, table à moyenner (plusieurs cœurs)
        ("ram", "1.3.6.1.2.1.25.2.3.1.6"),  # hrStorageUsed, à combiner avec hrStorageSize
                                             # et filtrer sur hrStorageType = RAM (côté collecte)
        ("volume", "1.3.6.1.2.1.25.2.3.1.6"),  # hrStorageUsed aussi, mais filtré sur le type
                                                 # "disque fixe" ; un ou plusieurs volumes possibles
    ])

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(2, 'Switch Cisco', "
        "'Supervise la disponibilité, le trafic entrant/sortant par interface, le CPU et la "
        "mémoire — spécifique aux équipements Cisco IOS (via CISCO-PROCESS-MIB et "
        "CISCO-MEMORY-POOL-MIB). À réserver aux équipements de cette marque')"
    )
    _inserer_elements_surveilles(connexion, 2, elements_communs + [
        ("cpu", "1.3.6.1.4.1.9.9.109.1.1.1.1.8"),  # cpmCPUTotal5minRev, déjà en %
        ("ram", "1.3.6.1.4.1.9.9.48.1.1.1.5"),     # ciscoMemoryPoolUsed, à combiner avec
                                                     # ciscoMemoryPoolFree (côté collecte)
    ])

    connexion.execute(
        "INSERT OR IGNORE INTO modele_supervision (id, nom, description) VALUES "
        "(3, 'Équipement réseau basique', "
        "'Supervise uniquement la disponibilité et le trafic entrant/sortant par interface "
        "(pas de CPU ni de RAM). Le choix le plus sûr pour un équipement réseau dont on ne "
        "connaît pas les MIB constructeur, ou qui ne remonte aucune donnée de CPU/RAM en SNMP')"
    )
    _inserer_elements_surveilles(connexion, 3, elements_communs)


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


def modifier_intervalle_polling(id_equipement: int, intervalle_polling: int | None) -> None:
    """Change l'intervalle de polling d'un équipement (None = valeur par défaut)."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET intervalle_polling = ? WHERE id = ?",
            (intervalle_polling, id_equipement),
        )


def modifier_duree_conservation(id_equipement: int, duree_conservation: int | None) -> None:
    """Change la durée de conservation des mesures d'un équipement, en jours
    (None = valeur par défaut). Le maximum (30 j) est vérifié côté route, pas ici."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET duree_conservation = ? WHERE id = ?",
            (duree_conservation, id_equipement),
        )


# ---------------------------------------------------------------- conservation de l'historique

TRANCHE_AGREGATION = 30 * 60  # secondes : 48 mesures agrégées par jour au maximum
_EPOQUE = datetime(1970, 1, 1)


def _en_secondes(instant: datetime) -> float:
    return (instant - _EPOQUE).total_seconds()


def _en_horodatage(secondes: float) -> str:
    """Même format que les horodatages stockés par SQLite (UTC, « 2026-11-14T10:30:00 ») :
    toutes les comparaisons de dates se font entre chaînes de ce format."""
    return (_EPOQUE + timedelta(seconds=secondes)).strftime("%Y-%m-%dT%H:%M:%S")


def _il_y_a(jours: int) -> str:
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    return _en_horodatage(_en_secondes(maintenant - timedelta(days=jours)))


def _debit_entre(precedente, mesure) -> float | None:
    """Débit (par seconde) entre deux relevés bruts consécutifs d'un compteur. None si l'un
    des deux manque (équipement injoignable) ou si le compteur est revenu en arrière (remise
    à zéro ou dépassement de capacité) : un trou plutôt qu'un débit négatif."""
    if precedente is None or mesure["valeur"] is None or precedente["valeur"] is None:
        return None
    delta_valeur = mesure["valeur"] - precedente["valeur"]
    delta_temps = (datetime.fromisoformat(mesure["horodatage"])
                   - datetime.fromisoformat(precedente["horodatage"])).total_seconds()
    return delta_valeur / delta_temps if delta_valeur >= 0 and delta_temps > 0 else None


def _agreger_mesures(connexion: sqlite3.Connection, equipement_id: int, avant: str) -> None:
    """Remplace les mesures brutes antérieures à « avant » par leur résumé par tranche de
    30 minutes (moyenne, minimum, maximum, nombre d'échantillons). Pour un compteur de
    trafic par interface, on résume les débits calculés entre relevés successifs (c'est
    ce qu'affichent les graphiques), pas le compteur brut, qui ne fait que croître."""
    lignes = connexion.execute(
        "SELECT mesure.metrique_id, mesure.interface_id, mesure.horodatage, mesure.valeur, "
        "metrique.type_valeur, metrique.par_interface "
        "FROM mesure JOIN metrique ON metrique.id = mesure.metrique_id "
        "WHERE mesure.equipement_id = ? AND mesure.horodatage < ? "
        "ORDER BY mesure.metrique_id, mesure.interface_id, mesure.horodatage",
        (equipement_id, avant),
    ).fetchall()

    # Une série = une métrique sur une interface (ou sur l'équipement entier).
    for (metrique_id, interface_id), serie in groupby(lignes, key=lambda l: (l["metrique_id"], l["interface_id"])):
        serie = list(serie)
        est_debit = serie[0]["type_valeur"] == "COMPTEUR" and serie[0]["par_interface"]
        tranches: dict[int, list] = {}
        precedente = None
        for ligne in serie:
            valeur = ligne["valeur"]
            if est_debit:
                valeur, precedente = _debit_entre(precedente, ligne), ligne
            debut = int(_en_secondes(datetime.fromisoformat(ligne["horodatage"])) // TRANCHE_AGREGATION)
            tranches.setdefault(debut * TRANCHE_AGREGATION, []).append(valeur)

        for debut, valeurs in tranches.items():
            connues = [v for v in valeurs if v is not None]
            connexion.execute(
                "INSERT INTO mesure_agregee (equipement_id, metrique_id, interface_id, horodatage, "
                "moyenne, minimum, maximum, nb_echantillons) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (equipement_id, metrique_id, interface_id, _en_horodatage(debut),
                 sum(connues) / len(connues) if connues else None,
                 min(connues, default=None), max(connues, default=None), len(connues)),
            )
    connexion.execute("DELETE FROM mesure WHERE equipement_id = ? AND horodatage < ?", (equipement_id, avant))


def entretenir_historique() -> None:
    """Politique de conservation, appliquée au démarrage puis une fois par jour.

    Mesures, pour chaque équipement (durée : la sienne, sinon duree_conservation_agregee) :
    toutes les mesures brutes pendant duree_conservation_detail jours (15) ; au-delà, si
    l'équipement conserve plus longtemps, elles sont agrégées par demi-heure ; tout ce qui
    dépasse sa durée de conservation est purgé. Logs : changements de statut et alertes
    clôturées gardés duree_conservation_evenements jours (365), erreurs de collecte
    duree_conservation_erreurs jours (30). Une alerte encore active n'est jamais purgée."""
    parametres = obtenir_parametres_generaux()
    detail = parametres["duree_conservation_detail"]
    # Limite alignée sur un début de tranche : une demi-heure n'est jamais coupée en deux
    # entre deux passages (sinon elle serait agrégée deux fois, en deux morceaux).
    limite_detail = _il_y_a(detail)
    limite_detail = _en_horodatage(
        _en_secondes(datetime.fromisoformat(limite_detail)) // TRANCHE_AGREGATION * TRANCHE_AGREGATION)

    with get_connection() as connexion:
        for equipement in connexion.execute("SELECT id, duree_conservation FROM equipement").fetchall():
            duree = equipement["duree_conservation"] or parametres["duree_conservation_agregee"]
            if duree > detail:
                _agreger_mesures(connexion, equipement["id"], limite_detail)
            limite_purge = _il_y_a(duree)
            connexion.execute("DELETE FROM mesure WHERE equipement_id = ? AND horodatage < ?",
                              (equipement["id"], limite_purge))
            connexion.execute("DELETE FROM mesure_agregee WHERE equipement_id = ? AND horodatage < ?",
                              (equipement["id"], limite_purge))

        limite_evenements = _il_y_a(parametres["duree_conservation_evenements"])
        connexion.execute("DELETE FROM changement_statut WHERE horodatage < ?", (limite_evenements,))
        connexion.execute("DELETE FROM alerte WHERE etat != 'ACTIVE' "
                          "AND COALESCE(date_cloture, date_declenchement) < ?", (limite_evenements,))
        connexion.execute("DELETE FROM erreur_collecte WHERE horodatage < ?",
                          (_il_y_a(parametres["duree_conservation_erreurs"]),))


def changer_modele_supervision(id_equipement: int, modele_supervision_id: int) -> None:
    """Change le modèle de supervision d'un équipement existant."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE equipement SET modele_supervision_id = ? WHERE id = ?",
            (modele_supervision_id, id_equipement),
        )


def supprimer_equipement(id_equipement: int) -> None:
    """Supprime un équipement et tout ce qui le concerne, immédiatement : ses logs
    (effacés explicitement, leur clé étrangère étant en SET NULL pour permettre
    l'archivage), puis l'équipement lui-même, ce qui supprime en cascade ses mesures,
    mesures agrégées, interfaces et volumes."""
    with get_connection() as connexion:
        for table in TABLES_LOGS:
            connexion.execute(f"DELETE FROM {table} WHERE equipement_id = ?", (id_equipement,))
        connexion.execute("DELETE FROM equipement WHERE id = ?", (id_equipement,))


def archiver_equipement(id_equipement: int) -> None:
    """Retire un équipement en gardant ses logs (changements de statut, alertes, erreurs
    de collecte), qui suivent ensuite la même politique de conservation que les autres.
    Ses alertes actives sont clôturées (plus rien ne les réévaluerait), son nom est
    recopié dans ses logs pour rester affichable, puis il est supprimé : ses mesures,
    interfaces et volumes disparaissent en cascade, ses logs passent à equipement_id NULL."""
    with get_connection() as connexion:
        ligne = connexion.execute("SELECT nom FROM equipement WHERE id = ?", (id_equipement,)).fetchone()
        if ligne is None:
            return
        connexion.execute(
            "UPDATE alerte SET etat = 'CLOTUREE', date_cloture = strftime('%Y-%m-%dT%H:%M:%S', 'now') "
            "WHERE equipement_id = ? AND etat = 'ACTIVE'", (id_equipement,))
        for table in TABLES_LOGS:
            connexion.execute(f"UPDATE {table} SET nom_equipement_archive = ? WHERE equipement_id = ?",
                              (ligne["nom"], id_equipement))
        connexion.execute("DELETE FROM equipement WHERE id = ?", (id_equipement,))


def obtenir_equipement(id_equipement: int) -> sqlite3.Row | None:
    """Renvoie un équipement par son id, ou None s'il n'existe pas."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM equipement WHERE id = ?", (id_equipement,)
        ).fetchone()


def obtenir_equipement_detail(id_equipement: int) -> sqlite3.Row | None:
    """Équipement avec le nom de son groupe et de son modèle, pour la fiche équipement."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT equipement.*, groupe.nom AS groupe_nom, modele_supervision.nom AS modele_nom "
            "FROM equipement "
            "LEFT JOIN groupe ON groupe.id = equipement.groupe_id "
            "JOIN modele_supervision ON modele_supervision.id = equipement.modele_supervision_id "
            "WHERE equipement.id = ?",
            (id_equipement,),
        ).fetchone()


def lister_interfaces(equipement_id: int) -> list[sqlite3.Row]:
    """Interfaces d'un équipement (visibles et masquées), triées par index SNMP."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM interface WHERE equipement_id = ? ORDER BY index_snmp",
            (equipement_id,),
        ).fetchall()


def dernieres_valeurs_par_interface(equipement_id: int, code_metrique: str) -> dict[int, float]:
    """Dernière valeur connue d'une métrique par-interface (ex. taux d'utilisation), pour
    chaque interface d'un équipement. Renvoie {interface_id: valeur}, pour affichage sur la
    fiche équipement (cf. fiche_equipement.html).

    Un seul passage sur les mesures : avec MAX() dans une requête groupée, SQLite renvoie les
    autres colonnes (ici valeur) de la ligne qui porte ce maximum, c'est-à-dire la plus
    récente de chaque interface. Une sous-requête corrélée (une recherche par mesure) devenait
    très lente dès quelques jours d'historique sur un équipement à nombreuses interfaces."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT mesure.interface_id, mesure.valeur, MAX(mesure.id) FROM mesure "
            "JOIN metrique ON metrique.id = mesure.metrique_id "
            "WHERE mesure.equipement_id = ? AND metrique.code = ? AND mesure.interface_id IS NOT NULL "
            "GROUP BY mesure.interface_id",
            (equipement_id, code_metrique),
        ).fetchall()
        return {ligne["interface_id"]: ligne["valeur"] for ligne in lignes}


def obtenir_interface(interface_id: int) -> sqlite3.Row | None:
    """Renvoie une interface par son id (nom, vitesse... pour le calcul du taux
    d'utilisation, cf. app/collecte.py)."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM interface WHERE id = ?", (interface_id,)).fetchone()


def _basculer_visibilite(table: str, id_ligne: int) -> int | None:
    """Masque/réaffiche une ligne d'une table "supervisable" (interface ou volume, même
    forme : equipement_id + visible). Renvoie l'id de l'équipement, ou None si absente."""
    with get_connection() as connexion:
        ligne = connexion.execute(
            f"SELECT equipement_id, visible FROM {table} WHERE id = ?", (id_ligne,)
        ).fetchone()
        if ligne is None:
            return None
        connexion.execute(
            f"UPDATE {table} SET visible = ? WHERE id = ?",
            (0 if ligne["visible"] else 1, id_ligne),
        )
        return ligne["equipement_id"]


def basculer_visibilite_interface(interface_id: int) -> int | None:
    """Masque une interface visible, ou réaffiche une interface masquée. Renvoie l'id de
    son équipement (pour la redirection), ou None si l'interface n'existe pas."""
    return _basculer_visibilite("interface", interface_id)


def lister_volumes(equipement_id: int) -> list[sqlite3.Row]:
    """Volumes d'un équipement (visibles et masqués), triés par index SNMP."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM volume WHERE equipement_id = ? ORDER BY index_snmp",
            (equipement_id,),
        ).fetchall()


def basculer_visibilite_volume(volume_id: int) -> int | None:
    """Masque un volume visible, ou réaffiche un volume masqué. Renvoie l'id de son
    équipement (pour la redirection), ou None si le volume n'existe pas."""
    return _basculer_visibilite("volume", volume_id)


def _filtre_logs(equipement_id: int | None, depuis: str | None, jusqu_a: str | None,
                  colonne_horodatage: str = "horodatage", nom_archive: str | None = None) -> tuple[str, list]:
    """Construit la clause WHERE commune aux tables de logs, et ses paramètres. Le nom de
    la colonne horodatage varie selon la table (alerte utilise date_declenchement). Un
    équipement archivé n'a plus d'id : on le filtre par le nom recopié dans ses logs."""
    condition = "1 = 1"
    parametres: list = []
    if equipement_id is not None:
        condition += " AND equipement_id = ?"
        parametres.append(equipement_id)
    if nom_archive is not None:
        condition += " AND equipement_id IS NULL AND nom_equipement_archive = ?"
        parametres.append(nom_archive)
    if depuis:
        condition += f" AND {colonne_horodatage} >= ?"
        parametres.append(depuis)
    if jusqu_a:
        condition += f" AND {colonne_horodatage} <= ?"
        parametres.append(jusqu_a)
    return condition, parametres


# Les trois types de logs : table, colonne d'horodatage, et texte affiché dans « Détail ».
TYPES_LOGS = {
    "CHANGEMENT_STATUT": ("changement_statut", "horodatage", "(ancien_statut || ' -> ' || nouveau_statut)"),
    "ERREUR_COLLECTE": ("erreur_collecte", "horodatage", "(type_erreur || COALESCE(' : ' || message, ''))"),
    "ALERTE": ("alerte", "date_declenchement", "message"),
}


def _types_retenus(type_log: str | None) -> list[str]:
    """Un type précis si demandé (et connu), sinon les trois."""
    return [type_log] if type_log in TYPES_LOGS else list(TYPES_LOGS)


def compter_logs(equipement_id: int | None = None, depuis: str | None = None,
                  jusqu_a: str | None = None, nom_archive: str | None = None,
                  type_log: str | None = None) -> int:
    """Nombre total de logs correspondant aux filtres (pour la pagination)."""
    comptages, parametres = [], []
    for type_ in _types_retenus(type_log):
        table, colonne_horodatage, _ = TYPES_LOGS[type_]
        condition, parametres_type = _filtre_logs(equipement_id, depuis, jusqu_a, colonne_horodatage, nom_archive)
        comptages.append(f"(SELECT COUNT(*) FROM {table} WHERE {condition})")
        parametres += parametres_type
    with get_connection() as connexion:
        return connexion.execute(f"SELECT {' + '.join(comptages)} AS total", parametres).fetchone()["total"]


def lister_logs(equipement_id: int | None = None, depuis: str | None = None,
                 jusqu_a: str | None = None, limite: int = 50, decalage: int = 0,
                 nom_archive: str | None = None, type_log: str | None = None) -> list[sqlite3.Row]:
    """Historique unifié (changements de statut + erreurs de collecte + alertes), le plus
    récent en premier, filtrable par type de log, par équipement (actif, ou archivé via son
    nom) et par plage de dates, et paginé. Chaque ligne porte un type_log pour les
    distinguer à l'affichage ; equipement_id est NULL pour un équipement archivé (pas de
    fiche à lier). Seules les tables des types retenus entrent dans l'UNION ALL."""
    selections, parametres = [], []
    for type_ in _types_retenus(type_log):
        table, colonne_horodatage, detail = TYPES_LOGS[type_]
        condition, parametres_type = _filtre_logs(equipement_id, depuis, jusqu_a, colonne_horodatage, nom_archive)
        selections.append(
            f"SELECT equipement_id, nom_equipement_archive, {colonne_horodatage} AS horodatage, "
            f"'{type_}' AS type_log, {detail} AS detail FROM {table} WHERE {condition}")
        parametres += parametres_type
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT sous_requete.equipement_id, "
            "COALESCE(equipement.nom, sous_requete.nom_equipement_archive) AS equipement_nom, "
            "sous_requete.horodatage, sous_requete.type_log, sous_requete.detail "
            f"FROM ({' UNION ALL '.join(selections)}) AS sous_requete "
            "LEFT JOIN equipement ON equipement.id = sous_requete.equipement_id "
            "ORDER BY sous_requete.horodatage DESC LIMIT ? OFFSET ?",
            parametres + [limite, decalage],
        ).fetchall()


def lister_noms_equipements_archives() -> list[str]:
    """Noms des équipements archivés qui ont encore des logs (pour le filtre de la page Logs)."""
    with get_connection() as connexion:
        return [ligne[0] for ligne in connexion.execute(
            " UNION ".join(f"SELECT nom_equipement_archive FROM {table} "
                           "WHERE equipement_id IS NULL AND nom_equipement_archive IS NOT NULL"
                           for table in TABLES_LOGS) + " ORDER BY 1"
        ).fetchall()]


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


def obtenir_metrique(code: str) -> sqlite3.Row | None:
    """Renvoie une métrique du catalogue par son code, ou None si elle n'existe pas."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM metrique WHERE code = ?", (code,)).fetchone()


def lister_debit(equipement_id: int, code_metrique: str, interface_id: int | None = None,
                  depuis: str | None = None, jusqu_a: str | None = None) -> list[dict]:
    """Débit (par seconde) d'un compteur de trafic par interface (if_in_octets/if_out_octets),
    calculé par différence entre deux relevés bruts consécutifs. Un trou dans les relevés
    bruts (équipement injoignable), ou un compteur revenu en arrière (remise à zéro ou
    dépassement de capacité, déjà journalisé en VALEUR_ABERRANTE à la collecte, cf.
    app/collecte.py), donne un trou ici aussi plutôt qu'un débit négatif."""
    mesures = lister_mesures(equipement_id, code_metrique, interface_id, depuis, jusqu_a)
    resultat = []
    precedente = None
    for mesure in mesures:
        resultat.append({"horodatage": mesure["horodatage"], "valeur": _debit_entre(precedente, mesure)})
        precedente = mesure
    return resultat


def lister_mesures_agregees(equipement_id: int, code_metrique: str, interface_id: int | None = None,
                            depuis: str | None = None, jusqu_a: str | None = None) -> list[dict]:
    """Mesures agrégées (une par demi-heure) d'une métrique, au même format que les mesures
    brutes pour les graphiques : « valeur » = la moyenne de la tranche (déjà un débit pour
    un compteur de trafic). Minimum et maximum sont fournis en plus."""
    condition = "mesure_agregee.equipement_id = ? AND metrique.code = ?"
    parametres: list = [equipement_id, code_metrique]
    if interface_id is not None:
        condition += " AND mesure_agregee.interface_id = ?"
        parametres.append(interface_id)
    if depuis:
        condition += " AND mesure_agregee.horodatage >= ?"
        parametres.append(depuis)
    if jusqu_a:
        condition += " AND mesure_agregee.horodatage <= ?"
        parametres.append(jusqu_a)
    with get_connection() as connexion:
        return [dict(ligne) for ligne in connexion.execute(
            "SELECT mesure_agregee.horodatage, mesure_agregee.moyenne AS valeur, "
            "mesure_agregee.minimum, mesure_agregee.maximum "
            "FROM mesure_agregee JOIN metrique ON metrique.id = mesure_agregee.metrique_id "
            f"WHERE {condition} ORDER BY mesure_agregee.horodatage",
            parametres,
        ).fetchall()]


def debit_instantane(equipement_id: int, metrique_id: int, interface_id: int) -> float | None:
    """Débit (par seconde) entre les deux derniers relevés bruts d'un compteur de trafic par
    interface, pour le calcul immédiat du taux d'utilisation à la collecte (cf.
    app/collecte.py::_calculer_utilisation_interfaces). Même règle que lister_debit : un
    compteur revenu en arrière donne None, jamais un débit négatif."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT horodatage, valeur FROM mesure WHERE equipement_id = ? AND metrique_id = ? "
            "AND interface_id = ? AND valeur IS NOT NULL ORDER BY horodatage DESC LIMIT 2",
            (equipement_id, metrique_id, interface_id),
        ).fetchall()
    if len(lignes) < 2:
        return None
    recente, precedente = lignes
    delta_valeur = recente["valeur"] - precedente["valeur"]
    delta_temps = (datetime.fromisoformat(recente["horodatage"])
                   - datetime.fromisoformat(precedente["horodatage"])).total_seconds()
    if delta_valeur < 0 or delta_temps <= 0:
        return None
    return delta_valeur / delta_temps


def derniere_valeur_mesure(equipement_id: int, metrique_id: int,
                            interface_id: int | None = None) -> float | None:
    """Dernière valeur brute connue d'une métrique (et, si fournie, d'une interface), pour
    détecter à la collecte un compteur revenu en arrière (cf. app/collecte.py)."""
    condition = "equipement_id = ? AND metrique_id = ? AND valeur IS NOT NULL"
    parametres: list = [equipement_id, metrique_id]
    if interface_id is not None:
        condition += " AND interface_id = ?"
        parametres.append(interface_id)
    else:
        condition += " AND interface_id IS NULL"
    with get_connection() as connexion:
        ligne = connexion.execute(
            f"SELECT valeur FROM mesure WHERE {condition} ORDER BY horodatage DESC LIMIT 1",
            parametres,
        ).fetchone()
        return ligne["valeur"] if ligne else None


def trouver_equipement(recherche: str) -> sqlite3.Row | None:
    """Trouve un équipement par nom ou adresse exacte, sinon par correspondance partielle
    si elle désigne un seul équipement sans ambiguïté. Renvoie None si rien ou plusieurs."""
    with get_connection() as connexion:
        ligne = connexion.execute(
            "SELECT * FROM equipement WHERE nom = ? OR adresse = ?", (recherche, recherche)
        ).fetchone()
        if ligne:
            return ligne
        resultats = connexion.execute(
            "SELECT * FROM equipement WHERE nom LIKE ? OR adresse LIKE ?",
            (f"%{recherche}%", f"%{recherche}%"),
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


def lister_modeles_supervision() -> list[sqlite3.Row]:
    """Renvoie tous les modèles de supervision, triés par nom."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM modele_supervision ORDER BY nom").fetchall()


def creer_groupe(nom: str) -> int:
    """Insère un groupe et renvoie son id."""
    with get_connection() as connexion:
        curseur = connexion.execute("INSERT INTO groupe (nom) VALUES (?)", (nom,))
        return curseur.lastrowid


def lister_groupes_vides() -> list[sqlite3.Row]:
    """Groupes qui ne contiennent aucun équipement (candidats à la suppression)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT groupe.* FROM groupe WHERE NOT EXISTS "
            "(SELECT 1 FROM equipement WHERE equipement.groupe_id = groupe.id) "
            "ORDER BY groupe.nom"
        ).fetchall()


def supprimer_groupe_si_vide(groupe_id: int) -> None:
    """Supprime un groupe, seulement s'il ne contient encore aucun équipement (vérifié ici,
    pas juste côté affichage, pour parer à un équipement ajouté entre-temps dans ce groupe)."""
    with get_connection() as connexion:
        connexion.execute(
            "DELETE FROM groupe WHERE id = ? AND NOT EXISTS "
            "(SELECT 1 FROM equipement WHERE equipement.groupe_id = groupe.id)",
            (groupe_id,),
        )


# ---------------------------------------------------------------- collecte

def lister_equipements_a_collecter(intervalle_polling_defaut: int) -> list[sqlite3.Row]:
    """Équipements dont l'intervalle effectif (le leur, ou la valeur par défaut) est écoulé
    depuis la dernière collecte (ou qui n'ont encore jamais été collectés)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM equipement WHERE "
            "   derniere_collecte IS NULL"
            "   OR (strftime('%s', 'now') - strftime('%s', derniere_collecte)) "
            "       >= COALESCE(intervalle_polling, ?)",
            (intervalle_polling_defaut,),
        ).fetchall()


def obtenir_parametres_generaux() -> sqlite3.Row:
    """Renvoie l'unique ligne de réglages globaux."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM parametres_generaux WHERE id = 1").fetchone()


def lister_elements_actifs(modele_supervision_id: int) -> list[sqlite3.Row]:
    """Éléments surveillés actifs d'un modèle, avec le code et les propriétés de leur métrique."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT element_surveille.oid, metrique.id AS metrique_id, metrique.code, "
            "metrique.libelle, metrique.unite, metrique.type_valeur, metrique.par_interface "
            "FROM element_surveille JOIN metrique ON metrique.id = element_surveille.metrique_id "
            "WHERE element_surveille.modele_supervision_id = ? AND element_surveille.actif = 1 "
            "ORDER BY metrique.libelle",
            (modele_supervision_id,),
        ).fetchall()


# ---------------------------------------------------------------- modèles de supervision

def lister_modeles_detailles() -> list[sqlite3.Row]:
    """Modèles avec, pour la page de gestion, leur nombre d'équipements et la liste de
    leurs métriques (libellés séparés par « · »)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT modele_supervision.*, "
            "  (SELECT COUNT(*) FROM equipement "
            "   WHERE equipement.modele_supervision_id = modele_supervision.id) AS nb_equipements, "
            "  (SELECT COUNT(*) FROM element_surveille "
            "   WHERE element_surveille.modele_supervision_id = modele_supervision.id) AS nb_metriques, "
            "  (SELECT group_concat(metrique.libelle, ' · ') FROM element_surveille "
            "   JOIN metrique ON metrique.id = element_surveille.metrique_id "
            "   WHERE element_surveille.modele_supervision_id = modele_supervision.id) AS metriques "
            "FROM modele_supervision ORDER BY modele_supervision.nom"
        ).fetchall()


def obtenir_modele(modele_id: int) -> sqlite3.Row | None:
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM modele_supervision WHERE id = ?", (modele_id,)).fetchone()


def creer_modele(nom: str, description: str | None, modele_source_id: int | None = None) -> int:
    """Crée un modèle ; s'il part d'un modèle existant, recopie ses éléments surveillés
    (mêmes métriques, mêmes OID, même état actif/inactif). Lève IntegrityError si le nom
    est déjà pris."""
    with get_connection() as connexion:
        curseur = connexion.execute(
            "INSERT INTO modele_supervision (nom, description) VALUES (?, ?)", (nom, description)
        )
        if modele_source_id:
            connexion.execute(
                "INSERT INTO element_surveille (modele_supervision_id, metrique_id, oid, actif) "
                "SELECT ?, metrique_id, oid, actif FROM element_surveille WHERE modele_supervision_id = ?",
                (curseur.lastrowid, modele_source_id),
            )
        return curseur.lastrowid


def modifier_modele(modele_id: int, nom: str, description: str | None) -> None:
    """Lève IntegrityError si le nouveau nom est déjà pris par un autre modèle."""
    with get_connection() as connexion:
        connexion.execute(
            "UPDATE modele_supervision SET nom = ?, description = ? WHERE id = ?",
            (nom, description, modele_id),
        )


def supprimer_modele(modele_id: int) -> None:
    """Supprime un modèle et ses éléments surveillés (cascade). Lève IntegrityError s'il
    est encore utilisé par un équipement (clé étrangère sans cascade sur equipement)."""
    with get_connection() as connexion:
        connexion.execute("DELETE FROM modele_supervision WHERE id = ?", (modele_id,))


def lister_equipements_du_modele(modele_id: int) -> list[sqlite3.Row]:
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT id, nom, statut FROM equipement WHERE modele_supervision_id = ? ORDER BY nom",
            (modele_id,),
        ).fetchall()


def lister_elements_modele(modele_id: int) -> list[sqlite3.Row]:
    """Tous les éléments surveillés d'un modèle, actifs ou non, avec leur métrique."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT element_surveille.id, element_surveille.oid, element_surveille.actif, "
            "metrique.code, metrique.libelle, metrique.unite, metrique.type_valeur, metrique.par_interface "
            "FROM element_surveille JOIN metrique ON metrique.id = element_surveille.metrique_id "
            "WHERE element_surveille.modele_supervision_id = ? ORDER BY metrique.libelle",
            (modele_id,),
        ).fetchall()


def lister_metriques_catalogue() -> list[sqlite3.Row]:
    """Métriques qu'on peut associer à un OID (toutes sauf celles purement calculées, qui
    n'ont pas d'OID : le taux d'utilisation des interfaces)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM metrique WHERE code != 'utilisation_interface' ORDER BY libelle"
        ).fetchall()


def creer_metrique(libelle: str, unite: str | None, type_valeur: str, par_interface: bool) -> int:
    """Ajoute une métrique au catalogue. Son code (identifiant technique) est déduit du
    libellé (« Température SW1 » -> « temperature_sw1 »), avec un suffixe s'il est déjà
    pris : un code existant, notamment cpu/ram/volume qui ont un traitement particulier à
    la collecte, n'est donc jamais réutilisé par erreur."""
    base = unicodedata.normalize("NFKD", libelle).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_") or "metrique"
    with get_connection() as connexion:
        code, suffixe = base, 2
        while connexion.execute("SELECT 1 FROM metrique WHERE code = ?", (code,)).fetchone():
            code, suffixe = f"{base}_{suffixe}", suffixe + 1
        curseur = connexion.execute(
            "INSERT INTO metrique (code, libelle, unite, type_valeur, par_interface) VALUES (?, ?, ?, ?, ?)",
            (code, libelle, unite, type_valeur, int(par_interface)),
        )
        return curseur.lastrowid


def ajouter_element(modele_id: int, metrique_id: int, oid: str) -> None:
    """Lève IntegrityError si cette métrique est déjà supervisée par ce modèle."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO element_surveille (modele_supervision_id, metrique_id, oid) VALUES (?, ?, ?)",
            (modele_id, metrique_id, oid),
        )


def _modele_de_l_element(connexion: sqlite3.Connection, element_id: int) -> int | None:
    ligne = connexion.execute(
        "SELECT modele_supervision_id FROM element_surveille WHERE id = ?", (element_id,)
    ).fetchone()
    return ligne["modele_supervision_id"] if ligne else None


def modele_de_l_element(element_id: int) -> int | None:
    with get_connection() as connexion:
        return _modele_de_l_element(connexion, element_id)


def modifier_oid_element(element_id: int, oid: str) -> int | None:
    """Change l'OID d'un élément surveillé ; renvoie l'id de son modèle (None si introuvable)."""
    with get_connection() as connexion:
        connexion.execute("UPDATE element_surveille SET oid = ? WHERE id = ?", (oid, element_id))
        return _modele_de_l_element(connexion, element_id)


def basculer_element(element_id: int) -> int | None:
    """Active/désactive un élément surveillé (désactivé = plus collecté, mais conservé)."""
    with get_connection() as connexion:
        connexion.execute("UPDATE element_surveille SET actif = 1 - actif WHERE id = ?", (element_id,))
        return _modele_de_l_element(connexion, element_id)


def supprimer_element(element_id: int) -> int | None:
    """Retire une métrique d'un modèle (la métrique reste au catalogue, et son historique
    de mesures est conservé)."""
    with get_connection() as connexion:
        modele_id = _modele_de_l_element(connexion, element_id)
        connexion.execute("DELETE FROM element_surveille WHERE id = ?", (element_id,))
        return modele_id


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
                      vitesse: int | None, statut: str) -> sqlite3.Row:
    """Crée l'interface si elle est nouvelle, sinon met à jour son nom/vitesse/statut
    (jamais sa visibilité, que seul basculer_visibilite_interface change). Renvoie son id
    et son indicateur de visibilité, pour que la collecte sache si elle doit relever ses
    mesures par interface ou non."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO interface (equipement_id, index_snmp, nom, vitesse, statut) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (equipement_id, index_snmp) DO UPDATE SET "
            "nom = excluded.nom, vitesse = excluded.vitesse, statut = excluded.statut",
            (equipement_id, index_snmp, nom, vitesse, statut),
        )
        return connexion.execute(
            "SELECT id, visible FROM interface WHERE equipement_id = ? AND index_snmp = ?",
            (equipement_id, index_snmp),
        ).fetchone()


def upsert_volume(equipement_id: int, index_snmp: int, nom: str, taille_octets: int,
                   utilise_octets: int, pourcentage_utilise: float) -> sqlite3.Row:
    """Crée le volume s'il est nouveau (visible par défaut : seuls les volumes qui
    représentent un vrai disque arrivent ici, cf. app/collecte.py), sinon met à jour son
    nom/sa taille/son pourcentage utilisé (jamais sa visibilité, que seul
    basculer_visibilite_volume change). Renvoie son id et son indicateur de visibilité."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO volume (equipement_id, index_snmp, nom, taille_octets, utilise_octets, "
            "pourcentage_utilise) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (equipement_id, index_snmp) DO UPDATE SET "
            "nom = excluded.nom, taille_octets = excluded.taille_octets, "
            "utilise_octets = excluded.utilise_octets, pourcentage_utilise = excluded.pourcentage_utilise",
            (equipement_id, index_snmp, nom, taille_octets, utilise_octets, pourcentage_utilise),
        )
        return connexion.execute(
            "SELECT id, visible FROM volume WHERE equipement_id = ? AND index_snmp = ?",
            (equipement_id, index_snmp),
        ).fetchone()


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
    sortie de EN_ATTENTE/INDISPONIBLE. Ne touche pas à DEGRADE : ce statut relève des
    alertes (cf. declencher_alerte_*/_cloturer_alerte), pas de la connectivité.

    Au retour d'une indisponibilité, l'équipement repasse en DEGRADE s'il a encore des
    alertes actives (ex. une alerte mémoire déclenchée avant la coupure) : les fonctions
    declencher_alerte_* ne le feraient pas, puisqu'elles s'arrêtent dès que l'alerte existe
    déjà. Sans ça, il restait OPERATIONNEL avec une alerte active."""
    with get_connection() as connexion:
        ancien_statut = connexion.execute(
            "SELECT statut FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()["statut"]
        connexion.execute(
            "UPDATE equipement SET nb_echecs_consecutifs = 0, "
            "derniere_collecte = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
            (equipement_id,),
        )
        if ancien_statut not in ("OPERATIONNEL", "DEGRADE"):
            a_des_alertes = connexion.execute(
                "SELECT 1 FROM alerte WHERE equipement_id = ? AND etat = 'ACTIVE' LIMIT 1",
                (equipement_id,),
            ).fetchone()
            _changer_statut(connexion, equipement_id, ancien_statut,
                            "DEGRADE" if a_des_alertes else "OPERATIONNEL")


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


# ---------------------------------------------------------------- seuils et alertes

def lister_seuils_configurables() -> list[sqlite3.Row]:
    """Métriques pour lesquelles un seuil global peut être défini : CPU, RAM, stockage et
    utilisation d'interface. Liste fixe : les métriques créées dans un modèle personnalisé
    (ex. une température) n'ont pas de seuil, tant que les seuils ne sont pas réglables par
    équipement. Avec le seuil actuel s'il y en a un (LEFT JOIN : peut n'y en avoir aucun)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT metrique.id AS metrique_id, metrique.code, metrique.libelle, metrique.unite, "
            "seuil.operateur, seuil.valeur_seuil "
            "FROM metrique LEFT JOIN seuil ON seuil.metrique_id = metrique.id "
            "WHERE metrique.code IN ('cpu', 'ram', 'volume', 'utilisation_interface') "
            "ORDER BY metrique.libelle"
        ).fetchall()


def definir_seuil(metrique_id: int, operateur: str, valeur_seuil: float) -> None:
    """Crée ou remplace le seuil global d'une métrique."""
    with get_connection() as connexion:
        connexion.execute(
            "INSERT INTO seuil (metrique_id, operateur, valeur_seuil) VALUES (?, ?, ?) "
            "ON CONFLICT (metrique_id) DO UPDATE SET "
            "operateur = excluded.operateur, valeur_seuil = excluded.valeur_seuil",
            (metrique_id, operateur, valeur_seuil),
        )


def supprimer_seuil(metrique_id: int) -> None:
    """Retire le seuil d'une métrique. Clôture d'abord les alertes actives qu'il avait
    déclenchées : une fois le seuil supprimé, plus rien ne les réévaluerait jamais, elles
    resteraient actives pour toujours et l'équipement resterait coincé en DEGRADE."""
    with get_connection() as connexion:
        seuil = connexion.execute("SELECT id FROM seuil WHERE metrique_id = ?", (metrique_id,)).fetchone()
        if seuil is None:
            return
        for alerte in connexion.execute(
            "SELECT id, equipement_id FROM alerte WHERE seuil_id = ? AND etat = 'ACTIVE'", (seuil["id"],)
        ).fetchall():
            _cloturer_alerte(connexion, alerte["id"], alerte["equipement_id"])
        connexion.execute("DELETE FROM seuil WHERE id = ?", (seuil["id"],))


def obtenir_seuil(metrique_id: int) -> sqlite3.Row | None:
    """Le seuil global d'une métrique, ou None s'il n'y en a pas."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM seuil WHERE metrique_id = ?", (metrique_id,)).fetchone()


def _degrader_si_necessaire(connexion: sqlite3.Connection, equipement_id: int) -> None:
    """Passe l'équipement en DEGRADE, sauf s'il est déjà INDISPONIBLE (qui reste prioritaire)
    ou déjà DEGRADE (pour une autre raison). Appelée à l'intérieur d'une connexion ouverte."""
    ligne = connexion.execute("SELECT statut FROM equipement WHERE id = ?", (equipement_id,)).fetchone()
    if ligne["statut"] not in ("INDISPONIBLE", "DEGRADE"):
        _changer_statut(connexion, equipement_id, ligne["statut"], "DEGRADE")


def declencher_alerte_seuil(equipement_id: int, seuil_id: int, metrique_id: int, valeur: float) -> None:
    """Crée l'alerte de dépassement de seuil si elle n'existe pas déjà (idempotent : appelée
    à chaque cycle de collecte tant que le seuil reste dépassé), et passe l'équipement en DEGRADE."""
    with get_connection() as connexion:
        deja_active = connexion.execute(
            "SELECT 1 FROM alerte WHERE equipement_id = ? AND seuil_id = ? AND etat = 'ACTIVE'",
            (equipement_id, seuil_id),
        ).fetchone()
        if deja_active:
            return

        metrique = connexion.execute(
            "SELECT libelle, unite FROM metrique WHERE id = ?", (metrique_id,)
        ).fetchone()
        unite = metrique["unite"] or ""
        message = f"{metrique['libelle']} à {valeur:.1f}{unite}, seuil dépassé"
        connexion.execute(
            "INSERT INTO alerte (equipement_id, type_alerte, seuil_id, valeur_mesuree, message) "
            "VALUES (?, 'SEUIL_DEPASSE', ?, ?, ?)",
            (equipement_id, seuil_id, valeur, message),
        )
        _degrader_si_necessaire(connexion, equipement_id)


def _cloturer_alerte(connexion: sqlite3.Connection, alerte_id: int, equipement_id: int) -> None:
    """Clôture une alerte, et repasse l'équipement en OPERATIONNEL s'il n'a plus aucune
    autre alerte active, de quelque type que ce soit. Appelée dans une connexion ouverte."""
    connexion.execute(
        "UPDATE alerte SET etat = 'CLOTUREE', "
        "date_cloture = strftime('%Y-%m-%dT%H:%M:%S', 'now') WHERE id = ?",
        (alerte_id,),
    )
    autres_actives = connexion.execute(
        "SELECT COUNT(*) AS n FROM alerte WHERE equipement_id = ? AND etat = 'ACTIVE'",
        (equipement_id,),
    ).fetchone()["n"]
    if autres_actives == 0:
        statut_actuel = connexion.execute(
            "SELECT statut FROM equipement WHERE id = ?", (equipement_id,)
        ).fetchone()["statut"]
        if statut_actuel == "DEGRADE":
            _changer_statut(connexion, equipement_id, "DEGRADE", "OPERATIONNEL")


def resoudre_alerte_seuil(equipement_id: int, seuil_id: int) -> None:
    """Clôture l'alerte de dépassement de seuil si elle était active, et repasse
    l'équipement en OPERATIONNEL si c'était sa dernière alerte active."""
    with get_connection() as connexion:
        alerte = connexion.execute(
            "SELECT id FROM alerte WHERE equipement_id = ? AND seuil_id = ? AND etat = 'ACTIVE'",
            (equipement_id, seuil_id),
        ).fetchone()
        if alerte is None:
            return
        _cloturer_alerte(connexion, alerte["id"], equipement_id)


def declencher_alerte_interface(equipement_id: int, interface_id: int, interface_nom: str) -> None:
    """Crée l'alerte "interface supervisée indisponible" si elle n'existe pas déjà, et passe
    l'équipement en DEGRADE."""
    with get_connection() as connexion:
        deja_active = connexion.execute(
            "SELECT 1 FROM alerte WHERE equipement_id = ? AND interface_id = ? AND etat = 'ACTIVE'",
            (equipement_id, interface_id),
        ).fetchone()
        if deja_active:
            return
        message = f"Interface {interface_nom} indisponible (DOWN)"
        connexion.execute(
            "INSERT INTO alerte (equipement_id, type_alerte, interface_id, message) "
            "VALUES (?, 'INTERFACE_INDISPONIBLE', ?, ?)",
            (equipement_id, interface_id, message),
        )
        _degrader_si_necessaire(connexion, equipement_id)


def resoudre_alerte_interface(equipement_id: int, interface_id: int) -> None:
    """Clôture l'alerte "interface indisponible" si elle était active (l'interface est
    revenue UP, ou n'est plus supervisée), et repasse l'équipement en OPERATIONNEL si
    c'était sa dernière alerte active."""
    with get_connection() as connexion:
        alerte = connexion.execute(
            "SELECT id FROM alerte WHERE equipement_id = ? AND interface_id = ? AND etat = 'ACTIVE'",
            (equipement_id, interface_id),
        ).fetchone()
        if alerte is None:
            return
        _cloturer_alerte(connexion, alerte["id"], equipement_id)


def declencher_alerte_interface_utilisation(equipement_id: int, interface_id: int, seuil_id: int,
                                             valeur: float, nom_interface: str) -> None:
    """Crée l'alerte de dépassement de seuil sur le taux d'utilisation de cette interface si
    elle n'existe pas déjà (seuil_id ET interface_id dans la recherche, pour ne pas se
    confondre avec une alerte "interface indisponible" sur la même interface), et passe
    l'équipement en DEGRADE."""
    with get_connection() as connexion:
        deja_active = connexion.execute(
            "SELECT 1 FROM alerte WHERE equipement_id = ? AND interface_id = ? AND seuil_id = ? "
            "AND etat = 'ACTIVE'",
            (equipement_id, interface_id, seuil_id),
        ).fetchone()
        if deja_active:
            return
        message = f"Interface « {nom_interface} » utilisée à {valeur:.1f}%, seuil dépassé"
        connexion.execute(
            "INSERT INTO alerte (equipement_id, type_alerte, seuil_id, interface_id, valeur_mesuree, message) "
            "VALUES (?, 'SEUIL_DEPASSE', ?, ?, ?, ?)",
            (equipement_id, seuil_id, interface_id, valeur, message),
        )
        _degrader_si_necessaire(connexion, equipement_id)


def resoudre_alerte_interface_utilisation(equipement_id: int, interface_id: int, seuil_id: int) -> None:
    """Clôture l'alerte de seuil sur le taux d'utilisation de cette interface si elle était
    active (repassée sous le seuil, ou plus supervisée), et repasse l'équipement en
    OPERATIONNEL si c'était sa dernière alerte active."""
    with get_connection() as connexion:
        alerte = connexion.execute(
            "SELECT id FROM alerte WHERE equipement_id = ? AND interface_id = ? AND seuil_id = ? "
            "AND etat = 'ACTIVE'",
            (equipement_id, interface_id, seuil_id),
        ).fetchone()
        if alerte is None:
            return
        _cloturer_alerte(connexion, alerte["id"], equipement_id)


def declencher_alerte_volume(equipement_id: int, volume_id: int, seuil_id: int,
                              valeur: float, nom_volume: str) -> None:
    """Crée l'alerte de dépassement de seuil sur ce volume si elle n'existe pas déjà
    (un volume donné peut avoir sa propre alerte, comme une interface), et passe
    l'équipement en DEGRADE."""
    with get_connection() as connexion:
        deja_active = connexion.execute(
            "SELECT 1 FROM alerte WHERE equipement_id = ? AND volume_id = ? AND etat = 'ACTIVE'",
            (equipement_id, volume_id),
        ).fetchone()
        if deja_active:
            return
        message = f"Stockage « {nom_volume} » à {valeur:.1f}%, seuil dépassé"
        connexion.execute(
            "INSERT INTO alerte (equipement_id, type_alerte, seuil_id, volume_id, valeur_mesuree, message) "
            "VALUES (?, 'SEUIL_DEPASSE', ?, ?, ?, ?)",
            (equipement_id, seuil_id, volume_id, valeur, message),
        )
        _degrader_si_necessaire(connexion, equipement_id)


def resoudre_alerte_volume(equipement_id: int, volume_id: int) -> None:
    """Clôture l'alerte de seuil sur ce volume si elle était active (repassé sous le
    seuil, ou plus supervisé), et repasse l'équipement en OPERATIONNEL si c'était sa
    dernière alerte active."""
    with get_connection() as connexion:
        alerte = connexion.execute(
            "SELECT id FROM alerte WHERE equipement_id = ? AND volume_id = ? AND etat = 'ACTIVE'",
            (equipement_id, volume_id),
        ).fetchone()
        if alerte is None:
            return
        _cloturer_alerte(connexion, alerte["id"], equipement_id)


def lister_alertes(etat: str | None = "ACTIVE", equipement_id: int | None = None) -> list[sqlite3.Row]:
    """Alertes, les plus récentes en premier, filtrables par état (None = toutes) et/ou
    par équipement (utilisé sur la fiche équipement pour expliquer un statut DEGRADE)."""
    condition = "1 = 1"
    parametres: list = []
    if etat:
        condition += " AND alerte.etat = ?"
        parametres.append(etat)
    if equipement_id is not None:
        condition += " AND alerte.equipement_id = ?"
        parametres.append(equipement_id)
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT alerte.*, equipement.nom AS equipement_nom "
            "FROM alerte JOIN equipement ON equipement.id = alerte.equipement_id "
            f"WHERE {condition} ORDER BY alerte.date_declenchement DESC",
            parametres,
        ).fetchall()
