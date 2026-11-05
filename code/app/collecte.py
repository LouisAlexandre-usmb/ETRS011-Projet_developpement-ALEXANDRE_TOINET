"""Service de collecte SNMP.

Pour chaque équipement actif, interroge les éléments surveillés de son modèle de
supervision et enregistre les mesures. Un échec sur un équipement (ou sur un indicateur
précis) n'empêche jamais la collecte du reste du parc : chaque équipement, et chaque
indicateur, est traité indépendamment. Un équipement passe INDISPONIBLE après plusieurs échecs
consécutifs, pas dès le premier timeout, pour éviter les fausses alertes.
"""
import asyncio
import logging
import math
import sys
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import config, db
from app.snmp_client import OID_SYS_UPTIME, SnmpError, snmp_get, snmp_walk

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"  # en Mb/s, pour les liens au-delà de 4,29 Gb/s
VITESSE_SATUREE = 4294967295                    # maximum de ifSpeed, qui ne dit plus rien
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"  # valeur de hrStorageType qui désigne la mémoire vive

OID_CISCO_MEM_POOL_NAME = "1.3.6.1.4.1.9.9.48.1.1.1.2"
OID_CISCO_MEM_POOL_USED = "1.3.6.1.4.1.9.9.48.1.1.1.5"
OID_CISCO_MEM_POOL_FREE = "1.3.6.1.4.1.9.9.48.1.1.1.6"
OID_UCD_CPU_IDLE = "1.3.6.1.4.1.2021.11.11.0"
OID_UCD_MEM_TOTAL = "1.3.6.1.4.1.2021.4.5.0"
OID_UCD_MEM_DISPONIBLE = "1.3.6.1.4.1.2021.4.6.0"
CISCO_MEM_POOL_PRINCIPAL = "Processor"  # pool qui représente la mémoire système globale

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


async def ping(host: str, timeout: float) -> bool | None:
    """Teste si l'équipement répond au ping ; None si la commande ping n'est pas disponible."""
    if host.startswith("-"):  # ne doit pas être lu comme une option de la commande
        return False
    delai = max(1, math.ceil(timeout))
    option_delai = "-t" if sys.platform == "darwin" else "-W"
    try:
        processus = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", option_delai, str(delai), host,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        return await asyncio.wait_for(processus.wait(), delai + 2) == 0
    except TimeoutError:
        processus.kill()
        await processus.wait()
        return False


async def _traiter_echec_snmp(equipement: dict, parametres, exc: Exception, timeout: float) -> None:
    """L'agent SNMP ne répond pas : le ping distingue un équipement en panne d'un service en erreur."""
    id_equipement = equipement["id"]
    seuil = parametres["nb_echecs_avant_indisponibilite"]
    joignable = await ping(equipement["adresse"], timeout)
    if joignable:
        categorie, code, statut = "SERVICE", "SNMP_EN_ECHEC", "DEGRADE"
        message = "Répond au ping mais pas en SNMP (agent arrêté, communauté incorrecte ou filtrage)"
    else:
        categorie, code, statut = "EQUIPEMENT", "INJOIGNABLE", "INDISPONIBLE"
        message = ("Ne répond ni au ping ni en SNMP" if joignable is False
                   else "Ne répond pas en SNMP (ping non disponible sur ce poste)")

    db.enregistrer_erreur_collecte(id_equipement, "INJOIGNABLE", f"{message} : {exc}")
    # Un relevé vide par indicateur : l'interruption apparaît comme un trou dans les courbes
    interfaces = db.interfaces_a_mesurer(id_equipement)
    for element in db.lister_elements_actifs(equipement["modele_supervision_id"]):
        for interface_id in (interfaces.values() if element["par_interface"] else [None]):
            db.enregistrer_mesure(id_equipement, element["metrique_id"], None, interface_id)

    echecs = db.enregistrer_echec_collecte(id_equipement, seuil, statut)
    if echecs >= seuil:
        db.cloturer_alertes_sauf(id_equipement, code)
        db.ouvrir_alerte(id_equipement, categorie, code, message)
    logger.warning(
        "Collecte en échec sur %s (%s), échec consécutif n° %d, alerte à partir de %d : %s",
        equipement["nom"], equipement["adresse"], echecs, seuil, message,
    )


def _appliquer_seuil(equipement: dict, element, valeur: float | None, seuils: dict) -> None:
    """Ouvre ou clôture l'alerte de seuil d'un indicateur selon sa dernière valeur."""
    seuil = seuils.get(element["metrique_id"])
    if seuil is not None and valeur is not None and valeur > seuil["valeur"]:
        critique = valeur > seuil["valeur_critique"]
        limite = seuil["valeur_critique"] if critique else seuil["valeur"]
        db.ouvrir_alerte(
            equipement["id"], "SERVICE", "SEUIL_DEPASSE",
            f"{seuil['libelle']} à {valeur:.1f} % (seuil {'critique' if critique else 'd’avertissement'} : {limite:g} %)",
            element["metrique_id"], "CRITIQUE" if critique else "AVERTISSEMENT",
        )
    else:
        db.cloturer_alerte(equipement["id"], "SEUIL_DEPASSE", element["metrique_id"])


async def collecter_equipement(equipement: dict) -> None:
    """Collecte un équipement : test de connectivité, puis chaque indicateur de son modèle."""
    parametres = db.obtenir_parametres_generaux()
    timeout, retries = parametres["delai_reponse_snmp"], config.SNMP_RETRIES
    options = {"timeout": timeout, "retries": retries, "version": equipement["version_snmp"]}
    id_equipement = equipement["id"]

    try:
        await snmp_get(equipement["adresse"], equipement["communaute_snmp"], [OID_SYS_UPTIME], **options)
    except SnmpError as exc:
        await _traiter_echec_snmp(equipement, parametres, exc, timeout)
        return

    db.cloturer_alerte(id_equipement, "INJOIGNABLE")
    db.cloturer_alerte(id_equipement, "SNMP_EN_ECHEC")
    await decouvrir_interfaces(equipement, timeout, retries)
    interfaces = db.interfaces_a_mesurer(id_equipement)
    seuils = {seuil["metrique_id"]: seuil for seuil in db.seuils_de_l_equipement(id_equipement) if seuil["actif"]}

    for element in db.lister_elements_actifs(equipement["modele_supervision_id"]):
        metrique_id = element["metrique_id"]
        try:
            if element["code"] == "cpu":
                valeur = await _collecter_cpu(equipement, element, options)
            elif element["code"] == "ram":
                valeur = await _collecter_ram(equipement, element, options)
            elif element["par_interface"]:
                valeur = await _collecter_par_interface(equipement, element, interfaces, options)
            else:
                valeur = await _collecter_scalaire(equipement, element, options)
            if valeur is not None:  # les indicateurs par interface enregistrent eux-mêmes leurs relevés
                db.enregistrer_mesure(id_equipement, metrique_id, valeur)
            db.cloturer_alerte(id_equipement, "RELEVE_EN_ECHEC", metrique_id)
            _appliquer_seuil(equipement, element, valeur, seuils)
        except (SnmpError, ValueError, TypeError) as exc:
            # ValueError/TypeError : l'agent répond « OID absent » à la place d'un nombre
            detail = exc if isinstance(exc, SnmpError) else "OID absent ou valeur non numérique"
            db.enregistrer_erreur_collecte(id_equipement, "INDICATEUR_NON_SUPPORTE",
                                            f"{element['code']} : {detail}")
            db.enregistrer_mesure(id_equipement, metrique_id, None)
            if db.releve_en_echec_persistant(id_equipement, metrique_id,
                                              parametres["nb_echecs_avant_indisponibilite"]):
                db.ouvrir_alerte(id_equipement, "SERVICE", "RELEVE_EN_ECHEC",
                                 f"Relevé SNMP en échec : {element['code']} ({detail})", metrique_id)

    statut = "DEGRADE" if db.a_alerte_service_active(id_equipement) else "OPERATIONNEL"
    db.enregistrer_reussite_collecte(id_equipement, statut)
    if equipement["statut"] == "INDISPONIBLE":
        logger.info("%s (%s) est de nouveau joignable", equipement["nom"], equipement["adresse"])


async def decouvrir_interfaces(equipement: dict, timeout: float, retries: int) -> bool:
    """Met à jour la table interface à partir de ifTable ; renvoie False si la lecture échoue."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    options = {"timeout": timeout, "retries": retries, "version": equipement["version_snmp"]}
    try:
        noms = await snmp_walk(host, community, OID_IF_DESCR, **options)
        vitesses = await snmp_walk(host, community, OID_IF_SPEED, **options)
        statuts = await snmp_walk(host, community, OID_IF_OPER_STATUS, **options)
    except SnmpError as exc:
        db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                        f"Découverte des interfaces : {exc}")
        return False

    vitesses_hautes = {}
    if any(int(vitesse) == VITESSE_SATUREE for vitesse in vitesses.values()):
        try:
            vitesses_hautes = await snmp_walk(host, community, OID_IF_HIGH_SPEED, **options)
        except SnmpError:
            pass  # table absente (SNMP v1 par exemple) : la vitesse reste inconnue

    index_declares = []
    for oid, nom in noms.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        vitesse_oid = f"{OID_IF_SPEED}.{index_snmp}"
        statut_oid = f"{OID_IF_OPER_STATUS}.{index_snmp}"
        vitesse = int(vitesses[vitesse_oid]) if vitesse_oid in vitesses else None
        if vitesse == VITESSE_SATUREE:
            vitesse_haute = vitesses_hautes.get(f"{OID_IF_HIGH_SPEED}.{index_snmp}")
            vitesse = int(vitesse_haute) * 1_000_000 if vitesse_haute is not None else None
        code_statut = int(statuts[statut_oid]) if statut_oid in statuts else None
        statut = STATUT_INTERFACE_SNMP.get(code_statut, "INCONNU")
        db.upsert_interface(equipement["id"], index_snmp, str(nom), vitesse, statut)
        index_declares.append(index_snmp)
    if index_declares:  # une réponse vide ne prouve pas que toutes les interfaces ont disparu
        db.marquer_interfaces_absentes(equipement["id"], index_declares)
    return True


async def _collecter_scalaire(equipement: dict, element, options: dict) -> float:
    """Indicateur qui existe en un seul exemplaire sur l'équipement (ex. uptime)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_get(host, community, [element["oid"]], **options)
    return float(valeurs[element["oid"]])


async def _collecter_par_interface(equipement: dict, element, interfaces: dict[int, int],
                                    options: dict) -> None:
    """Indicateur relevé une fois par interface (trafic entrant/sortant)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_walk(host, community, element["oid"], **options)
    if not valeurs:
        raise SnmpError("table vide ou non supportée")
    for oid, valeur in valeurs.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        interface_id = interfaces.get(index_snmp)
        if interface_id is not None:
            db.enregistrer_mesure(equipement["id"], element["metrique_id"], float(valeur), interface_id)


async def _collecter_cpu(equipement: dict, element, options: dict) -> float:
    """Charge CPU en %. UCD-SNMP donne le temps d'inactivité ; les autres MIB une charge par
    cœur/processeur, dont on garde la moyenne."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    if element["oid"] == OID_UCD_CPU_IDLE:
        valeurs = await snmp_get(host, community, [OID_UCD_CPU_IDLE], **options)
        return 100 - float(valeurs[OID_UCD_CPU_IDLE])

    valeurs = await snmp_walk(host, community, element["oid"], **options)
    if not valeurs:
        raise SnmpError("charge CPU non supportée par cet équipement")
    return sum(int(valeur) for valeur in valeurs.values()) / len(valeurs)


async def _collecter_ram(equipement: dict, element, options: dict) -> float:
    """La façon de calculer un pourcentage de RAM dépend de la MIB disponible sur
    l'équipement (donc du modèle de supervision, donc de l'OID configuré pour "ram")."""
    if element["oid"] == OID_CISCO_MEM_POOL_USED:
        return await _collecter_ram_cisco(equipement, element, options)
    if element["oid"] == OID_UCD_MEM_TOTAL:
        return await _collecter_ram_ucd(equipement, options)
    return await _collecter_ram_host_resources(equipement, element, options)


async def _collecter_ram_ucd(equipement: dict, options: dict) -> float:
    """UCD-SNMP-MIB : mémoire totale et mémoire disponible."""
    valeurs = await snmp_get(equipement["adresse"], equipement["communaute_snmp"],
                             [OID_UCD_MEM_TOTAL, OID_UCD_MEM_DISPONIBLE], **options)
    totale = float(valeurs[OID_UCD_MEM_TOTAL])
    if totale == 0:
        raise SnmpError("memTotalReal = 0")
    return (totale - float(valeurs[OID_UCD_MEM_DISPONIBLE])) / totale * 100


async def _collecter_ram_host_resources(equipement: dict, element, options: dict) -> float:
    """HOST-RESOURCES-MIB : il faut d'abord trouver la ligne de hrStorageTable dont le
    type est "mémoire vive", puis combiner hrStorageUsed et hrStorageSize."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    types = await snmp_walk(host, community, OID_HR_STORAGE_TYPE, **options)
    index_ram = next(
        (oid.rsplit(".", 1)[-1] for oid, type_oid in types.items() if str(type_oid) == HR_STORAGE_RAM),
        None,
    )
    if index_ram is None:
        raise SnmpError("hrStorageType « mémoire vive » non trouvé")

    oid_utilisee = f"{element['oid']}.{index_ram}"
    oid_totale = f"{OID_HR_STORAGE_SIZE}.{index_ram}"
    valeurs = await snmp_get(host, community, [oid_utilisee, oid_totale], **options)
    totale = float(valeurs[oid_totale])
    if totale == 0:
        raise SnmpError("hrStorageSize = 0")
    return float(valeurs[oid_utilisee]) / totale * 100


async def _collecter_ram_cisco(equipement: dict, element, options: dict) -> float:
    """CISCO-MEMORY-POOL-MIB (IOS ne supporte pas HOST-RESOURCES-MIB) : trouve le pool
    "Processor" (la mémoire système globale), puis combine ciscoMemoryPoolUsed et
    ciscoMemoryPoolFree."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    noms = await snmp_walk(host, community, OID_CISCO_MEM_POOL_NAME, **options)
    index_pool = next(
        (oid.rsplit(".", 1)[-1] for oid, nom in noms.items() if str(nom) == CISCO_MEM_POOL_PRINCIPAL),
        None,
    )
    if index_pool is None:
        raise SnmpError(f"pool mémoire « {CISCO_MEM_POOL_PRINCIPAL} » non trouvé")

    oid_utilisee = f"{element['oid']}.{index_pool}"
    oid_libre = f"{OID_CISCO_MEM_POOL_FREE}.{index_pool}"
    valeurs = await snmp_get(host, community, [oid_utilisee, oid_libre], **options)
    utilisee = float(valeurs[oid_utilisee])
    totale = utilisee + float(valeurs[oid_libre])
    if totale == 0:
        raise SnmpError("pool mémoire vide")
    return utilisee / totale * 100
