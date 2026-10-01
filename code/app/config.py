"""Configuration de l'application, lue depuis les variables d'environnement.

Les valeurs par défaut permettent aussi de lancer l'application hors Docker.
"""
import os

# Équipement interrogé par la page de démonstration
SNMP_TARGET = os.getenv("SNMP_TARGET", "127.0.0.1")
SNMP_PORT = int(os.getenv("SNMP_PORT", "161"))
SNMP_COMMUNITY = os.getenv("SNMP_COMMUNITY", "public")

# Paramètres des requêtes SNMP
SNMP_TIMEOUT = float(os.getenv("SNMP_TIMEOUT", "2"))   # secondes
SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "1"))

# Base SQLite (utilisée dans les prochaines étapes)
DB_PATH = os.getenv("DB_PATH", "donnees/supervision.db")