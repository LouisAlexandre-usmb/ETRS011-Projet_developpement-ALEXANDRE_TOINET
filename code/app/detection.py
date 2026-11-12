"""Reconnaissance du constructeur d'un équipement, à partir des règles des modèles intégrés
(modeles_integres.json) : début du sysObjectID, texte du sysDescr, lecture d'un OID.

Les conditions d'une règle (sysObjectID, sysDescr, lecture d'un OID) doivent toutes être vraies ;
la règle la plus précise l'emporte. Aucune requête réseau n'est faite en dehors de l'équipement
lui-même.
"""
import re


def regex_php(motif: str) -> re.Pattern | None:
    """Traduit une expression PHP (« /^Linux /i ») en expression Python, ou None si elle est illisible."""
    if not isinstance(motif, str) or len(motif) < 2:
        return None
    fin = motif.rfind(motif[0])
    if fin <= 0:
        return None
    options = 0
    for lettre in motif[fin + 1:]:
        options |= {"i": re.I, "s": re.S, "m": re.M, "x": re.X}.get(lettre, 0)
    try:
        return re.compile(motif[1:fin].replace("(?<", "(?P<").replace("(?P<=", "(?<=").replace("(?P<!", "(?<!"), options)
    except re.error:
        return None


def _liste(valeur) -> list:
    if valeur is None:
        return []
    return valeur if isinstance(valeur, list) else [valeur]


def _comparer(valeur, operateur, attendu) -> bool:
    """Comparaison d'une lecture SNMP avec la valeur attendue d'une règle (égal, début, contient…)."""
    operateur = operateur or "="
    if attendu is False:   # « = false » : l'objet est absent ; « != false » : il existe
        return (valeur is None) == (operateur == "=")
    if operateur == "exists":
        return valeur is not None
    if valeur is None:
        return operateur == "!="
    texte = str(valeur)
    attendus = [str(a) for a in _liste(attendu)]
    if operateur == "=":
        return texte in attendus
    if operateur == "!=":
        return texte not in attendus
    if operateur == "starts":
        return any(texte.startswith(a) for a in attendus)
    if operateur == "ends":
        return any(texte.endswith(a) for a in attendus)
    if operateur == "contains":
        return any(a in texte for a in attendus)
    if operateur == "regex":
        return any((motif := regex_php(a)) is not None and motif.search(texte) for a in attendus)
    try:
        nombre, reference = float(texte), float(attendus[0])
    except (ValueError, IndexError):
        return False
    return {">": nombre > reference, "<": nombre < reference, ">=": nombre >= reference,
            "<=": nombre <= reference}.get(operateur, False)


async def evaluer_regle(regle: dict, sys_object_id: str, sys_descr: str, lire) -> int:
    """Score d'une règle de détection (0 si elle ne correspond pas). Les conditions d'une règle
    doivent toutes être vraies. `lire(référence)` lit un OID sur l'équipement (None s'il est absent)."""
    if not isinstance(regle, dict) or set(regle) - {
            "sysObjectID", "sysObjectID_except", "sysObjectID_regex", "sysObjectID_regex_except", "sysDescr",
            "sysDescr_except", "sysDescr_regex", "sysDescr_regex_except", "snmpget", "snmpget_except"}:
        return 0   # condition non gérée (snmpwalk...) : on préfère ne pas conclure
    oid = "." + sys_object_id.lstrip(".") if sys_object_id else ""
    score = 1
    if "sysObjectID" in regle:
        prefixes = [str(p) for p in _liste(regle["sysObjectID"]) if oid.startswith(str(p))]
        if not prefixes:
            return 0
        score += max(len(p) for p in prefixes)
    if any(oid.startswith(str(p)) for p in _liste(regle.get("sysObjectID_except"))):
        return 0
    for cle, attendu in (("sysObjectID_regex", True), ("sysObjectID_regex_except", False)):
        if cle in regle and any((m := regex_php(p)) is not None and m.search(oid) for p in _liste(regle[cle])) != attendu:
            return 0
    if "sysDescr" in regle:
        if not any(str(texte) in sys_descr for texte in _liste(regle["sysDescr"])):
            return 0
        score += 1
    if any(str(texte) in sys_descr for texte in _liste(regle.get("sysDescr_except"))):
        return 0
    for cle, attendu in (("sysDescr_regex", True), ("sysDescr_regex_except", False)):
        if cle in regle and any((m := regex_php(p)) is not None and m.search(sys_descr) for p in _liste(regle[cle])) != attendu:
            return 0
    for cle, attendu in (("snmpget", True), ("snmpget_except", False)):
        if cle in regle:
            condition = regle[cle] if isinstance(regle[cle], dict) else {}
            if not condition.get("oid"):
                return 0
            valeur = await lire(str(condition["oid"]).lstrip("."))
            if _comparer(valeur, condition.get("op"), condition.get("value")) != attendu:
                return 0
            score += 10
    return score


async def reconnaitre(constructeurs: list[dict], sys_object_id: str | None, sys_descr: str | None, lire) -> dict | None:
    """Le constructeur dont une règle correspond le mieux, ou None (l'équipement reste générique)."""
    meilleur, meilleur_score = None, 0
    for constructeur in constructeurs:
        for regle in constructeur["regles"]:
            score = await evaluer_regle(regle, sys_object_id or "", sys_descr or "", lire)
            if score > meilleur_score:
                meilleur, meilleur_score = constructeur, score
    return meilleur
