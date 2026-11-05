"""Service de collecte SNMP.

Pour chaque équipement actif, interroge les éléments surveillés de son modèle de
supervision et enregistre les mesures. Un échec sur un équipement (ou sur un indicateur
précis) n'empêche jamais la collecte du reste du parc : chaque équipement, et chaque
indicateur, est traité indépendamment.
"""
import asyncio

from app import config, db
from app.snmp_client import OID_SYS_UPTIME, SnmpError, snmp_get, snmp_walk

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"  # valeur de hrStorageType qui désigne la mémoire vive

OID_CISCO_MEM_POOL_NAME = "1.3.6.1.4.1.9.9.48.1.1.1.2"
OID_CISCO_MEM_POOL_USED = "1.3.6.1.4.1.9.9.48.1.1.1.5"
OID_CISCO_MEM_POOL_FREE = "1.3.6.1.4.1.9.9.48.1.1.1.6"
CISCO_MEM_POOL_PRINCIPAL = "Processor"  # pool qui représente la mémoire système globale

STATUT_INTERFACE_SNMP = {1: "UP", 2: "DOWN"}  # ifOperStatus ; toute autre valeur -> INCONNU


async def collecter_tout() -> None:
    """Lance la collecte de tous les équipements actifs, en parallèle (un équipement lent
    ne doit pas retarder les autres, cf. règle métier d'indépendance). Ignore l'intervalle
    de polling : utile pour un déclenchement manuel, pas pour le planificateur."""
    equipements = db.lister_equipements_actifs()
    await asyncio.gather(*(collecter_equipement(equipement) for equipement in equipements))


async def verifier_echeances() -> None:
    """Appelée périodiquement par le planificateur : ne collecte que les équipements dont
    l'intervalle de polling (le leur, ou la valeur par défaut) est écoulé."""
    parametres = db.obtenir_parametres_generaux()
    equipements = db.lister_equipements_a_collecter(parametres["intervalle_polling_defaut"])
    await asyncio.gather(*(collecter_equipement(equipement) for equipement in equipements))


async def collecter_equipement(equipement: dict) -> None:
    """Collecte un équipement : test de connectivité, puis chaque indicateur de son modèle."""
    parametres = db.obtenir_parametres_generaux()
    timeout, retries = config.SNMP_TIMEOUT, config.SNMP_RETRIES
    host, community = equipement["adresse"], equipement["communaute_snmp"]

    try:
        await snmp_get(host, community, [OID_SYS_UPTIME], timeout=timeout, retries=retries)
    except SnmpError as exc:
        db.enregistrer_erreur_collecte(equipement["id"], "INJOIGNABLE", str(exc))
        db.enregistrer_echec_collecte(equipement["id"], parametres["nb_echecs_avant_indisponibilite"])
        return

    interfaces = await _decouvrir_interfaces(equipement, timeout, retries)

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
        except SnmpError as exc:
            db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                            f"{element['code']} : {exc}")
            db.enregistrer_mesure(equipement["id"], element["metrique_id"], None)

    db.enregistrer_reussite_collecte(equipement["id"])


async def _decouvrir_interfaces(equipement: dict, timeout: float, retries: int) -> dict[int, int]:
    """Met à jour la table interface à partir de ifTable (nom/vitesse/statut, pour toutes
    les interfaces), et renvoie {ifIndex: interface_id} pour celles qui sont visibles
    seulement : une interface masquée garde son identité à jour, mais ne remonte plus de
    mesures de trafic (cf. _collecter_par_interface, qui se base sur cette correspondance)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    try:
        noms = await snmp_walk(host, community, OID_IF_DESCR, timeout=timeout, retries=retries)
        vitesses = await snmp_walk(host, community, OID_IF_SPEED, timeout=timeout, retries=retries)
        statuts = await snmp_walk(host, community, OID_IF_OPER_STATUS, timeout=timeout, retries=retries)
    except SnmpError as exc:
        db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                        f"Découverte des interfaces : {exc}")
        return {}

    correspondance = {}
    for oid, nom in noms.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        vitesse_oid = f"{OID_IF_SPEED}.{index_snmp}"
        statut_oid = f"{OID_IF_OPER_STATUS}.{index_snmp}"
        vitesse = int(vitesses[vitesse_oid]) if vitesse_oid in vitesses else None
        code_statut = int(statuts[statut_oid]) if statut_oid in statuts else None
        statut = STATUT_INTERFACE_SNMP.get(code_statut, "INCONNU")
        interface = db.upsert_interface(equipement["id"], index_snmp, str(nom), vitesse, statut)
        if interface["visible"]:
            correspondance[index_snmp] = interface["id"]
    return correspondance


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
    """La façon de calculer un pourcentage de RAM dépend de la MIB disponible sur
    l'équipement (donc du modèle de supervision, donc de l'OID configuré pour "ram")."""
    if element["oid"] == OID_CISCO_MEM_POOL_USED:
        await _collecter_ram_cisco(equipement, element, timeout, retries)
    else:
        await _collecter_ram_host_resources(equipement, element, timeout, retries)


async def _collecter_ram_host_resources(equipement: dict, element, timeout: float, retries: int) -> None:
    """HOST-RESOURCES-MIB : il faut d'abord trouver la ligne de hrStorageTable dont le
    type est "mémoire vive", puis combiner hrStorageUsed et hrStorageSize."""
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


async def _collecter_ram_cisco(equipement: dict, element, timeout: float, retries: int) -> None:
    """CISCO-MEMORY-POOL-MIB (IOS ne supporte pas HOST-RESOURCES-MIB) : trouve le pool
    "Processor" (la mémoire système globale), puis combine ciscoMemoryPoolUsed et
    ciscoMemoryPoolFree."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    noms = await snmp_walk(host, community, OID_CISCO_MEM_POOL_NAME, timeout=timeout, retries=retries)
    index_pool = next(
        (oid.rsplit(".", 1)[-1] for oid, nom in noms.items() if str(nom) == CISCO_MEM_POOL_PRINCIPAL),
        None,
    )
    if index_pool is None:
        raise SnmpError(f"pool mémoire « {CISCO_MEM_POOL_PRINCIPAL} » non trouvé")

    oid_utilisee = f"{element['oid']}.{index_pool}"
    oid_libre = f"{OID_CISCO_MEM_POOL_FREE}.{index_pool}"
    valeurs = await snmp_get(host, community, [oid_utilisee, oid_libre], timeout=timeout, retries=retries)
    utilisee = float(valeurs[oid_utilisee])
    totale = utilisee + float(valeurs[oid_libre])
    if totale == 0:
        raise SnmpError("pool mémoire vide")
    db.enregistrer_mesure(equipement["id"], element["metrique_id"], utilisee / totale * 100)
