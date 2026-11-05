"""Service de collecte : interroge périodiquement les équipements et met à jour leur statut.

Première version, limitée à la disponibilité : aucune mesure n'est encore historisée.
Un équipement passe INDISPONIBLE après plusieurs échecs consécutifs, pas dès le premier
timeout, pour éviter les fausses alertes liées à une perte réseau ponctuelle.
"""
import asyncio
import logging
import sqlite3
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import config, db
from app.snmp_client import OID_SYS_UPTIME, snmp_get

logger = logging.getLogger(__name__)

# Fréquence à laquelle on cherche les équipements à interroger (et non l'intervalle de collecte) :
# un équipement ajouté reçoit ainsi sa première collecte en quelques secondes.
PERIODE_VERIFICATION = 5   # secondes

_planificateur: AsyncIOScheduler | None = None
_prochaine_collecte: dict[int, float] = {}       # id équipement -> instant de la prochaine collecte
_en_cours: dict[int, asyncio.Task] = {}          # id équipement -> collecte en cours


async def _collecter(equipement: sqlite3.Row, parametres: sqlite3.Row) -> None:
    """Interroge un équipement et enregistre le résultat."""
    seuil = parametres["nb_echecs_avant_indisponibilite"]
    try:
        await snmp_get(
            equipement["adresse"], equipement["communaute_snmp"], [OID_SYS_UPTIME],
            timeout=parametres["delai_reponse_snmp"], retries=config.SNMP_RETRIES,
        )
    except Exception as exc:  # une erreur sur un équipement ne doit pas toucher les autres
        db.noter_collecte_echouee(equipement["id"], seuil)
        logger.warning(
            "Collecte en échec sur %s (%s), échec consécutif n° %d, indisponible à partir de %d : %s",
            equipement["nom"], equipement["adresse"], equipement["nb_echecs_consecutifs"] + 1,
            seuil, exc,
        )
        return

    db.noter_collecte_reussie(equipement["id"])
    if equipement["statut"] == "INDISPONIBLE":
        logger.info("%s (%s) est de nouveau joignable", equipement["nom"], equipement["adresse"])


async def _lancer_collectes() -> None:
    """Démarre, chacune dans sa propre tâche, les collectes arrivées à échéance : un équipement
    lent à répondre ne retarde donc pas les autres."""
    parametres = db.lire_parametres()
    maintenant = time.monotonic()
    for equipement in db.lister_equipements_actifs():
        id_equipement = equipement["id"]
        if id_equipement in _en_cours or maintenant < _prochaine_collecte.get(id_equipement, 0):
            continue
        intervalle = equipement["intervalle_polling"] or parametres["intervalle_polling_defaut"]
        _prochaine_collecte[id_equipement] = maintenant + intervalle
        tache = asyncio.create_task(_collecter(equipement, parametres))
        _en_cours[id_equipement] = tache
        tache.add_done_callback(lambda _, cle=id_equipement: _en_cours.pop(cle, None))


def demarrer() -> None:
    """Démarre le planificateur (à appeler depuis la boucle asyncio de l'application)."""
    global _planificateur
    _planificateur = AsyncIOScheduler()
    _planificateur.add_job(
        _lancer_collectes, "interval", seconds=PERIODE_VERIFICATION, max_instances=1, coalesce=True,
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
