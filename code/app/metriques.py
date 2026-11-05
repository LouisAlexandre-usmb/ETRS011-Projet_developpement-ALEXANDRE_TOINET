"""Calculs dérivés des mesures brutes, faits à la lecture : les relevés restent stockés tels
quels, donc ces calculs restent recalculables si leur logique évolue."""
from datetime import datetime

MAX_COMPTEUR_32_BITS = 2 ** 32
MARGE_VITESSE = 1.2   # tolère l'imprécision des horodatages, à la seconde près


def _octets_ecoules(precedent: float, courant: float) -> float | None:
    """Différence entre deux relevés d'un compteur. Un compteur qui recule a soit fait le tour
    (il était dans sa moitié haute), soit été remis à zéro par un redémarrage : ce second cas
    ne permet aucun calcul."""
    if courant >= precedent:
        return courant - precedent
    if precedent >= MAX_COMPTEUR_32_BITS / 2:
        return courant - precedent + MAX_COMPTEUR_32_BITS
    return None


def calculer_debit(mesures: list, vitesse: int | None = None) -> list[dict]:
    """Transforme des relevés de compteur d'octets en débit (bits/s). Un relevé absent ou un
    débit impossible (au-delà de la vitesse de l'interface) donne None : un trou dans la courbe."""
    points = []
    precedent = None
    for mesure in mesures:
        valeur, horodatage = mesure["valeur"], mesure["horodatage"]
        if valeur is None:
            points.append({"horodatage": horodatage, "valeur": None})
            precedent = None
            continue
        if precedent is not None:
            duree = (datetime.fromisoformat(horodatage)
                     - datetime.fromisoformat(precedent["horodatage"])).total_seconds()
            octets = _octets_ecoules(precedent["valeur"], valeur)
            debit = octets * 8 / duree if octets is not None and duree > 0 else None
            if debit is not None and vitesse and debit > vitesse * MARGE_VITESSE:
                debit = None
            points.append({"horodatage": horodatage, "valeur": debit})
        precedent = mesure
    return points
