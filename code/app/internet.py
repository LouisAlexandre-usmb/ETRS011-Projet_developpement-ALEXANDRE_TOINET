"""Test régulier de l'accès à Internet : les graphiques chargent Chart.js depuis un CDN. Sans
Internet, l'application fonctionne, mais un bandeau prévient que les graphiques peuvent manquer."""
import asyncio
import logging
import urllib.error
import urllib.request

from app import config

logger = logging.getLogger(__name__)

DELAI_TEST_INTERNET = 3   # secondes

_acces_internet: bool | None = None   # None tant que le test du démarrage n'est pas terminé
_tache_demarrage: asyncio.Task | None = None


def _tester_internet() -> bool:
    try:
        requete = urllib.request.Request(config.URL_TEST_INTERNET, method="HEAD")
        with urllib.request.urlopen(requete, timeout=DELAI_TEST_INTERNET):  # noqa: S310 (adresse de configuration)
            return True
    except urllib.error.HTTPError:  # le serveur a répondu, même par une erreur : Internet est accessible
        return True
    except (OSError, ValueError):
        return False


async def verifier_internet() -> bool:
    """Teste l'accès à Internet et mémorise le résultat, dans un thread pour ne rien bloquer."""
    global _acces_internet
    _acces_internet = await asyncio.to_thread(_tester_internet)
    if _acces_internet:
        logger.info("Accès à Internet : oui")
    else:
        logger.warning("Accès à Internet : non, les graphiques (Chart.js) peuvent ne pas s'afficher")
    return _acces_internet


def acces_internet() -> bool | None:
    return _acces_internet


def demarrer() -> None:
    """Lance le test en tâche de fond : l'application démarre sans attendre le réseau."""
    global _tache_demarrage
    _tache_demarrage = asyncio.create_task(verifier_internet())


def arreter() -> None:
    if _tache_demarrage is not None:
        _tache_demarrage.cancel()
