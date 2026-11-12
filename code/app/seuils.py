"""Seuils d'alerte d'un équipement : pour chaque service relevé, son unité, la condition proposée
par défaut et la façon de comparer une valeur.

Un seuil compare la valeur d'un service à une limite d'avertissement et/ou une limite critique,
selon une condition : « supérieur à », « inférieur à » ou « égal à ». Les limites sont saisies dans
l'unité du service (%, Mb/s, minutes, °C...), et les valeurs brutes sont converties avant comparaison.
"""
import json
import re

OPERATEURS = {">": "supérieur à", "<": "inférieur à", "=": "égal à"}

# Services de mesure : unité affichée, bornes de saisie, pas et condition proposée
PROFILS_METRIQUES = {
    "cpu": {"unite": "%", "min": 0, "max": 100, "pas": 0.1, "operateur": ">", "nature": "NOMBRE"},
    "ram": {"unite": "%", "min": 0, "max": 100, "pas": 0.1, "operateur": ">", "nature": "NOMBRE"},
    "disque": {"unite": "%", "min": 0, "max": 100, "pas": 0.1, "operateur": ">", "nature": "NOMBRE"},
    # Uptime : un équipement qui vient de redémarrer a un uptime faible, d'où « inférieur à »
    "uptime": {"unite": "min", "min": 0, "max": None, "pas": 1, "operateur": "<", "nature": "NOMBRE"},
    # Trafic : le débit (calculé entre deux relevés des compteurs), pas le compteur lui-même
    "if_in_octets": {"unite": "Mb/s", "min": 0, "max": None, "pas": 0.01, "operateur": ">", "nature": "NOMBRE"},
    "if_out_octets": {"unite": "Mb/s", "min": 0, "max": None, "pas": 0.01, "operateur": ">", "nature": "NOMBRE"},
}

# Capteurs : leur description commence par leur classe (« Température : Disk 1 »)
UNITES_CAPTEURS = {
    "Température": "°C", "Ventilateur": "tr/min", "Tension": "V", "Courant": "A", "Puissance": "W",
    "Humidité": "%", "Pourcentage": "%", "Charge": "%", "Fréquence": "Hz", "Autonomie": "min",
    "Signal optique": "dBm", "Énergie consommée": "kWh", "Pression": "Pa", "Débit d'air": "CFM",
}


def profil_metrique(code: str) -> dict | None:
    return PROFILS_METRIQUES.get(code)


def profil_objet(objet) -> dict | None:
    """Profil d'un objet détecté : un nombre (mesure ou compteur), ou un état et ses libellés.
    Un texte n'a pas de seuil."""
    nature = objet["nature"]
    if nature == "ETAT":
        etats = json.loads(objet["etats"] or "{}")
        return {"unite": "", "min": None, "max": None, "pas": 1, "operateur": "=", "nature": "ETAT",
                "etats": {int(cle): libelle for cle, libelle in etats.items() if str(cle).lstrip("-").isdigit()}}
    if nature in ("MESURE", "COMPTEUR"):
        classe = str(objet["description"] or "").split(" : ", 1)[0]
        return {"unite": UNITES_CAPTEURS.get(classe, ""), "min": None, "max": None, "pas": "any",
                "operateur": ">", "nature": "NOMBRE"}
    return None


def convertir_metrique(code: str, valeur: float | None) -> float | None:
    """Valeur relevée -> unité du seuil. L'uptime est relevé en centièmes de seconde."""
    if valeur is None:
        return None
    return valeur / 6000 if code == "uptime" else valeur


def depasse(valeur: float, operateur: str, limite: float | None) -> bool:
    if limite is None:
        return False
    if operateur == ">":
        return valeur > limite
    if operateur == "<":
        return valeur < limite
    return valeur == limite


def niveau(valeur: float | None, seuil) -> str | None:
    """CRITIQUE, AVERTISSEMENT, ou None si la valeur respecte le seuil (ou est inconnue)."""
    if valeur is None:
        return None
    if depasse(valeur, seuil["operateur"], seuil["critique"]):
        return "CRITIQUE"
    if depasse(valeur, seuil["operateur"], seuil["avertissement"]):
        return "AVERTISSEMENT"
    return None


def nombre(valeur) -> float | None:
    """Valeur relevée (nombre, ou texte comme « 42 ») -> nombre, ou None."""
    try:
        return float(valeur)
    except (TypeError, ValueError):
        correspondance = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(valeur or ""))
        return float(correspondance.group(1)) if correspondance else None


def valider(profil: dict, operateur: str, avertissement: float | None, critique: float | None,
            actif: bool) -> str | None:
    """Vérifie un seuil saisi ; renvoie le message d'erreur, ou None."""
    if operateur not in OPERATEURS:
        return "Condition inconnue"
    if profil["nature"] == "ETAT":
        if operateur != "=":
            return "Un état se compare avec « égal à »"
        for limite in (avertissement, critique):
            if limite is not None and int(limite) not in profil["etats"]:
                return "État inconnu pour ce service"
    for limite in (avertissement, critique):
        if limite is None:
            continue
        if profil["min"] is not None and limite < profil["min"]:
            return f"Valeur trop basse (minimum {profil['min']} {profil['unite']})".strip()
        if profil["max"] is not None and limite > profil["max"]:
            return f"Valeur trop haute (maximum {profil['max']} {profil['unite']})".strip()
    if actif and avertissement is None and critique is None:
        return "Indiquez au moins une limite (avertissement ou critique)"
    if avertissement is not None and critique is not None:
        if operateur == ">" and avertissement > critique:
            return "Avec « supérieur à », l'avertissement ne peut pas dépasser le niveau critique"
        if operateur == "<" and avertissement < critique:
            return "Avec « inférieur à », l'avertissement ne peut pas être sous le niveau critique"
    return None


def formater(valeur: float | None, profil: dict) -> str:
    """Valeur lisible dans l'unité du service ; un état est traduit par son libellé."""
    if valeur is None:
        return "—"
    if profil["nature"] == "ETAT":
        return profil["etats"].get(int(valeur), str(int(valeur)))
    texte = f"{valeur:.2f}".rstrip("0").rstrip(".") if valeur != int(valeur) else str(int(valeur))
    return f"{texte} {profil['unite']}".strip()
