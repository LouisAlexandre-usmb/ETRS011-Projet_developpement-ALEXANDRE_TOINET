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
import re
import sqlite3
import sys
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import config, db, detection, internet, metriques, seuils
from app.snmp_client import OID_SYS_UPTIME, SnmpError, snmp_get, snmp_walk

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"  # en Mb/s, pour les liens au-delà de 4,29 Gb/s
VITESSE_SATUREE = 4294967295                    # maximum de ifSpeed, qui ne dit plus rien
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
OID_HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
OID_HR_STORAGE_UNITES = "1.3.6.1.2.1.25.2.3.1.4"   # hrStorageAllocationUnits : octets par unité
HR_STORAGE_DISQUE = "1.3.6.1.2.1.25.2.1.4"  # valeur de hrStorageType qui désigne un disque fixe
HR_STORAGE_RAM = "1.3.6.1.2.1.25.2.1.2"  # valeur de hrStorageType qui désigne la mémoire vive
# Lignes de hrStorageTable qu'un agent Linux (Net-SNMP) compte dans la mémoire utilisée, mais que le
# système libère à la demande : on les retire, sinon un NAS paraît plein à 90 %
MEMOIRE_RECUPERABLE = {"Cached memory", "Memory buffers"}

OID_UCD_CPU_IDLE = "1.3.6.1.4.1.2021.11.11.0"
OID_UCD_MEM_TOTAL = "1.3.6.1.4.1.2021.4.5.0"
OID_UCD_MEM_DISPONIBLE = "1.3.6.1.4.1.2021.4.6.0"

# ifOperStatus : up(1) ; down(2), notPresent(6) et lowerLayerDown(7) sont des pannes ; testing, unknown
# et dormant restent INCONNU (ni alerte, ni retour à la normale)
STATUT_INTERFACE_SNMP = {1: "UP", 2: "DOWN", 6: "DOWN", 7: "DOWN"}

# Préfixes d'interfaces virtuelles courantes, non proposées par défaut (l'admin garde le choix)
PREFIXES_VIRTUELS = ("docker", "veth", "br-", "tap", "sit", "lo", "ovs-system")

# Disques fixes qui ne reflètent pas la capacité utile : systèmes virtuels, conteneurs, images en lecture seule
DISQUES_IGNORES = re.compile(r"^(/dev|/run|/sys|/proc|/snap|/boot|/var/lib/docker)(/|$)|/@docker(/|$)")
VOLUME_NAS = re.compile(r"^(/volume\d+|/share/[^/]+)$")  # sur un NAS, seuls les volumes de données comptent

# Objets détectés : leur relevé parcourt des tables entières, donc moins souvent que la collecte
PERIODE_OBJETS = 300            # secondes
MAX_RELEVES_SIMULTANES = 3
MAX_ECHECS_CONSECUTIFS = 3      # l'équipement ne répond plus : inutile d'attendre sur chaque MIB
# Les objets sont relevés en arrière-plan : on peut attendre davantage qu'à la collecte. Un NAS dont les
# disques sont en veille met plusieurs secondes à répondre sur leur table (Synology : 2,5 s par disque)
DELAI_OBJETS = 10               # secondes par requête
MAX_INSTANCES_GRAPHIQUE = 16    # au-delà (table SMART...), seules les dernières valeurs sont gardées
MAX_OBJETS_GRAPHIQUE = 30       # par équipement : l'historique doit rester raisonnable en base
SYNTAXES_NOMBRE = re.compile(r"integer|gauge|unsigned", re.I)
SYNTAXES_COMPTEUR = re.compile(r"counter", re.I)
OBJET_INDEX = re.compile(r"index$", re.I)

logger = logging.getLogger(__name__)

# Fréquence à laquelle on cherche les équipements à interroger (et non l'intervalle de collecte) :
# un équipement ajouté reçoit ainsi sa première collecte en quelques secondes.
PERIODE_VERIFICATION = 5   # secondes

_planificateur: AsyncIOScheduler | None = None
# Échéances gardées en mémoire : derniere_collecte n'est mise à jour qu'en cas de réussite, elle
# ferait réinterroger un équipement en panne à chaque vérification au lieu de suivre son intervalle.
# Clé (id équipement, id métrique) pour un service ; (id équipement, None) pour l'équipement lui-même.
_prochaine_collecte: dict[tuple[int, int | None], float] = {}
_en_cours: dict[int, asyncio.Task] = {}      # id équipement -> collecte en cours


def demarrer() -> None:
    """Démarre le planificateur (à appeler depuis la boucle asyncio de l'application)."""
    global _planificateur
    _planificateur = AsyncIOScheduler()
    _planificateur.add_job(
        verifier_echeances, "interval", seconds=PERIODE_VERIFICATION, max_instances=1, coalesce=True,
    )
    _planificateur.add_job(db.purger_donnees_anciennes, "interval", hours=24)
    _planificateur.add_job(internet.verifier_internet, "interval", minutes=5)
    _planificateur.add_job(relever_objets, "interval", seconds=PERIODE_OBJETS, max_instances=1, coalesce=True)
    _planificateur.start()
    a_identifier = db.equipements_a_identifier()
    if a_identifier:
        lancer_identification(a_identifier)


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


def oublier_echeances(id_equipement: int | None = None) -> None:
    """Après un changement d'intervalle : la prochaine vérification relance la collecte, au lieu
    d'attendre la fin de l'ancien intervalle."""
    for cle in list(_prochaine_collecte):
        if id_equipement is None or cle[0] == id_equipement:
            del _prochaine_collecte[cle]


async def verifier_echeances() -> None:
    """Appelée périodiquement par le planificateur : démarre, chacune dans sa propre tâche, les
    collectes dont l'intervalle de polling est écoulé (celui de l'équipement, sinon la valeur
    globale). Elle n'attend pas leur fin : un équipement lent ne retarde donc
    pas les autres."""
    parametres = db.obtenir_parametres_generaux()
    maintenant = time.monotonic()
    for equipement in db.lister_equipements_actifs():
        id_equipement = equipement["id"]
        if id_equipement in _en_cours:
            continue
        intervalle = equipement["intervalle_polling"] or parametres["intervalle_polling_defaut"]
        services_dus = set()
        for service in db.services_de_l_equipement(id_equipement):
            cle = (id_equipement, service["metrique_id"])
            if maintenant >= _prochaine_collecte.get(cle, 0):
                services_dus.add(service["metrique_id"])
                _prochaine_collecte[cle] = maintenant + intervalle
        # La disponibilité suit l'intervalle de l'équipement, même si ses services sont espacés
        equipement_du = maintenant >= _prochaine_collecte.get((id_equipement, None), 0)
        if equipement_du:
            _prochaine_collecte[(id_equipement, None)] = maintenant + intervalle
        if not services_dus and not equipement_du:
            continue
        tache = asyncio.create_task(_collecter_sans_interrompre(equipement, services_dus))
        _en_cours[id_equipement] = tache
        tache.add_done_callback(lambda _, cle=id_equipement: _en_cours.pop(cle, None))


async def _collecter_sans_interrompre(equipement: dict, services: set[int] | None = None) -> None:
    """Collecte un équipement ; une erreur imprévue est journalisée sans toucher aux autres."""
    if verrou(equipement["id"]).locked():
        # Relevé des capteurs ou identification en cours : l'agent est peut-être occupé (NAS qui
        # réveille ses disques). La collecte attend le cycle suivant plutôt que de compter un faux échec.
        logger.info("Collecte de %s reportée : relevé des capteurs en cours", equipement["nom"])
        return
    try:
        await collecter_equipement(equipement, services)
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


async def _traiter_echec_snmp(equipement: dict, parametres, exc: Exception, timeout: float,
                              services: set[int] | None) -> None:
    """L'agent SNMP ne répond pas : le ping distingue un équipement en panne d'un service en erreur."""
    id_equipement = equipement["id"]
    seuil = equipement["nb_echecs_alerte"] or parametres["nb_echecs_avant_indisponibilite"]
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
        if services is not None and element["metrique_id"] not in services:
            continue
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


def cibles_de_seuil(id_equipement: int) -> list[dict]:
    """Services gardés d'un équipement qui peuvent recevoir un seuil : ses mesures (CPU, RAM, disque,
    uptime, trafic), puis ses objets détectés (capteurs, états). Chacun avec son profil (unité,
    condition proposée) et son seuil : celui réglé sur l'équipement, sinon la valeur par défaut
    (CPU, RAM, disque), sinon aucun."""
    enregistres = db.seuils_enregistres(id_equipement)
    defauts = db.seuils_par_defaut()
    cibles = []
    for metrique in db.metriques_du_modele(id_equipement):
        profil = seuils.profil_metrique(metrique["code"])
        if not metrique["garde"] or profil is None:
            continue
        propre, defaut = enregistres.get(("m", metrique["metrique_id"])), defauts.get(metrique["metrique_id"])
        if propre is not None:
            seuil = {**dict(propre), "origine": "propre"}
        elif defaut is not None:
            seuil = {"operateur": ">", "avertissement": defaut["valeur"], "critique": defaut["valeur_critique"],
                     "actif": 1, "origine": "defaut"}
        else:
            seuil = None
        cibles.append({"cle": f"m{metrique['metrique_id']}", "metrique_id": metrique["metrique_id"], "objet_id": None,
                       "code": metrique["code"], "libelle": metrique["libelle"], "groupe": "Mesures",
                       "profil": profil, "seuil": seuil, "a_defaut": defaut is not None})
    for objet in db.lister_objets_detectes(id_equipement):
        profil = seuils.profil_objet(objet)
        if not objet["garde"] or profil is None:
            continue
        propre = enregistres.get(("o", objet["id"]))
        groupe = objet["table_nom"] if objet["table_nom"] and objet["table_nom"] != objet["objet"] else "Valeurs simples"
        cibles.append({"cle": f"o{objet['id']}", "metrique_id": None, "objet_id": objet["id"], "code": None,
                       "libelle": objet["objet"], "groupe": f"{groupe} · {objet['mib']}", "profil": profil,
                       "seuil": {**dict(propre), "origine": "propre"} if propre is not None else None,
                       "a_defaut": False})
    return cibles


def _valeurs_de_la_cible(id_equipement: int, cible: dict, valeurs_objets: dict) -> list[tuple[str | None, float | None]]:
    """Valeurs actuelles d'un service, dans l'unité de son seuil : [(instance, valeur)]."""
    if cible["objet_id"] is not None:
        return [(instance, seuils.nombre(valeur)) for instance, valeur in valeurs_objets.get(cible["objet_id"], {}).items()]
    if cible["code"] in ("if_in_octets", "if_out_octets"):
        resultats = []
        for interface in db.lister_interfaces(id_equipement):
            if interface["suivi"] != "OUI" or not interface["presente"]:
                continue
            points = metriques.calculer_debit(
                db.deux_dernieres_mesures(id_equipement, cible["metrique_id"], interface["id"]), interface["vitesse"])
            debit = points[-1]["valeur"] if points else None
            resultats.append((interface["nom"], debit / 1_000_000 if debit is not None else None))
        return resultats
    return [(None, seuils.convertir_metrique(cible["code"], db.derniere_valeur(id_equipement, cible["metrique_id"])))]


def valeurs_actuelles(id_equipement: int, cibles: list[dict]) -> dict[str, str]:
    """Valeur actuelle de chaque service, lisible : « 42 °C », ou « 24 à 27 °C (4) » pour plusieurs instances."""
    valeurs_objets: dict[int, dict[str, str]] = {}
    for ligne in db.lister_valeurs_objets(id_equipement):
        valeurs_objets.setdefault(ligne["objet_id"], {})[ligne["instance"]] = ligne["valeur"]
    textes = {}
    for cible in cibles:
        nombres = [v for _, v in _valeurs_de_la_cible(id_equipement, cible, valeurs_objets) if v is not None]
        profil = cible["profil"]
        if not nombres:
            textes[cible["cle"]] = "—"
        elif len(nombres) == 1 or profil["nature"] == "ETAT" and len(set(nombres)) == 1:
            textes[cible["cle"]] = seuils.formater(nombres[0], profil) + (f" ({len(nombres)})" if len(nombres) > 1 else "")
        elif profil["nature"] == "ETAT":
            textes[cible["cle"]] = ", ".join(sorted({seuils.formater(v, profil) for v in nombres})) + f" ({len(nombres)})"
        else:
            textes[cible["cle"]] = (f"{seuils.formater(min(nombres), profil)} à {seuils.formater(max(nombres), profil)}"
                                    f" ({len(nombres)})")
    return textes


def reevaluer_seuils(id_equipement: int) -> None:
    """Compare les dernières valeurs de chaque service gardé à son seuil, ouvre ou actualise les
    alertes, et clôt celles des services sans seuil actif ou qui ne sont plus gardés. Appelé après
    chaque collecte, chaque relevé des objets et chaque changement de seuil ou de choix des services."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None or equipement["statut"] == "INDISPONIBLE":
        return  # aucune mesure récente à comparer
    valeurs_objets: dict[int, dict[str, str]] = {}
    for ligne in db.lister_valeurs_objets(id_equipement):
        valeurs_objets.setdefault(ligne["objet_id"], {})[ligne["instance"]] = ligne["valeur"]
    en_alerte = set()
    for cible in cibles_de_seuil(id_equipement):
        seuil = cible["seuil"]
        if not seuil or not seuil["actif"]:
            continue
        depassements = []
        for instance, valeur in _valeurs_de_la_cible(id_equipement, cible, valeurs_objets):
            niveau = seuils.niveau(valeur, seuil)
            if niveau:
                depassements.append((niveau, instance, valeur))
        if not depassements:
            continue
        critique = any(niveau == "CRITIQUE" for niveau, *_ in depassements)
        limite = seuil["critique"] if critique else seuil["avertissement"]
        profil = cible["profil"]
        detail = "; ".join(f"{instance + ' : ' if instance else ''}{seuils.formater(valeur, profil)}"
                           for niveau, instance, valeur in depassements[:3])
        if len(depassements) > 3:
            detail += f" (+{len(depassements) - 3})"
        message = (f"{cible['libelle']} : {detail} (seuil {'critique' if critique else 'd’avertissement'} : "
                   f"{seuils.OPERATEURS[seuil['operateur']]} {seuils.formater(limite, profil)})")
        db.ouvrir_alerte(id_equipement, "SERVICE", "SEUIL_DEPASSE", message, cible["metrique_id"],
                         "CRITIQUE" if critique else "AVERTISSEMENT", cible["objet_id"])
        en_alerte.add((cible["metrique_id"], cible["objet_id"]))
    for alerte in db.lister_alertes(statut="ACTIVE", equipement_id=id_equipement):
        if alerte["code"] == "SEUIL_DEPASSE" and (alerte["metrique_id"], alerte["objet_id"]) not in en_alerte:
            db.cloturer_alerte(id_equipement, "SEUIL_DEPASSE", alerte["metrique_id"], alerte["objet_id"])
    db.actualiser_statut_service(id_equipement)


class Releve(float):
    """Pourcentage de RAM ou de disque, accompagné des tailles en octets (et du volume concerné)."""

    def __new__(cls, pourcentage: float, utilise: float | None = None, total: float | None = None,
                detail: str | None = None, volumes: list[tuple] | None = None):
        releve = super().__new__(cls, pourcentage)
        releve.utilise, releve.total, releve.detail = utilise, total, detail
        releve.volumes = volumes or []   # disque : (nom, %, octets utilisés, octets au total) de chaque volume
        return releve


def _nature_objet(objet: dict) -> str:
    if OBJET_INDEX.search(objet["nom"]) or not (SYNTAXES_NOMBRE.search(objet["syntaxe"]) or SYNTAXES_COMPTEUR.search(objet["syntaxe"])):
        return "TEXTE"
    if SYNTAXES_COMPTEUR.search(objet["syntaxe"]):
        return "COMPTEUR"
    return "ETAT" if objet["etats"] else "MESURE"


def _valeur_lisible(valeur) -> str:
    texte = " ".join(str(valeur).split())
    return "".join(caractere if caractere.isprintable() else "·" for caractere in texte)[:120]


def _instances_par_objet(mib: dict, valeurs: dict) -> dict[str, dict[str, object]]:
    """Associe les valeurs renvoyées par l'équipement aux objets de la MIB : {objet: {instance: valeur}}."""
    par_oid = {objet["oid"]: objet["nom"] for objet in mib["objets"]}
    resultat: dict[str, dict[str, object]] = {}
    for oid, valeur in valeurs.items():
        racine = oid
        while "." in racine:
            racine = racine.rsplit(".", 1)[0]
            if racine in par_oid:
                resultat.setdefault(par_oid[racine], {})[oid[len(racine) + 1:]] = valeur
                break
    return resultat


VALEURS_ABSENTES = {"NoSuchObject", "NoSuchInstance", "EndOfMibView"}
LOT_GET = 20   # OID par requête GET


async def _valeurs_simples_manquantes(equipement, mib: dict, valeurs: dict, options: dict) -> dict:
    """Certains agents (NAS QNAP...) sautent des valeurs simples lors d'un parcours, mais y répondent
    quand on les demande : pour une MIB présente, on les demande donc directement, par lots."""
    presentes = {oid.rsplit(".", 1)[0] for oid in valeurs}
    manquants = [f"{objet['oid']}.0" for objet in mib["objets"] if objet["type"] == "scalar" and objet["oid"] not in presentes]
    trouvees = {}
    for debut in range(0, len(manquants), LOT_GET):
        try:
            reponse = await snmp_get(equipement["adresse"], equipement["communaute_snmp"],
                                     manquants[debut:debut + LOT_GET], **options)
        except SnmpError:
            continue
        trouvees.update({oid: valeur for oid, valeur in reponse.items() if type(valeur).__name__ not in VALEURS_ABSENTES})
    return trouvees


def branches_mib(mib: dict) -> list[str]:
    """Branches à parcourir pour un groupe d'objets : son OID de base s'il est connu (modèles
    intégrés), sinon l'OID de chaque objet. Une colonne ne renvoie ainsi que ses
    lignes, sans le reste de la table ni de la branche du constructeur (sur un NAS, la table SMART
    réveillerait chaque disque)."""
    if mib.get("base"):
        return [mib["base"]]
    oids = sorted({objet["oid"] for objet in mib["objets"]}, key=lambda o: [int(p) for p in o.split(".")])
    return [oid for oid in oids if not any(oid.startswith(autre + ".") for autre in oids)]


TAILLE_TEXTE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)i?B?\s*$", re.I)
PUISSANCES = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}


def _taille(valeur) -> float | None:
    """Nombre, ou taille écrite en texte avec son unité (« 3.8 GB », comme sur les NAS QNAP)."""
    nombre = _nombre(valeur)
    if nombre is not None:
        return nombre
    correspondance = TAILLE_TEXTE.match(str(valeur))
    if correspondance is None:
        return None
    return float(correspondance.group(1)) * 1024 ** PUISSANCES[correspondance.group(2).upper()]


def _nombre(valeur) -> float | None:
    try:
        return float(str(valeur))
    except ValueError:
        return None


async def detecter_objets(equipement: dict) -> dict[str, list[dict]]:
    """Interroge l'équipement sur les objets des modèles intégrés (capteurs génériques et ceux de son constructeur) et
    enregistre ce qui répond, avec les valeurs obtenues. Un groupe auquel il ne répond pas est
    simplement ignoré. Renvoie, par modèle, les objets détectés et leurs valeurs :
    {modèle: [{"objet": ..., "instances": ...}]}."""
    # Une seule tentative : une lecture qui expire sera retentée au relevé suivant, et pendant ce temps
    # la collecte principale de l'équipement attend
    options = {"timeout": max(config.SNMP_TIMEOUT, DELAI_OBJETS), "retries": 0, "version": equipement["version_snmp"]}
    detections: dict[str, list[dict]] = {}
    echecs_consecutifs = 0
    # Une branche n'est parcourue qu'une fois par relevé, même si plusieurs groupes de capteurs la
    # partagent : sur un NAS, chaque parcours de la table des disques les réveille
    lues: dict[str, dict | SnmpError] = {}

    async def lire_branche(base: str) -> dict:
        englobante = next((b for b in lues if base == b or base.startswith(b + ".")), None)
        if englobante is None:
            try:
                lues[base] = await snmp_walk(equipement["adresse"], equipement["communaute_snmp"], base, **options)
            except SnmpError as exc:
                lues[base] = exc
            englobante = base
        resultat = lues[englobante]
        if isinstance(resultat, SnmpError):
            raise resultat
        return {oid: v for oid, v in resultat.items() if oid.startswith(base + ".")}

    for modele in db.lister_modeles_objets():
        if modele.get("systeme") and modele["systeme"] != equipement["systeme"]:
            continue   # capteurs d'un constructeur : seulement sur ses équipements
        detectes, instances_detectees, repondues = [], {}, set()
        for mib in modele["mibs"]:
            if echecs_consecutifs >= MAX_ECHECS_CONSECUTIFS:
                break
            # Chaque branche compte seule : une lecture trop lente (SMART d'un disque en veille) n'écarte
            # pas les autres valeurs du groupe ; elle sera retentée au relevé suivant
            valeurs, en_echec, une_reponse = {}, False, False
            for base in branches_mib(mib):
                if echecs_consecutifs >= MAX_ECHECS_CONSECUTIFS:
                    en_echec = True
                    break
                try:
                    valeurs.update(await lire_branche(base))
                    une_reponse, echecs_consecutifs = True, 0
                except SnmpError as exc:
                    logger.info("%s ne répond pas à %s (%s) : %s", equipement["nom"], mib["nom"], base, exc)
                    en_echec = True
                    echecs_consecutifs += 1
            if not une_reponse:
                continue
            if valeurs and equipement["version_snmp"] == "V2C":
                valeurs.update(await _valeurs_simples_manquantes(equipement, mib, valeurs, options))
            if not en_echec:   # un objet absent n'est retiré que si tout le groupe a répondu
                repondues.add(mib["nom"])
            instances = _instances_par_objet(mib, valeurs)
            for objet in mib["objets"]:
                if objet.get("diviseur") and objet["nom"] in instances:   # précision déclarée par le modèle (dixièmes de degré...)
                    instances[objet["nom"]] = {i: (n / objet["diviseur"] if (n := _nombre(v)) is not None else v)
                                               for i, v in instances[objet["nom"]].items()}
                if objet["nom"] in instances:
                    nature = _nature_objet(objet)
                    if nature == "MESURE" and not any(_nombre(v) is not None for v in instances[objet["nom"]].values()):
                        nature = "TEXTE"
                    detectes.append({**objet, "mib": mib["nom"], "objet": objet["nom"], "nature": nature})
                    instances_detectees[(mib["nom"], objet["nom"])] = instances[objet["nom"]]
        try:
            identifiants = db.remplacer_objets_detectes(equipement["id"], modele["nom"], detectes, repondues)
        except sqlite3.IntegrityError:
            # L'équipement, ou ce modèle, a été supprimé pendant la détection : rien à enregistrer
            if db.obtenir_equipement(equipement["id"]) is None:
                logger.info("Détection interrompue : %s a été supprimé", equipement["nom"])
                return detections
            continue
        _enregistrer_releve(detectes, identifiants, instances_detectees, db.objets_ignores(equipement["id"]))
        detections[modele["nom"]] = [
            {"objet": objet, "instances": instances_detectees[(objet["mib"], objet["objet"])]} for objet in detectes
        ]
    reevaluer_seuils(equipement["id"])   # seuils des objets, avec les valeurs qui viennent d'être relevées
    return detections


def _enregistrer_releve(objets: list[dict], identifiants: dict, instances: dict, ignores: set[int]) -> None:
    valeurs, mesures = [], []
    avec_graphique = 0
    for objet in objets:
        cle = (objet["mib"], objet["objet"])
        if identifiants[cle] in ignores:   # écarté par l'admin : ni affiché, ni relevé
            continue
        graphique = objet["nature"] == "MESURE" and len(instances[cle]) <= MAX_INSTANCES_GRAPHIQUE and avec_graphique < MAX_OBJETS_GRAPHIQUE
        avec_graphique += graphique
        for instance, valeur in instances[cle].items():
            valeurs.append((identifiants[cle], instance, _valeur_lisible(valeur)))
            nombre = _nombre(valeur)
            if graphique and nombre is not None:
                mesures.append((identifiants[cle], instance, nombre))
    db.enregistrer_releve_objets(valeurs, mesures)


_detections: set[asyncio.Task] = set()
# Un seul relevé lourd à la fois par équipement : un agent lent (NAS qui réveille ses disques) ne répond
# plus à rien pendant qu'il parcourt ses tables, et une identification lancée en même temps conclurait
# à tort que rien ne répond
_verrous: dict[int, asyncio.Lock] = {}


def verrou(id_equipement: int) -> asyncio.Lock:
    return _verrous.setdefault(id_equipement, asyncio.Lock())


def lancer_detection(equipement_id: int | None = None) -> None:
    """Lance la détection en tâche de fond : sur un équipement, ou sur tout le parc."""
    tache = asyncio.create_task(_detecter_en_fond(equipement_id))
    _detections.add(tache)
    tache.add_done_callback(_detections.discard)


async def _detecter_en_fond(equipement_id: int | None) -> None:
    for equipement in db.lister_equipements_actifs():
        if equipement_id in (None, equipement["id"]):
            try:
                async with verrou(equipement["id"]):
                    await detecter_objets(equipement)
            except Exception:
                logger.exception("Détection impossible sur %s", equipement["nom"])


async def relever_objets() -> None:
    """Actualise régulièrement les objets détectés, plusieurs équipements à la fois."""
    limite = asyncio.Semaphore(MAX_RELEVES_SIMULTANES)

    a_identifier = set(db.equipements_a_identifier())

    async def relever(equipement) -> None:
        async with limite:
            try:
                if equipement["id"] in a_identifier:  # injoignable lors d'une identification précédente
                    await identifier_equipement(equipement["id"])
                else:
                    async with verrou(equipement["id"]):
                        await detecter_objets(equipement)
            except Exception:
                logger.exception("Relevé des objets impossible sur %s", equipement["nom"])

    # Tout le parc, pas seulement les équipements déjà détectés : celui qui était injoignable à son
    # ajout est ainsi détecté dès qu'il répond.
    equipements = [e for e in db.lister_equipements_actifs() if e["statut"] != "INDISPONIBLE"]
    await asyncio.gather(*(relever(equipement) for equipement in equipements))


async def collecter_equipement(equipement: dict, services: set[int] | None = None) -> None:
    """Collecte un équipement : test de connectivité, puis les indicateurs de son modèle (seulement
    ceux de `services` si cet ensemble est fourni)."""
    parametres = db.obtenir_parametres_generaux()
    timeout, retries = parametres["delai_reponse_snmp"], config.SNMP_RETRIES
    options = {"timeout": timeout, "retries": retries, "version": equipement["version_snmp"]}
    id_equipement = equipement["id"]

    try:
        await snmp_get(equipement["adresse"], equipement["communaute_snmp"], [OID_SYS_UPTIME], **options)
    except SnmpError as exc:
        await _traiter_echec_snmp(equipement, parametres, exc, timeout, services)
        return

    db.cloturer_alerte(id_equipement, "INJOIGNABLE")
    db.cloturer_alerte(id_equipement, "SNMP_EN_ECHEC")
    ignorees = db.metriques_ignorees(id_equipement)   # services écartés par l'admin
    elements = [element for element in db.lister_elements_actifs(equipement["modele_supervision_id"])
                if (services is None or element["metrique_id"] in services) and element["metrique_id"] not in ignorees]
    if services is None or any(element["par_interface"] for element in elements):
        await decouvrir_interfaces(equipement, timeout, retries)
    interfaces = db.interfaces_a_mesurer(id_equipement)
    echecs_equipement = equipement["nb_echecs_alerte"] or parametres["nb_echecs_avant_indisponibilite"]

    for element in elements:
        metrique_id = element["metrique_id"]
        try:
            if element["code"] in LECTURES:
                valeur = await _lire_indicateur(equipement, element, options)
            elif element["par_interface"]:
                valeur = await _collecter_par_interface(equipement, element, interfaces, options)
            else:
                valeur = await _collecter_scalaire(equipement, element, options)
            if valeur is not None:  # les indicateurs par interface enregistrent eux-mêmes leurs relevés
                db.enregistrer_mesure(id_equipement, metrique_id, float(valeur),
                                      utilise=getattr(valeur, "utilise", None), total=getattr(valeur, "total", None),
                                      detail=getattr(valeur, "detail", None))
                if getattr(valeur, "volumes", None):
                    db.enregistrer_volumes(id_equipement, valeur.volumes)
            db.cloturer_alerte(id_equipement, "RELEVE_EN_ECHEC", metrique_id)
        except (SnmpError, ValueError, TypeError) as exc:
            # ValueError/TypeError : l'agent répond « OID absent » à la place d'un nombre
            detail = exc if isinstance(exc, SnmpError) else "OID absent ou valeur non numérique"
            db.enregistrer_erreur_collecte(id_equipement, "INDICATEUR_NON_SUPPORTE",
                                            f"{element['code']} : {detail}")
            db.enregistrer_mesure(id_equipement, metrique_id, None)   # valeur inconnue : pas d'alerte de seuil
            if db.releve_en_echec_persistant(id_equipement, metrique_id, echecs_equipement):
                db.ouvrir_alerte(id_equipement, "SERVICE", "RELEVE_EN_ECHEC",
                                 f"Relevé SNMP en échec : {element['code']} ({detail})", metrique_id)

    statut = "DEGRADE" if db.a_alerte_service_active(id_equipement) else "OPERATIONNEL"
    db.enregistrer_reussite_collecte(id_equipement, statut)
    reevaluer_seuils(id_equipement)   # avec les valeurs qui viennent d'être relevées
    if equipement["statut"] == "INDISPONIBLE":
        logger.info("%s (%s) est de nouveau joignable", equipement["nom"], equipement["adresse"])


async def _indicateur_repond(equipement: dict, element, options: dict) -> bool:
    """Vrai si l'équipement fournit un pourcentage valide pour cet indicateur (rien n'est enregistré)."""
    try:
        return 0 <= await _lire_indicateur(equipement, element, options) <= 100
    except Exception:  # OID absent, table vide, délai dépassé… : la détection ne bloque jamais un ajout
        return False


async def _score_modele(equipement: dict, id_modele: int, options: dict) -> int:
    """Nombre d'indicateurs (CPU, RAM, disque) de ce modèle auxquels l'équipement répond, testés l'un
    après l'autre pour ne pas submerger un agent lent."""
    elements = [e for e in db.lister_elements_actifs(id_modele) if e["code"] in LECTURES]
    return sum([await _indicateur_repond(equipement, e, options) for e in elements])


async def choisir_modele_integre(adresse: str, communaute: str, version: str) -> int:
    """Choix immédiat, sans Internet : l'hôte standard si son CPU, sa RAM ou ses disques répondent,
    sinon le générique. L'identification qui suit pourra trouver mieux."""
    options = {"timeout": config.SNMP_TIMEOUT, "retries": config.SNMP_RETRIES, "version": version}
    equipement = {"adresse": adresse, "communaute_snmp": communaute}
    if await _score_modele(equipement, db.MODELE_HOTE_STANDARD, options):
        return db.MODELE_HOTE_STANDARD
    return db.MODELE_GENERIQUE


async def _lire_oid(equipement, options: dict, oid: str):
    """Lit un OID pour une règle de reconnaissance : la valeur, ou None si elle est absente."""
    try:
        valeur = (await snmp_get(equipement["adresse"], equipement["communaute_snmp"], [oid], **options))[oid]
    except Exception:  # noqa: BLE001 : OID refusé par l'agent ou par PySNMP, la condition est juste fausse
        return None
    return None if type(valeur).__name__ in VALEURS_ABSENTES else str(valeur)


async def identifier_equipement(id_equipement: int, changer: str = "TOUJOURS") -> dict | None:
    async with verrou(id_equipement):
        return await _identifier(id_equipement, changer)


async def _identifier(id_equipement: int, changer: str) -> dict | None:
    """Reconnaît le constructeur de l'équipement (règles des modèles intégrés), détecte ses capteurs,
    puis choisit entre le modèle de ce constructeur et l'hôte standard, d'après les indicateurs qui
    répondent ; si aucun ne répond, le générique. Tous les modèles restent proposés sur la fiche.
    `changer` : TOUJOURS ; SI_MEILLEUR (depuis un modèle générique, vers celui du constructeur s'il
    répond au moins aussi bien) ; JAMAIS."""
    equipement = db.obtenir_equipement(id_equipement)
    if equipement is None:
        return None
    options = {"timeout": config.SNMP_TIMEOUT, "retries": config.SNMP_RETRIES, "version": equipement["version_snmp"]}
    try:
        await snmp_get(equipement["adresse"], equipement["communaute_snmp"], [OID_SYS_UPTIME], **options)
    except SnmpError:
        # Muet ou occupé : on garde son modèle, l'identification sera refaite au relevé où il répondra
        logger.info("Identification reportée : %s ne répond pas", equipement["nom"])
        db.identification_a_refaire(id_equipement)
        return None

    async def lire(oid):
        return await _lire_oid(equipement, options, oid)
    constructeur = await detection.reconnaitre(db.constructeurs(), equipement["sys_object_id"],
                                               equipement["description_systeme"], lire)
    db.definir_systeme(id_equipement, constructeur["systeme"] if constructeur else None)
    equipement = db.obtenir_equipement(id_equipement) or equipement

    # Le modèle du constructeur d'abord : à égalité, il l'emporte sur l'hôte standard. Les indicateurs
    # sont testés l'un après l'autre et avant le parcours des capteurs, qui peut occuper l'agent longtemps.
    candidats = [constructeur["id_modele"]] if constructeur else []
    candidats.append(db.MODELE_HOTE_STANDARD)
    scores = [await _score_modele(equipement, candidat, options) for candidat in candidats]
    meilleur = max(scores)
    choisi = candidats[scores.index(meilleur)] if meilleur > 0 else db.MODELE_GENERIQUE
    a_retenter = False
    if meilleur == 0:
        # Rien ne répond : l'équipement n'a pas ces indicateurs, ou son agent est occupé. Dans ce cas,
        # il ne répond même plus à une lecture simple.
        try:
            await snmp_get(equipement["adresse"], equipement["communaute_snmp"], [OID_SYS_UPTIME], **options)
        except SnmpError:
            a_retenter = True
    await detecter_objets(equipement)
    if db.obtenir_equipement(id_equipement) is None:   # supprimé pendant l'identification
        return None

    actuel = equipement["modele_supervision_id"]
    resultat = {"constructeur": constructeur and constructeur["cle"]}
    if a_retenter:
        # Joignable, mais aucun indicateur n'a répondu (agent occupé ?) : on garde le modèle actuel et
        # on retentera au prochain relevé, plutôt que de conclure au modèle générique
        logger.info("Identification de %s à refaire : son agent ne répond plus pour le moment", equipement["nom"])
        db.identification_a_refaire(id_equipement)
        return {**resultat, "modele": actuel, "a_retenter": True}
    if changer == "SI_MEILLEUR":
        score_actuel = scores[candidats.index(actuel)] if actuel in candidats else await _score_modele(equipement, actuel, options)
        appliquer = (actuel in db.MODELES_GENERIQUES and choisi not in db.MODELES_GENERIQUES
                     and meilleur >= score_actuel)
    else:
        appliquer = changer == "TOUJOURS"
    if appliquer and choisi != actuel:
        db.changer_modele_supervision(id_equipement, choisi)
        reevaluer_seuils(id_equipement)
        logger.info("Modèle de %s : %s", equipement["nom"],
                    next(m["nom"] for m in db.lister_modeles() if m["id"] == choisi))
    db.identification_terminee(id_equipement)
    return {**resultat, "modele": choisi if appliquer else actuel}


_identifications: set[asyncio.Task] = set()


def lancer_identification(ids: list[int] | None = None, changer: str = "TOUJOURS") -> None:
    """Lance l'identification en tâche de fond, sur ces équipements ou sur tout le parc actif."""
    tache = asyncio.create_task(_identifier_en_fond(ids, changer))
    _identifications.add(tache)
    tache.add_done_callback(_identifications.discard)


async def _identifier_en_fond(ids: list[int] | None, changer: str) -> None:
    if ids is None:
        ids = [equipement["id"] for equipement in db.lister_equipements_actifs()]
    for id_equipement in ids:
        try:
            await identifier_equipement(id_equipement, changer)
        except Exception:
            logger.exception("Identification impossible pour l'équipement %s", id_equipement)


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
    reevaluer_interfaces(equipement["id"])
    return True


def reevaluer_interfaces(id_equipement: int) -> None:
    """Alertes d'interface : une interface suivie (cochée) à l'état « down » ouvre une alerte, qui se
    ferme quand elle repasse « up », n'est plus suivie ou disparaît. Les interfaces non cochées gardent
    leur état affiché, sans alerte : un switch a beaucoup de ports inutilisés, et une alerte sur chacun
    serait du bruit. Appelée après chaque découverte et chaque changement du choix des interfaces."""
    en_panne = {interface["id"]: interface for interface in db.lister_interfaces(id_equipement)
                if interface["suivi"] == "OUI" and interface["presente"] and interface["statut"] == "DOWN"}
    for interface in en_panne.values():
        db.ouvrir_alerte(id_equipement, "SERVICE", "INTERFACE_DOWN", f"Interface {interface['nom']} : down",
                         interface_id=interface["id"])
    for alerte in db.lister_alertes(statut="ACTIVE", equipement_id=id_equipement):
        if alerte["code"] == "INTERFACE_DOWN" and alerte["interface_id"] not in en_panne:
            db.cloturer_alerte(id_equipement, "INTERFACE_DOWN", interface_id=alerte["interface_id"])
    db.actualiser_statut_service(id_equipement)


async def _lire_indicateur(equipement: dict, element, options: dict) -> float:
    """Lit un indicateur en % : selon la méthode d'un modèle de constructeur, sinon selon le calcul propre à son OID."""
    methode = element["methode"] if "methode" in element.keys() else None
    if methode:
        return await _collecter_par_methode(equipement, element, methode, options)
    return await LECTURES[element["code"]](equipement, element, options)


async def _collecter_par_methode(equipement: dict, element, methode: str, options: dict) -> float:
    """Calcul d'un modèle de constructeur : DIRECT, MOYENNE_TABLE, MAX_TABLE, RATIO_UTILISE_TOTAL
    (utilisé / total), RATIO_UTILISE_LIBRE (utilisé / (utilisé + libre)) ou RATIO_LIBRE_TOTAL
    ((total - libre) / total)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    diviseur = (element["diviseur"] if "diviseur" in element.keys() else None) or 1
    if methode == "PREMIER_TABLE":
        return await _collecter_sur_colonnes(equipement, element, methode, options) / diviseur
    if methode.startswith("RATIO_") and methode.endswith("_TABLE"):
        releve = await _collecter_sur_colonnes(equipement, element, methode, options)
        # Pour un ratio, « diviseur » porte l'unité des tailles (1024 pour des Ko)
        return Releve(releve, releve.utilise * diviseur, releve.total * diviseur)
    if methode == "DIRECT":
        return float((await snmp_get(host, community, [element["oid"]], **options))[element["oid"]]) / diviseur
    if methode in ("RATIO_UTILISE_TOTAL", "RATIO_UTILISE_LIBRE", "RATIO_LIBRE_TOTAL"):
        oid_second = element["oid_secondaire"]
        valeurs = await snmp_get(host, community, [element["oid"], oid_second], **options)
        premier, second = _taille(valeurs[element["oid"]]), _taille(valeurs[oid_second])
        if premier is None or second is None:
            raise ValueError("taille illisible")
        total = premier + second if methode == "RATIO_UTILISE_LIBRE" else second
        if total == 0:
            raise SnmpError("total nul")
        utilise = total - premier if methode == "RATIO_LIBRE_TOTAL" else premier
        return Releve(utilise / total * 100, utilise * diviseur, total * diviseur)
    valeurs = [float(valeur) for valeur in (await snmp_walk(host, community, element["oid"], **options)).values()]
    if not valeurs:
        raise SnmpError("table vide ou non supportée")
    return (max(valeurs) if methode == "MAX_TABLE" else sum(valeurs) / len(valeurs)) / diviseur


async def _collecter_sur_colonnes(equipement: dict, element, methode: str, options: dict) -> float:
    """Méthodes d'un modèle de constructeur sur des colonnes : la première instance qui répond (une réserve
    de mémoire, par exemple). PREMIER_TABLE lit un pourcentage ; RATIO_..._TABLE combine deux colonnes."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    valeurs = await snmp_walk(host, community, element["oid"], **options)
    for oid in sorted(valeurs, key=lambda o: [int(p) for p in o.split(".")]):
        premier = _taille(valeurs[oid])
        if premier is None:
            continue
        if methode == "PREMIER_TABLE":
            return premier
        oid_second = element["oid_secondaire"] + oid[len(element["oid"]):]
        reponse = await snmp_get(host, community, [oid_second], **options)
        second = _taille(reponse.get(oid_second))
        if second is None:
            continue
        total = premier + second if methode == "RATIO_UTILISE_LIBRE_TABLE" else second
        if total <= 0:
            continue
        utilise = total - premier if methode == "RATIO_LIBRE_TOTAL_TABLE" else premier
        return Releve(utilise / total * 100, utilise, total)
    raise SnmpError("aucune instance ne répond")


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
    """Charge CPU en % : moyenne des cœurs de hrProcessorLoad, sinon UCD-SNMP (100 - inactivité)."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    if element["oid"] != OID_UCD_CPU_IDLE:
        valeurs = await snmp_walk(host, community, element["oid"], **options)
        if valeurs:
            return sum(int(valeur) for valeur in valeurs.values()) / len(valeurs)
        if element["oid"] != db.OID_HR_PROCESSOR_LOAD:
            raise SnmpError("charge CPU non supportée par cet équipement")
    try:
        valeurs = await snmp_get(host, community, [OID_UCD_CPU_IDLE], **options)
        return 100 - float(valeurs[OID_UCD_CPU_IDLE])
    except (SnmpError, ValueError, TypeError) as exc:
        raise SnmpError("charge CPU non supportée par cet équipement") from exc


async def _collecter_ram(equipement: dict, element, options: dict) -> float:
    """Mémoire utilisée en % : ligne « mémoire vive » de hrStorageTable, sinon UCD-SNMP."""
    if element["oid"] == OID_UCD_MEM_TOTAL:
        return await _collecter_ram_ucd(equipement, options)
    try:
        return await _collecter_ram_host_resources(equipement, element, options)
    except SnmpError as exc:
        try:
            return await _collecter_ram_ucd(equipement, options)
        except (SnmpError, ValueError, TypeError):
            raise exc from None


async def _collecter_disque(equipement: dict, element, options: dict) -> float:
    """Capacité disque en % : celle du volume le plus rempli, car c'est lui qui posera problème."""
    return await _collecter_disque_host_resources(equipement, element, options)


async def _collecter_disque_host_resources(equipement: dict, element, options: dict) -> float:
    """HOST-RESOURCES-MIB : disques fixes de hrStorageTable, hors systèmes virtuels. Si l'équipement
    a des volumes de NAS (/volume1, /share/...), seuls ceux-ci comptent : la partition système d'un
    NAS, petite et souvent presque pleine, déclencherait de fausses alertes."""
    host, community = equipement["adresse"], equipement["communaute_snmp"]
    types = await snmp_walk(host, community, OID_HR_STORAGE_TYPE, **options)
    noms = await snmp_walk(host, community, OID_HR_STORAGE_DESCR, **options)
    utilises = await snmp_walk(host, community, element["oid"], **options)
    tailles = await snmp_walk(host, community, OID_HR_STORAGE_SIZE, **options)
    unites = await snmp_walk(host, community, OID_HR_STORAGE_UNITES, **options)

    volumes = {}  # nom -> (pourcentage utilisé, octets utilisés, octets au total)
    for oid, type_oid in types.items():
        index = oid.rsplit(".", 1)[-1]
        nom = str(noms.get(f"{OID_HR_STORAGE_DESCR}.{index}", ""))
        taille = int(tailles.get(f"{OID_HR_STORAGE_SIZE}.{index}", 0))
        utilise = utilises.get(f"{element['oid']}.{index}")
        # Une taille nulle ou négative : volume vide, ou entier 32 bits dépassé sur un très grand disque
        if str(type_oid) == HR_STORAGE_DISQUE and not DISQUES_IGNORES.search(nom) and taille > 0 and utilise is not None:
            unite = _nombre(unites.get(f"{OID_HR_STORAGE_UNITES}.{index}")) or 1
            volumes[nom] = (int(utilise) / taille * 100, int(utilise) * unite, taille * unite)
    if not volumes:
        raise SnmpError("aucun disque fixe trouvé")
    candidats = {nom: v for nom, v in volumes.items() if VOLUME_NAS.match(nom)} or volumes
    nom = max(candidats, key=lambda n: candidats[n][0])
    # Le plus rempli sert aux seuils et aux alertes ; tous sont gardés pour le graphique et le détail
    return Releve(*candidats[nom], detail=nom, volumes=[(n, *v) for n, v in sorted(candidats.items())])


async def _collecter_ram_ucd(equipement: dict, options: dict) -> float:
    """UCD-SNMP-MIB : mémoire totale et mémoire disponible."""
    valeurs = await snmp_get(equipement["adresse"], equipement["communaute_snmp"],
                             [OID_UCD_MEM_TOTAL, OID_UCD_MEM_DISPONIBLE], **options)
    totale = float(valeurs[OID_UCD_MEM_TOTAL])
    if totale == 0:
        raise SnmpError("memTotalReal = 0")
    utilisee = totale - float(valeurs[OID_UCD_MEM_DISPONIBLE])
    return Releve(utilisee / totale * 100, utilisee * 1024, totale * 1024)   # tailles en Ko


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
    oid_unite = f"{OID_HR_STORAGE_UNITES}.{index_ram}"
    valeurs = await snmp_get(host, community, [oid_utilisee, oid_totale, oid_unite], **options)
    unite = _nombre(valeurs.get(oid_unite)) or 1
    totale = float(valeurs[oid_totale])
    if totale == 0:
        raise SnmpError("hrStorageSize = 0")
    utilisee = float(valeurs[oid_utilisee])
    # Même unité (hrStorageAllocationUnits) que la RAM sur un agent Net-SNMP : on peut soustraire
    noms = await snmp_walk(host, community, OID_HR_STORAGE_DESCR, **options)
    recuperables = [oid.rsplit(".", 1)[-1] for oid, nom in noms.items() if str(nom) in MEMOIRE_RECUPERABLE]
    if recuperables:
        utilises = await snmp_get(host, community, [f"{element['oid']}.{i}" for i in recuperables], **options)
        reprise = sum(_nombre(v) or 0 for v in utilises.values() if type(v).__name__ not in VALEURS_ABSENTES)
        if 0 <= reprise <= utilisee:
            utilisee -= reprise
    return Releve(utilisee / totale * 100, utilisee * unite, totale * unite)


LECTURES = {"cpu": _collecter_cpu, "ram": _collecter_ram, "disque": _collecter_disque}
