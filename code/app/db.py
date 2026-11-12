"""Accès à la base SQLite : connexion, création du schéma, fonctions d'accès aux données.

Pas d'ORM : le SQL est écrit à la main. Chaque fonction ouvre sa propre
connexion et la referme ; pour le volume de ce projet, c'est plus simple à suivre qu'un pool
de connexions partagées.
"""
import sqlite3
from datetime import datetime
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
    """Ajoute les colonnes apparues après la création initiale d'une base existante
    (pas d'outil de migration dans ce projet : on reste sur de simples ALTER TABLE)."""
    try:
        connexion.execute("ALTER TABLE interface ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # colonne déjà présente (base créée après son ajout au schéma)

    try:
        connexion.execute("ALTER TABLE equipement ADD COLUMN duree_conservation INTEGER")
    except sqlite3.OperationalError:
        pass  # colonne déjà présente

    try:
        connexion.execute(
            "ALTER TABLE alerte ADD COLUMN volume_id INTEGER REFERENCES volume(id) ON DELETE SET NULL"
        )
    except sqlite3.OperationalError:
        pass  # colonne déjà présente (une simple colonne nullable, pas de CHECK à élargir ici)

    for colonne in ("taille_octets", "utilise_octets"):
        try:
            connexion.execute(f"ALTER TABLE volume ADD COLUMN {colonne} INTEGER")
        except sqlite3.OperationalError:
            pass  # colonne déjà présente

    # alerte.type_alerte doit accepter INTERFACE_INDISPONIBLE, et alerte a besoin d'un
    # interface_id : un ALTER TABLE ne peut pas élargir une contrainte CHECK existante, donc
    # on recrée la table (en copiant les lignes) si elle n'a pas encore la nouvelle colonne.
    colonnes_alerte = [ligne[1] for ligne in connexion.execute("PRAGMA table_info(alerte)").fetchall()]
    if colonnes_alerte and "interface_id" not in colonnes_alerte:
        connexion.executescript(
            "ALTER TABLE alerte RENAME TO alerte_ancienne;"
            "CREATE TABLE alerte ("
            "    id                 INTEGER PRIMARY KEY AUTOINCREMENT,"
            "    equipement_id      INTEGER NOT NULL REFERENCES equipement(id) ON DELETE CASCADE,"
            "    type_alerte        TEXT NOT NULL CHECK (type_alerte IN"
            "        ('INDISPONIBILITE', 'SEUIL_DEPASSE', 'INTERFACE_INDISPONIBLE')),"
            "    etat               TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (etat IN ('ACTIVE', 'CLOTUREE', 'MASQUEE')),"
            "    date_declenchement TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),"
            "    date_cloture       TEXT,"
            "    valeur_mesuree     REAL,"
            "    message            TEXT NOT NULL,"
            "    seuil_id           INTEGER REFERENCES seuil(id) ON DELETE SET NULL,"
            "    interface_id       INTEGER REFERENCES interface(id) ON DELETE SET NULL,"
            "    cause_racine_id    INTEGER REFERENCES alerte(id)"
            ");"
            "INSERT INTO alerte (id, equipement_id, type_alerte, etat, date_declenchement,"
            "                    date_cloture, valeur_mesuree, message, seuil_id, cause_racine_id) "
            "SELECT id, equipement_id, type_alerte, etat, date_declenchement,"
            "       date_cloture, valeur_mesuree, message, seuil_id, cause_racine_id "
            "FROM alerte_ancienne;"
            "DROP TABLE alerte_ancienne;"
        )


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


def purger_mesures_anciennes() -> int:
    """Supprime les mesures plus anciennes que la durée de conservation effective de
    chaque équipement (la sienne si réglée, sinon la valeur par défaut). Renvoie le
    nombre de lignes supprimées."""
    parametres = obtenir_parametres_generaux()
    total_supprime = 0
    with get_connection() as connexion:
        equipements = connexion.execute("SELECT id, duree_conservation FROM equipement").fetchall()
        for equipement in equipements:
            duree = equipement["duree_conservation"] or parametres["duree_conservation_detail"]
            curseur = connexion.execute(
                "DELETE FROM mesure WHERE equipement_id = ? "
                "AND horodatage < datetime('now', '-' || ? || ' days')",
                (equipement["id"], duree),
            )
            total_supprime += curseur.rowcount
    return total_supprime


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
    """Interfaces d'un équipement (visibles et masquées), triées par index SNMP."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM interface WHERE equipement_id = ? ORDER BY index_snmp",
            (equipement_id,),
        ).fetchall()


def dernieres_valeurs_par_interface(equipement_id: int, code_metrique: str) -> dict[int, float]:
    """Dernière valeur connue d'une métrique par-interface (ex. taux d'utilisation), pour
    chaque interface d'un équipement. Renvoie {interface_id: valeur}, pour affichage sur la
    fiche équipement (cf. fiche_equipement.html)."""
    with get_connection() as connexion:
        lignes = connexion.execute(
            "SELECT mesure.interface_id, mesure.valeur FROM mesure "
            "JOIN metrique ON metrique.id = mesure.metrique_id "
            "WHERE mesure.equipement_id = ? AND metrique.code = ? AND mesure.interface_id IS NOT NULL "
            "AND mesure.id = ("
            "    SELECT MAX(m2.id) FROM mesure AS m2 WHERE m2.equipement_id = mesure.equipement_id "
            "    AND m2.metrique_id = mesure.metrique_id AND m2.interface_id = mesure.interface_id"
            ")",
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
                  colonne_horodatage: str = "horodatage") -> tuple[str, list]:
    """Construit la clause WHERE commune aux tables de logs, et ses paramètres. Le nom de
    la colonne horodatage varie selon la table (alerte utilise date_declenchement)."""
    condition = "1 = 1"
    parametres: list = []
    if equipement_id is not None:
        condition += " AND equipement_id = ?"
        parametres.append(equipement_id)
    if depuis:
        condition += f" AND {colonne_horodatage} >= ?"
        parametres.append(depuis)
    if jusqu_a:
        condition += f" AND {colonne_horodatage} <= ?"
        parametres.append(jusqu_a)
    return condition, parametres


def compter_logs(equipement_id: int | None = None, depuis: str | None = None,
                  jusqu_a: str | None = None) -> int:
    """Nombre total de logs correspondant aux filtres (pour la pagination)."""
    condition_cs, parametres_cs = _filtre_logs(equipement_id, depuis, jusqu_a)
    condition_ec, parametres_ec = _filtre_logs(equipement_id, depuis, jusqu_a)
    condition_al, parametres_al = _filtre_logs(equipement_id, depuis, jusqu_a, "date_declenchement")
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT (SELECT COUNT(*) FROM changement_statut WHERE " + condition_cs + ") + "
            "       (SELECT COUNT(*) FROM erreur_collecte WHERE " + condition_ec + ") + "
            "       (SELECT COUNT(*) FROM alerte WHERE " + condition_al + ") AS total",
            parametres_cs + parametres_ec + parametres_al,
        ).fetchone()["total"]


def lister_logs(equipement_id: int | None = None, depuis: str | None = None,
                 jusqu_a: str | None = None, limite: int = 50, decalage: int = 0) -> list[sqlite3.Row]:
    """Historique unifié (changements de statut + erreurs de collecte + alertes), le plus
    récent en premier, filtrable par équipement et par plage de dates, et paginé. Chaque
    ligne porte un type_log pour les distinguer à l'affichage."""
    condition_cs, parametres_cs = _filtre_logs(equipement_id, depuis, jusqu_a)
    condition_ec, parametres_ec = _filtre_logs(equipement_id, depuis, jusqu_a)
    condition_al, parametres_al = _filtre_logs(equipement_id, depuis, jusqu_a, "date_declenchement")
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT equipement.id AS equipement_id, equipement.nom AS equipement_nom, "
            "sous_requete.horodatage, sous_requete.type_log, sous_requete.detail "
            "FROM ("
            "   SELECT equipement_id, horodatage, 'CHANGEMENT_STATUT' AS type_log, "
            "          (ancien_statut || ' -> ' || nouveau_statut) AS detail "
            "   FROM changement_statut WHERE " + condition_cs + " "
            "   UNION ALL "
            "   SELECT equipement_id, horodatage, 'ERREUR_COLLECTE' AS type_log, "
            "          (type_erreur || COALESCE(' : ' || message, '')) AS detail "
            "   FROM erreur_collecte WHERE " + condition_ec + " "
            "   UNION ALL "
            "   SELECT equipement_id, date_declenchement AS horodatage, 'ALERTE' AS type_log, "
            "          message AS detail "
            "   FROM alerte WHERE " + condition_al +
            ") AS sous_requete "
            "JOIN equipement ON equipement.id = sous_requete.equipement_id "
            "ORDER BY sous_requete.horodatage DESC LIMIT ? OFFSET ?",
            parametres_cs + parametres_ec + parametres_al + [limite, decalage],
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
        debit = None
        if (precedente is not None and mesure["valeur"] is not None
                and precedente["valeur"] is not None):
            delta_valeur = mesure["valeur"] - precedente["valeur"]
            delta_temps = (datetime.fromisoformat(mesure["horodatage"])
                           - datetime.fromisoformat(precedente["horodatage"])).total_seconds()
            if delta_valeur >= 0 and delta_temps > 0:
                debit = delta_valeur / delta_temps
        resultat.append({"horodatage": mesure["horodatage"], "valeur": debit})
        precedente = mesure
    return resultat


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

def lister_equipements_actifs() -> list[sqlite3.Row]:
    """Renvoie les équipements à collecter (actif = 1)."""
    with get_connection() as connexion:
        return connexion.execute("SELECT * FROM equipement WHERE actif = 1").fetchall()


def lister_equipements_a_collecter(intervalle_polling_defaut: int) -> list[sqlite3.Row]:
    """Équipements actifs dont l'intervalle effectif (le leur, ou la valeur par défaut) est
    écoulé depuis la dernière collecte (ou qui n'ont encore jamais été collectés)."""
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM equipement WHERE actif = 1 AND ("
            "   derniere_collecte IS NULL"
            "   OR (strftime('%s', 'now') - strftime('%s', derniere_collecte)) "
            "       >= COALESCE(intervalle_polling, ?)"
            ")",
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
    passage à OPERATIONNEL si l'équipement ne l'était pas déjà. Ne touche pas à DEGRADE :
    ce statut relève des seuils (cf. declencher_alerte_seuil/resoudre_alerte_seuil), pas de
    la connectivité."""
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


# ---------------------------------------------------------------- seuils et alertes

def lister_seuils_configurables() -> list[sqlite3.Row]:
    """Métriques pour lesquelles un seuil global peut être défini : CPU, RAM et stockage
    pour l'instant (pas uptime, qui est un compteur, pas un pourcentage ; pas le
    trafic par interface, qui demanderait de savoir quelle interface a déclenché l'alerte).
    Avec le seuil actuel s'il y en a un (LEFT JOIN : peut n'y en avoir aucun)."""
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
            "operateur = excluded.operateur, valeur_seuil = excluded.valeur_seuil, actif = 1",
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


def obtenir_seuil_actif(metrique_id: int) -> sqlite3.Row | None:
    with get_connection() as connexion:
        return connexion.execute(
            "SELECT * FROM seuil WHERE metrique_id = ? AND actif = 1", (metrique_id,)
        ).fetchone()


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
