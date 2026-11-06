"""Service de collecte SNMP.

Pour chaque équipement actif, interroge les éléments surveillés de son modèle de
supervision et enregistre les mesures. Un échec sur un équipement (ou sur un indicateur
précis) n'empêche jamais la collecte du reste du parc : chaque équipement, et chaque
indicateur, est traité indépendamment.
"""
import asyncio
import re

from app import config, db
from app.snmp_client import OID_SYS_UPTIME, SnmpError, snmp_get, snmp_walk

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"  # ifHighSpeed (ifXTable), en Mb/s, sans le
                                                # plafond 32 bits (~4295 Mb/s) d'ifSpeed
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
OID_HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
OID_HR_STORAGE_ALLOC_UNITS = "1.3.6.1.2.1.25.2.3.1.4"  # taille en octets d'une unité d'allocation
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"     # valeur de hrStorageType pour la mémoire vive
HR_STORAGE_VOLUME = "1.3.6.1.2.1.25.2.1.4"  # valeur de hrStorageType pour un volume (type SNMP
                                             # "disque fixe", mais ce sont des points de montage,
                                             # pas des disques physiques : cf. commentaire schema.sql)

# hrStorageTable "disque fixe" remonte aussi des points de montage qui ne sont pas de vrais
# disques (pseudo-systèmes de fichiers comme /proc ou /tmp, montages internes à Docker...).
# On ne garde que la racine du système ("/") et les volumes de stockage façon NAS Synology
# ("/volume1", "/volume2"...) ; le reste n'est même pas inséré en base (cf. _collecter_volumes).
MOTIF_VOLUME_REEL = re.compile(r"^/$|^/volume(\d+)$", re.IGNORECASE)


def _nom_convivial(chemin: str) -> str:
    """Nom affiché à la place du chemin SNMP brut. Pour "/volumeN", reprend le numéro tel
    que Synology le nomme lui-même dans son interface (pas d'ambiguïté possible) ; "/" est
    le seul autre cas accepté par MOTIF_VOLUME_REEL."""
    correspondance = MOTIF_VOLUME_REEL.match(chemin)
    numero = correspondance.group(1) if correspondance else None
    return f"Volume {numero}" if numero else "Disque"

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

    # Réglé avant les seuils ci-dessous : le statut de connectivité (OPERATIONNEL) ne doit
    # jamais écraser un DEGRADE décidé par un dépassement de seuil pendant cette même passe.
    db.enregistrer_reussite_collecte(equipement["id"])

    interfaces = await _decouvrir_interfaces(equipement, timeout, retries)

    elements_actifs = db.lister_elements_actifs(equipement["modele_supervision_id"])
    for element in elements_actifs:
        try:
            if element["code"] == "cpu":
                await _collecter_cpu(equipement, element, timeout, retries)
            elif element["code"] == "ram":
                await _collecter_ram(equipement, element, timeout, retries)
            elif element["code"] == "volume":
                await _collecter_volumes(equipement, element, timeout, retries)
            elif element["par_interface"]:
                await _collecter_par_interface(equipement, element, interfaces, timeout, retries)
            else:
                await _collecter_scalaire(equipement, element, timeout, retries)
        except SnmpError as exc:
            db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                            f"{element['code']} : {exc}")
            db.enregistrer_mesure(equipement["id"], element["metrique_id"], None)

    # Taux d'utilisation des interfaces (Interface.tauxUtilisation du diagramme de classes) :
    # aucun OID à interroger, calculé à partir du trafic qu'on vient de collecter ci-dessus.
    codes_actifs = {element["code"] for element in elements_actifs}
    if "if_in_octets" in codes_actifs and "if_out_octets" in codes_actifs:
        _calculer_utilisation_interfaces(equipement, interfaces)


async def _decouvrir_interfaces(equipement: dict, timeout: float, retries: int) -> dict[int, int]:
    """Met à jour la table interface à partir de ifTable (nom/vitesse/statut, pour toutes
    les interfaces), et renvoie {ifIndex: interface_id} pour celles qui sont visibles
    seulement : une interface masquée garde son identité à jour, mais ne remonte plus de
    mesures de trafic (cf. _collecter_par_interface, qui se base sur cette correspondance)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    try:
        noms = await snmp_walk(host, community, OID_IF_DESCR, timeout=timeout, retries=retries)
        vitesses = await snmp_walk(host, community, OID_IF_SPEED, timeout=timeout, retries=retries)
        vitesses_hautes = await snmp_walk(host, community, OID_IF_HIGH_SPEED, timeout=timeout, retries=retries)
        statuts = await snmp_walk(host, community, OID_IF_OPER_STATUS, timeout=timeout, retries=retries)
    except SnmpError as exc:
        db.enregistrer_erreur_collecte(equipement["id"], "INDICATEUR_NON_SUPPORTE",
                                        f"Découverte des interfaces : {exc}")
        return {}

    correspondance = {}
    for oid, nom in noms.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        vitesse_oid = f"{OID_IF_SPEED}.{index_snmp}"
        vitesse_haute_oid = f"{OID_IF_HIGH_SPEED}.{index_snmp}"
        statut_oid = f"{OID_IF_OPER_STATUS}.{index_snmp}"
        # ifHighSpeed (Mb/s) est préféré dès qu'il est disponible : ifSpeed est un compteur
        # 32 bits qui plafonne à ~4295 Mb/s, donc faux pour toute interface plus rapide
        # (ex. 10 Gb/s). Repli sur ifSpeed seulement si l'équipement ne supporte pas ifXTable.
        vitesse_haute = int(vitesses_hautes[vitesse_haute_oid]) if vitesse_haute_oid in vitesses_hautes else 0
        if vitesse_haute > 0:
            vitesse = vitesse_haute * 1_000_000
        else:
            vitesse = int(vitesses[vitesse_oid]) if vitesse_oid in vitesses else None
        code_statut = int(statuts[statut_oid]) if statut_oid in statuts else None
        statut = STATUT_INTERFACE_SNMP.get(code_statut, "INCONNU")
        interface = db.upsert_interface(equipement["id"], index_snmp, str(nom), vitesse, statut)

        # Une interface DOWN ne dégrade l'équipement que si elle est supervisée ; si elle
        # est UP, INCONNU, ou n'est plus supervisée, on résout une éventuelle alerte en cours.
        if statut == "DOWN" and interface["visible"]:
            db.declencher_alerte_interface(equipement["id"], interface["id"], str(nom))
        else:
            db.resoudre_alerte_interface(equipement["id"], interface["id"])

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
    """Indicateur relevé une fois par interface (trafic entrant/sortant). Le débit (octets/s)
    se calcule à la lecture (cf. db.lister_debit) à partir de ces compteurs bruts, jamais
    stocké directement : on garde la valeur brute telle quelle, pour pouvoir recalculer plus
    tard. Si le compteur revient en arrière (remise à zéro de l'interface, ou dépassement de
    sa capacité), on le journalise sans pour autant renoncer à stocker le nouveau relevé brut."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_walk(host, community, element["oid"], timeout=timeout, retries=retries)
    if not valeurs:
        raise SnmpError("table vide ou non supportée")
    for oid, valeur in valeurs.items():
        index_snmp = int(oid.rsplit(".", 1)[-1])
        interface_id = interfaces.get(index_snmp)
        if interface_id is None:
            continue
        valeur = float(valeur)
        precedente = db.derniere_valeur_mesure(equipement["id"], element["metrique_id"], interface_id)
        if precedente is not None and valeur < precedente:
            db.enregistrer_erreur_collecte(
                equipement["id"], "VALEUR_ABERRANTE",
                f"{element['code']} (interface {index_snmp}) : compteur revenu en arrière "
                f"({precedente:.0f} -> {valeur:.0f}), débit non calculable sur ce point",
            )
        db.enregistrer_mesure(equipement["id"], element["metrique_id"], valeur, interface_id)


def _calculer_utilisation_interfaces(equipement: dict, interfaces: dict[int, int]) -> None:
    """Taux d'utilisation de chaque interface supervisée : le plus grand des deux débits
    (entrant/sortant) qu'on vient de collecter, rapporté à la capacité de l'interface
    (ifSpeed/ifHighSpeed, déjà connue via la table interface). Purement calculé à partir de
    données déjà en base : aucun appel SNMP supplémentaire, donc pas de SnmpError possible ici."""
    metrique = db.obtenir_metrique("utilisation_interface")
    metrique_in = db.obtenir_metrique("if_in_octets")
    metrique_out = db.obtenir_metrique("if_out_octets")
    seuil = db.obtenir_seuil_actif(metrique["id"])

    for interface_id in interfaces.values():
        interface = db.obtenir_interface(interface_id)
        if not interface["vitesse"]:
            continue  # capacité inconnue (ifSpeed absent) : taux non calculable

        debits = [
            db.debit_instantane(equipement["id"], metrique_in["id"], interface_id),
            db.debit_instantane(equipement["id"], metrique_out["id"], interface_id),
        ]
        debits = [debit for debit in debits if debit is not None]
        if not debits:
            continue  # pas encore deux relevés bruts consécutifs pour calculer un débit

        taux = max(debits) * 8 / interface["vitesse"] * 100
        db.enregistrer_mesure(equipement["id"], metrique["id"], taux, interface_id)

        if seuil is None:
            continue
        if _seuil_depasse(taux, seuil["operateur"], seuil["valeur_seuil"]):
            db.declencher_alerte_interface_utilisation(
                equipement["id"], interface_id, seuil["id"], taux, interface["nom"])
        else:
            db.resoudre_alerte_interface_utilisation(equipement["id"], interface_id, seuil["id"])


async def _collecter_cpu(equipement: dict, element, timeout: float, retries: int) -> None:
    """hrProcessorLoad : une valeur par cœur/processeur, on enregistre la moyenne."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_walk(host, community, element["oid"], timeout=timeout, retries=retries)
    if not valeurs:
        raise SnmpError("hrProcessorLoad non supporté par cet équipement")
    moyenne = sum(int(valeur) for valeur in valeurs.values()) / len(valeurs)
    db.enregistrer_mesure(equipement["id"], element["metrique_id"], moyenne)
    _verifier_seuil(equipement["id"], element["metrique_id"], moyenne)


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
    _verifier_seuil(equipement["id"], element["metrique_id"], pourcentage)


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
    pourcentage = utilisee / totale * 100
    db.enregistrer_mesure(equipement["id"], element["metrique_id"], pourcentage)
    _verifier_seuil(equipement["id"], element["metrique_id"], pourcentage)


async def _collecter_volumes(equipement: dict, element, timeout: float, retries: int) -> None:
    """hrStorageTable, type "disque fixe" (en réalité des points de montage) : on ne garde
    que ceux qui représentent un vrai disque (cf. MOTIF_VOLUME_REEL), chacun supervisable
    individuellement (comme les interfaces). Les autres ne sont même pas enregistrés en
    base. element['oid'] vaut hrStorageUsed, comme pour la RAM, mais on filtre ici sur un
    autre hrStorageType."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    types = await snmp_walk(host, community, OID_HR_STORAGE_TYPE, timeout=timeout, retries=retries)
    if not types:
        raise SnmpError("hrStorageTable non supporté par cet équipement")

    noms = await snmp_walk(host, community, OID_HR_STORAGE_DESCR, timeout=timeout, retries=retries)
    unites = await snmp_walk(host, community, OID_HR_STORAGE_ALLOC_UNITS, timeout=timeout, retries=retries)
    utilisees = await snmp_walk(host, community, element["oid"], timeout=timeout, retries=retries)
    tailles = await snmp_walk(host, community, OID_HR_STORAGE_SIZE, timeout=timeout, retries=retries)
    seuil = db.obtenir_seuil_actif(element["metrique_id"])

    for oid_type, type_valeur in types.items():
        if str(type_valeur) != HR_STORAGE_VOLUME:
            continue
        index_snmp = oid_type.rsplit(".", 1)[-1]
        chemin = str(noms.get(f"{OID_HR_STORAGE_DESCR}.{index_snmp}", ""))
        if not MOTIF_VOLUME_REEL.match(chemin):
            continue  # pas un vrai disque (point de montage interne, pseudo-FS...) : ignoré

        oid_utilisee = f"{element['oid']}.{index_snmp}"
        oid_totale = f"{OID_HR_STORAGE_SIZE}.{index_snmp}"
        oid_unite = f"{OID_HR_STORAGE_ALLOC_UNITS}.{index_snmp}"
        if oid_utilisee not in utilisees or oid_totale not in tailles:
            continue
        totale_unites = float(tailles[oid_totale])
        if totale_unites == 0:
            continue
        unite_octets = int(unites[oid_unite]) if oid_unite in unites else 1
        pourcentage = float(utilisees[oid_utilisee]) / totale_unites * 100
        taille_octets = int(totale_unites * unite_octets)
        utilise_octets = int(float(utilisees[oid_utilisee]) * unite_octets)
        nom = _nom_convivial(chemin)

        volume = db.upsert_volume(equipement["id"], int(index_snmp), nom,
                                   taille_octets, utilise_octets, pourcentage)
        if not volume["visible"]:
            db.resoudre_alerte_volume(equipement["id"], volume["id"])
        elif seuil and _seuil_depasse(pourcentage, seuil["operateur"], seuil["valeur_seuil"]):
            db.declencher_alerte_volume(equipement["id"], volume["id"], seuil["id"], pourcentage, nom)
        else:
            db.resoudre_alerte_volume(equipement["id"], volume["id"])


def _verifier_seuil(equipement_id: int, metrique_id: int, valeur: float) -> None:
    """Après un relevé réussi : déclenche ou résout l'alerte de dépassement de seuil de
    cette métrique pour cet équipement (le seuil lui-même est global, cf. db.py)."""
    seuil = db.obtenir_seuil_actif(metrique_id)
    if seuil is None:
        return
    if _seuil_depasse(valeur, seuil["operateur"], seuil["valeur_seuil"]):
        db.declencher_alerte_seuil(equipement_id, seuil["id"], metrique_id, valeur)
    else:
        db.resoudre_alerte_seuil(equipement_id, seuil["id"])


def _seuil_depasse(valeur: float, operateur: str, valeur_seuil: float) -> bool:
    if operateur == "SUPERIEUR":
        return valeur > valeur_seuil
    if operateur == "SUPERIEUR_OU_EGAL":
        return valeur >= valeur_seuil
    if operateur == "INFERIEUR":
        return valeur < valeur_seuil
    return valeur <= valeur_seuil  # INFERIEUR_OU_EGAL
