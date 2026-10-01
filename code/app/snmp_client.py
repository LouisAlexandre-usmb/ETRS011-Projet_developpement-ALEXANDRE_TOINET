"""Client SNMP v2c minimal, basé sur PySNMP 7 (API asyncio).

Il sera réutilisé par le service de collecte : une fonction générique
`snmp_get` + des fonctions métier (ici : lecture du groupe "system").
"""
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
)

# OID du groupe "system" de la MIB-II (RFC 1213)
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_LOCATION = "1.3.6.1.2.1.1.6.0"


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


def formater_uptime(ticks: int) -> str:
    """sysUpTime est exprimé en centièmes de seconde -> « 3 j 04:12:55 »."""
    secondes = int(ticks) // 100
    jours, reste = divmod(secondes, 86400)
    heures, reste = divmod(reste, 3600)
    minutes, secondes = divmod(reste, 60)
    return f"{jours} j {heures:02d}:{minutes:02d}:{secondes:02d}"


async def lire_infos_systeme(host: str, community: str, port: int = 161,
                             timeout: float = 2, retries: int = 1) -> dict[str, str]:
    """Lit le groupe "system" d'un équipement (sysDescr, sysName, uptime…)."""
    valeurs = await snmp_get(
        host, community,
        [OID_SYS_DESCR, OID_SYS_NAME, OID_SYS_UPTIME, OID_SYS_LOCATION, OID_SYS_CONTACT],
        port=port, timeout=timeout, retries=retries,
    )
    return {
        "sysDescr": valeurs[OID_SYS_DESCR].prettyPrint(),
        "sysName": valeurs[OID_SYS_NAME].prettyPrint(),
        "sysUpTime": formater_uptime(int(valeurs[OID_SYS_UPTIME])),
        "sysLocation": valeurs[OID_SYS_LOCATION].prettyPrint(),
        "sysContact": valeurs[OID_SYS_CONTACT].prettyPrint(),
    }
