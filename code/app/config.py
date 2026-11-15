"""Configuration de l'application, lue depuis les variables d'environnement.

Les valeurs par défaut permettent aussi de lancer l'application hors Docker.
"""
import os

# Paramètres des requêtes SNMP
SNMP_TIMEOUT = float(os.getenv("SNMP_TIMEOUT", "2"))   # secondes
SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "1"))

# Base SQLite
DB_PATH = os.getenv("DB_PATH", "data/supervision.db")
