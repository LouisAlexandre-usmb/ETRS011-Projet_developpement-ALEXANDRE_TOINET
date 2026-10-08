"""Accès aux comptes utilisateurs.

TEMPORAIRE : en attendant la base SQLite, les deux comptes (un admin, un
lecteur) sont déclarés par variables d'environnement et leur mot de passe est
haché au démarrage. Quand la table `utilisateur` existera, seul le contenu de
`trouver_utilisateur` changera (un SELECT) ; le reste de l'application ne
bouge pas.

Table prévue :

    CREATE TABLE utilisateur (
        id           INTEGER PRIMARY KEY,
        login        TEXT NOT NULL UNIQUE,
        hash_mdp     TEXT NOT NULL,              -- hash Argon2id, jamais le mot de passe
        role         TEXT NOT NULL DEFAULT 'lecteur'
                     CHECK (role IN ('admin', 'lecteur')),
        cree_le      TEXT NOT NULL DEFAULT (datetime('now')),
        derniere_cnx TEXT
    );
"""
from dataclasses import dataclass

from app import config
from app.securite import hacher_mot_de_passe

ROLE_ADMIN = "admin"      # configure le parc : équipements, seuils, comptes
ROLE_LECTEUR = "lecteur"  # consulte le tableau de bord et les fiches, sans rien modifier


@dataclass(frozen=True)
class Utilisateur:
    login: str
    hash_mdp: str
    role: str

    @property
    def est_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _comptes_depuis_environnement() -> dict[str, Utilisateur]:
    comptes = {}
    for login, mot_de_passe, role in (
        (config.ADMIN_LOGIN, config.ADMIN_PASSWORD, ROLE_ADMIN),
        (config.LECTEUR_LOGIN, config.LECTEUR_PASSWORD, ROLE_LECTEUR),
    ):
        if login and mot_de_passe:  # compte ignoré s'il n'a pas de mot de passe
            comptes[login] = Utilisateur(login, hacher_mot_de_passe(mot_de_passe), role)
    return comptes


_COMPTES = _comptes_depuis_environnement()


def trouver_utilisateur(login: str) -> Utilisateur | None:
    return _COMPTES.get(login)
