"""Client SNMP v2c minimal, basé sur PySNMP 7 (API asyncio) : seul module qui parle SNMP.

Deux fonctions génériques, `snmp_get` (valeurs précises) et `snmp_walk` (une table entière),
utilisées par le service de collecte et le testeur d'OID, plus le test de connectivité fait
à l'ajout d'un équipement (`lire_description_systeme`).
"""
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
    walk_cmd,
)

# OID du groupe "system" de la MIB-II (RFC 1213)
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"   # test de connectivité à l'ajout d'un équipement
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"  # test de connectivité au début de chaque collecte


class SnmpError(Exception):
    """Équipement injoignable ou réponse SNMP en erreur."""


_engine: SnmpEngine | None = None


def _get_engine() -> SnmpEngine:
    """Un seul moteur SNMP pour toute l'application (créé à la première utilisation)."""
    global _engine
    if _engine is None:
        _engine = SnmpEngine()
    return _engine


async def snmp_get(host: str, community: str, oids: list[str], port: int = 161,
                   timeout: float = 2, retries: int = 1) -> dict[str, object]:
    """Envoie un GET SNMP v2c et renvoie {oid: valeur}."""
    try:
        transport = await UdpTransportTarget.create((host, port), timeout=timeout, retries=retries)
    except Exception as exc:  # nom d'hôte introuvable, etc.
        raise SnmpError(f"Adresse « {host} » introuvable (vérifier l'IP ou le nom DNS)") from exc

    error_indication, error_status, error_index, var_binds = await get_cmd(
        _get_engine(),
        CommunityData(community, mpModel=1),  # mpModel=1 -> SNMP v2c
        transport,
        ContextData(),
        *[ObjectType(ObjectIdentity(oid)) for oid in oids],
    )

    if error_indication:  # timeout, problème réseau, mauvaise communauté…
        raise SnmpError(str(error_indication))
    if error_status:      # l'agent a répondu, mais avec une erreur
        fautif = var_binds[int(error_index) - 1][0] if error_index else "?"
        raise SnmpError(f"{error_status.prettyPrint()} sur {fautif}")

    return {str(nom): valeur for nom, valeur in var_binds}


async def snmp_walk(host: str, community: str, oid_base: str, port: int = 161,
                    timeout: float = 2, retries: int = 1) -> dict[str, object]:
    """Parcourt une branche OID (une table SNMP, ex. ifTable) et renvoie {oid_complet: valeur}.

    Utilisé pour les indicateurs qui existent en plusieurs exemplaires sur un équipement
    (une valeur par interface, par cœur CPU...), contrairement à snmp_get qui lit des OID
    scalaires connus à l'avance.
    """
    try:
        transport = await UdpTransportTarget.create((host, port), timeout=timeout, retries=retries)
    except Exception as exc:  # nom d'hôte introuvable, etc.
        raise SnmpError(f"Adresse « {host} » introuvable (vérifier l'IP ou le nom DNS)") from exc

    resultats: dict[str, object] = {}
    parcours = walk_cmd(
        _get_engine(),
        CommunityData(community, mpModel=1),
        transport,
        ContextData(),
        ObjectType(ObjectIdentity(oid_base)),
        lexicographicMode=False,  # s'arrête à la fin de la branche demandée, pas à la fin de la MIB
    )
    async for error_indication, error_status, error_index, var_binds in parcours:
        if error_indication:
            raise SnmpError(str(error_indication))
        if error_status:
            fautif = var_binds[int(error_index) - 1][0] if error_index else "?"
            raise SnmpError(f"{error_status.prettyPrint()} sur {fautif}")
        for nom, valeur in var_binds:
            resultats[str(nom)] = valeur

    return resultats


async def lire_description_systeme(host: str, community: str,
                                   timeout: float = 2, retries: int = 1) -> str:
    """Test de connectivité à l'ajout ou à la modification d'un équipement : lit sysDescr,
    qui est aussi enregistré comme description système de l'équipement. Lève SnmpError si
    l'équipement ne répond pas avec ces paramètres."""
    valeurs = await snmp_get(host, community, [OID_SYS_DESCR], timeout=timeout, retries=retries)
    return valeurs[OID_SYS_DESCR].prettyPrint()
