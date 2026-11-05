"""Service de collecte SNMP.

Pour chaque équipement actif, interroge les éléments surveillés de son modèle de
supervision et enregistre les mesures. Un échec sur un équipement (ou sur un indicateur
précis) n'empêche jamais la collecte du reste du parc : chaque équipement, et chaque
indicateur, est traité indépendamment. Un équipement passe INDISPONIBLE après plusieurs échecs
consécutifs, pas dès le premier timeout, pour éviter les fausses alertes.
"""
import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import config, db
from app.snmp_client import OID_SYS_UPTIME, SnmpError, snmp_get, snmp_walk

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"  # valeur de hrStorageType qui désigne la mémoire vive

STATUT_INTERFACE_SNMP = {1: "UP", 2: "DOWN"}  # ifOperStatus ; toute autre valeur -> INCONNU

# Préfixes d'interfaces virtuelles courantes, non proposées par défaut (l'admin garde le choix)
PREFIXES_VIRTUELS = ("docker", "veth", "br-", "tap", "sit", "lo", "ovs-system")

logger = logging.getLogger(__name__)

# Fréquence à laquelle on cherche les équipements à interroger (et non l'intervalle de collecte) :
# un équipement ajouté reçoit ainsi sa première collecte en quelques secondes.
PERIODE_VERIFICATION = 5   # secondes

_planificateur: AsyncIOScheduler | None = None
# Échéances gardées en mémoire : derniere_collecte n'est mise à jour qu'en cas de réussite, elle
# ferait réinterroger un équipement en panne à chaque vérification au lieu de suivre son intervalle.
_prochaine_collecte: dict[int, float] = {}   # id équipement -> instant de la prochaine collecte
_en_cours: dict[int, asyncio.Task] = {}      # id équipement -> collecte en cours


def demarrer() -> None:
    """Démarre le planificateur (à appeler depuis la boucle asyncio de l'application)."""
    global _planificateur
    _planificateur = AsyncIOScheduler()
    _planificateur.add_job(
        verifier_echeances, "interval", seconds=PERIODE_VERIFICATION, max_instances=1, coalesce=True,
    )
    _planificateur.start()


def arreter() -> None:
    """Arrête le planificateur et annule les collectes en cours."""
    global _planificateur
    if _planificateur is not None:
        _planificateur.shutdown(wait=False)
        _planificateur = None
    for tache in list(_en_cours.values()):
        tache.cancel()


def interface_proposee(interface) -> bool:
    """Sélection proposée à l'admin : les interfaces actives, hors virtuelles courantes."""
    return interface["statut"] == "UP" and not interface["nom"].lower().startswith(PREFIXES_VIRTUELS)


async def collecter_tout() -> None:
    """Lance la collecte de tous les équipements actifs, en parallèle (un équipement lent
    ne doit pas retarder les autres, cf. règle métier d'indépendance). Ignore l'intervalle
    de polling : utile pour un déclenchement manuel, pas pour le planificateur."""
    equipements = db.lister_equipements_actifs()
    await asyncio.gather(*(_collecter_sans_interrompre(equipement) for equipement in equipements))


async def verifier_echeances() -> None:
    """Appelée périodiquement par le planificateur : démarre, chacune dans sa propre tâche, les
    collectes dont l'intervalle de polling (le leur, ou la valeur par défaut) est écoulé. Elle
    n'attend pas leur fin : un équipement lent ne retarde donc pas les autres."""
    parametres = db.obtenir_parametres_generaux()
    maintenant = time.monotonic()
    for equipement in db.lister_equipements_actifs():
        id_equipement = equipement["id"]
        if id_equipement in _en_cours or maintenant < _prochaine_collecte.get(id_equipement, 0):
            continue
        intervalle = equipement["intervalle_polling"] or parametres["intervalle_polling_defaut"]
        _prochaine_collecte[id_equipement] = maintenant + intervalle
        tache = asyncio.create_task(_collecter_sans_interrompre(equipement))
        _en_cours[id_equipement] = tache
        tache.add_done_callback(lambda _, cle=id_equipement: _en_cours.pop(cle, None))


async def _collecter_sans_interrompre(equipement: dict) -> None:
    """Collecte un équipement ; une erreur imprévue est journalisée sans toucher aux autres."""
    try:
        await collecter_equipement(equipement)
    except Exception:
        # Un équipement supprimé pendant sa collecte fait échouer les écritures : rien à signaler
        if db.obtenir_equipement(equipement["id"]) is not None:
            logger.exception("Erreur imprévue pendant la collecte de %s", equipement["nom"])


async def collecter_equipement(equipement: dict) -> None:
    """Collecte un équipement : test de connectivité, puis chaque indicateur de son modèle."""
    parametres = db.obtenir_parametres_generaux()
    timeout, retries = parametres["delai_reponse_snmp"], config.SNMP_RETRIES
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    seuil = parametres["nb_echecs_avant_indisponibilite"]

    try:
        await snmp_get(host, community, [OID_SYS_UPTIME], timeout=timeout, retries=retries)
    except SnmpError as exc:
        db.enregistrer_erreur_collecte(equipement["id"], "INJOIGNABLE", str(exc))
        db.enregistrer_echec_collecte(equipement["id"], seuil)
        logger.warning(
            "Collecte en échec sur %s (%s), échec consécutif n° %d, indisponible à partir de %d : %s",
            equipement["nom"], host, equipement["nb_echecs_consecutifs"] + 1, seuil, exc,
        )
        return

    await decouvrir_interfaces(equipement, timeout, retries)
    interfaces = db.interfaces_a_mesurer(equipement["id"])

    for element in db.lister_elements_actifs(equipement["modele_supervision_id"]):
        try:
            if element["code"] == "cpu":
                await _collecter_cpu(equipement, element, timeout, retries)
            elif element["code"] == "ram":
                await _collecter_ram(equipement, element, timeout, retries)
            elif element["par_interface"]:
                await _collecter_par_interface(equipement, element, interfaces, timeout, retries)
            else:
                await _collecter_scalaire(equipement, element, timeout, retries)
        except (SnmpError, ValueError, TypeError) as exc:
            # ValueError/TypeError : l'agent répond « OID absent » à la place d'un nombre
            detail = exc if isinstance(exc, SnmpError) else "OID absent ou valeur non numérique"
            db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                            f"{element['code']} : {detail}")
            db.enregistrer_mesure(equipement["id"], element["metrique_id"], None)

    db.enregistrer_reussite_collecte(equipement["id"])
    if equipement["statut"] == "INDISPONIBLE":
        logger.info("%s (%s) est de nouveau joignable", equipement["nom"], host)


async def decouvrir_interfaces(equipement: dict, timeout: float, retries: int) -> bool:
    """Met à jour la table interface à partir de ifTable ; renvoie False si la lecture échoue."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    try:
        noms = await snmp_walk(host, community, OID_IF_DESCR, timeout=timeout, retries=retries)
        vitesses = await snmp_walk(host, community, OID_IF_SPEED, timeout=timeout, retries=retries)
        statuts = await snmp_walk(host, community, OID_IF_OPER_STATUS, timeout=timeout, retries=retries)
    except SnmpError as exc:
        db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                        f"Découverte des interfaces : {exc}")
        return False

    index_declares = []
    for oid, nom in noms.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        vitesse_oid = f"{OID_IF_SPEED}.{index_snmp}"
        statut_oid = f"{OID_IF_OPER_STATUS}.{index_snmp}"
        vitesse = int(vitesses[vitesse_oid]) if vitesse_oid in vitesses else None
        code_statut = int(statuts[statut_oid]) if statut_oid in statuts else None
        statut = STATUT_INTERFACE_SNMP.get(code_statut, "INCONNU")
        db.upsert_interface(equipement["id"], index_snmp, str(nom), vitesse, statut)
        index_declares.append(index_snmp)
    if index_declares:  # une réponse vide ne prouve pas que toutes les interfaces ont disparu
        db.marquer_interfaces_absentes(equipement["id"], index_declares)
    return True


async def _collecter_scalaire(equipement: dict, element, timeout: float, retries: int) -> None:
    """Indicateur qui existe en un seul exemplaire sur l'équipement (ex. uptime)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_get(host, community, [element["oid"]], timeout=timeout, retries=retries)
    db.enregistrer_mesure(equipement["id"], element["metrique_id"], float(valeurs[element["oid"]]))


async def _collecter_par_interface(equipement: dict, element, interfaces: dict[int, int],
                                    timeout: float, retries: int) -> None:
    """Indicateur relevé une fois par interface (trafic entrant/sortant)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_walk(host, community, element["oid"], timeout=timeout, retries=retries)
    if not valeurs:
        raise SnmpError("table vide ou non supportée")
    for oid, valeur in valeurs.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        interface_id = interfaces.get(index_snmp)
        if interface_id is not None:
            db.enregistrer_mesure(equipement["id"], element["metrique_id"], float(valeur), interface_id)


async def _collecter_cpu(equipement: dict, element, timeout: float, retries: int) -> None:
    """hrProcessorLoad : une valeur par cœur/processeur, on enregistre la moyenne."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_walk(host, community, element["oid"], timeout=timeout, retries=retries)
    if not valeurs:
        raise SnmpError("hrProcessorLoad non supporté par cet équipement")
    moyenne = sum(int(valeur) for valeur in valeurs.values()) / len(valeurs)
    db.enregistrer_mesure(equipement["id"], element["metrique_id"], moyenne)


async def _collecter_ram(equipement: dict, element, timeout: float, retries: int) -> None:
    """hrStorageTable : il faut d'abord trouver la ligne dont le type est "mémoire vive",
    puis combiner hrStorageUsed et hrStorageSize pour obtenir un pourcentage."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    types = await snmp_walk(host, community, OID_HR_STORAGE_TYPE, timeout=timeout, retries=retries)
    index_ram = next(
        (oid.rsplit(".", 1)[-1] for oid, type_oid in types.items() if str(type_oid) == HR_STORAGE_RAM),
        None,
    )
    if index_ram is None:
        raise SnmpError("hrStorageType « mémoire vive » non trouvé")

    oid_utilisee = f"{element['oid']}.{index_ram}"
    oid_totale = f"{OID_HR_STORAGE_SIZE}.{index_ram}"
    valeurs = await snmp_get(host, community, [oid_utilisee, oid_totale], timeout=timeout, retries=retries)
    totale = float(valeurs[oid_totale])
    if totale == 0:
        raise SnmpError("hrStorageSize = 0")
    pourcentage = float(valeurs[oid_utilisee]) / totale * 100
    db.enregistrer_mesure(equipement["id"], element["metrique_id"], pourcentage)
