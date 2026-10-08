"""Configuration de l'application, lue depuis les variables d'environnement.

Les valeurs par défaut permettent aussi de lancer l'application hors Docker.
"""
import os
import secrets

# Équipement interrogé par la page de démonstration
SNMP_TARGET = os.getenv("SNMP_TARGET", "127.0.0.1")
SNMP_PORT = int(os.getenv("SNMP_PORT", "161"))
SNMP_COMMUNITY = os.getenv("SNMP_COMMUNITY", "public")

# Paramètres des requêtes SNMP
SNMP_TIMEOUT = float(os.getenv("SNMP_TIMEOUT", "2"))   # secondes
SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "1"))

# Base SQLite (utilisée dans les prochaines étapes)
DB_PATH = os.getenv("DB_PATH", "donnees/supervision.db")

# Sessions de connexion : clé de signature du cookie. Sans SECRET_KEY, une clé
# aléatoire est tirée à chaque démarrage (tout le monde est alors déconnecté).
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
SESSION_DUREE = int(os.getenv("SESSION_DUREE", str(8 * 3600)))   # secondes

# Comptes TEMPORAIRES, en attendant la table `utilisateur` (cf. app/utilisateurs.py).
# Un compte sans mot de passe n'est pas créé.
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
LECTEUR_LOGIN = os.getenv("LECTEUR_LOGIN", "lecteur")
LECTEUR_PASSWORD = os.getenv("LECTEUR_PASSWORD", "")
