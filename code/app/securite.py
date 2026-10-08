"""Briques de sécurité : hachage des mots de passe et limitation des tentatives.

Aucune dépendance au reste de l'application : ces fonctions resteront
identiques quand les comptes seront stockés en base.
"""
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2id avec les paramètres par défaut recommandés par argon2-cffi
_hacheur = PasswordHasher()


def hacher_mot_de_passe(mot_de_passe: str) -> str:
    """Renvoie le hash Argon2id (sel inclus) à stocker à la place du mot de passe."""
    return _hacheur.hash(mot_de_passe)


def verifier_mot_de_passe(hash_mdp: str, mot_de_passe: str) -> bool:
    try:
        return _hacheur.verify(hash_mdp, mot_de_passe)
    except (VerificationError, InvalidHashError):
        return False


# Hash factice : vérifié quand le login n'existe pas, pour que la réponse prenne
# le même temps qu'avec un vrai compte (sinon on devine les logins existants).
HASH_LEURRE = hacher_mot_de_passe("leurre")


# ------------------------------------------------- limitation des tentatives
MAX_ECHECS = 5          # échecs tolérés…
FENETRE_SECONDES = 60   # …sur cette durée, avant blocage

_echecs: dict[str, list[float]] = {}   # clé (adresse IP) -> instants des échecs récents


def secondes_avant_deblocage(cle: str) -> int:
    """0 si la clé peut tenter une connexion, sinon le nombre de secondes à attendre."""
    maintenant = time.monotonic()
    recents = [t for t in _echecs.get(cle, []) if maintenant - t < FENETRE_SECONDES]
    _echecs[cle] = recents
    if len(recents) < MAX_ECHECS:
        return 0
    return int(FENETRE_SECONDES - (maintenant - recents[0])) + 1


def noter_echec(cle: str) -> None:
    _echecs.setdefault(cle, []).append(time.monotonic())


def effacer_echecs(cle: str) -> None:
    _echecs.pop(cle, None)
