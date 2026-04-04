"""
snmpSend.py — SNMP Trap Sender for DAS Communication System
------------------------------------------------------------
Provides two public alarm functions for use by alarms.py:

    send_invalid_kpi_alarm()  — fires when a KPI has too many invalid samples
    send_threshold_alarm()    — fires when a time-averaged KPI is below threshold

OID Structure:
    Enterprise root  :  1.3.6.1.4.1.12345
    ├── .1  Trap OIDs
    │     ├── .1  trapInvalidKPI     (1.3.6.1.4.1.12345.1.1)
    │     └── .2  trapThresholdKPI   (1.3.6.1.4.1.12345.1.2)
    └── .2  Varbind OIDs
          ├── .1.0  band             Integer32  — band number
          ├── .2.0  kpi              OctetString — KPI name  (e.g. "RSRP")
          ├── .3.0  alarmType        OctetString — "INVALID" or "THRESHOLD"
          └── .4.0  detail           OctetString — human-readable detail message

Production changes needed:
    - Set NMS_IP to the company server IP
    - Change NMS_PORT from 1162 (test) to 162 (production)
    - Replace enterprise OID 12345 with your assigned PEN if applicable
"""

import asyncio
from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    NotificationType,
    ObjectIdentity,
    Integer32,
    OctetString,
    sendNotification,
)

# ── Configuration ─────────────────────────────────────────────────────────────
NMS_IP    = "10.231.136.163"
NMS_PORT  = 162               # Change to 162 in production
COMMUNITY = "public"

# ── OID Definitions ───────────────────────────────────────────────────────────
_ROOT = "1.3.6.1.4.1.12345"

OID_TRAP_INVALID   = f"{_ROOT}.1.1"   # Trap type: invalid KPI samples
OID_TRAP_THRESHOLD = f"{_ROOT}.1.2"   # Trap type: threshold breach

OID_VAR_BAND   = f"{_ROOT}.2.1.0"    # Varbind: band number  (Integer32)
OID_VAR_KPI    = f"{_ROOT}.2.2.0"    # Varbind: KPI name     (OctetString)
OID_VAR_ALARM  = f"{_ROOT}.2.3.0"    # Varbind: alarm type   (OctetString)
OID_VAR_DETAIL = f"{_ROOT}.2.4.0"    # Varbind: detail msg   (OctetString)


# ── Internal async sender ─────────────────────────────────────────────────────
async def _send_trap_async(
    trap_oid:   str,
    band:       int,
    kpi:        str,
    alarm_type: str,
    detail:     str,
) -> None:
    """
    Builds and sends one SNMPv2c trap. Called internally — do not call directly.

    Args:
        trap_oid:   OID that identifies the trap type (INVALID or THRESHOLD).
        band:       Band number.
        kpi:        KPI name string (e.g. "RSRP", "SS_RSRP").
        alarm_type: Human-readable alarm category ("INVALID" or "THRESHOLD").
        detail:     Detail message string carried in the varbind.
    """
    snmpEngine = SnmpEngine()

    errorIndication, errorStatus, errorIndex, varBinds = await sendNotification(
        snmpEngine,
        CommunityData(COMMUNITY, mpModel=1),
        UdpTransportTarget((NMS_IP, NMS_PORT)),
        ContextData(),
        "trap",
        NotificationType(
            ObjectIdentity(trap_oid)
        ).addVarBinds(
            (OID_VAR_BAND,   Integer32(band)),
            (OID_VAR_KPI,    OctetString(kpi)),
            (OID_VAR_ALARM,  OctetString(alarm_type)),
            (OID_VAR_DETAIL, OctetString(detail)),
        ),
    )

    snmpEngine.closeDispatcher()

    if errorIndication:
        print(f"[SNMP ERROR] Trap failed ({alarm_type} | Band {band} | {kpi}): {errorIndication}")
    elif errorStatus:
        print(f"[SNMP ERROR] Trap failed ({alarm_type} | Band {band} | {kpi}): {errorStatus.prettyPrint()}")
    else:
        print(f"[SNMP SENT]  {alarm_type} | Band {band} | {kpi} | {detail}")


def _send_trap(trap_oid: str, band: int, kpi: str, alarm_type: str, detail: str) -> None:
    """Synchronous wrapper around _send_trap_async for use in non-async code."""
    asyncio.run(_send_trap_async(trap_oid, band, kpi, alarm_type, detail))


# ── Public API ────────────────────────────────────────────────────────────────
def send_invalid_kpi_alarm(band: int, kpi: str, invalid_count: int) -> None:
    """
    Send an SNMP trap for a KPI with too many consecutive invalid samples.

    Args:
        band:          Band number.
        kpi:           KPI name (e.g. "RSRP", "SS_RSRP").
        invalid_count: Number of consecutive invalid samples detected (typically 3).
    """
    detail = f"last {invalid_count} samples invalid"
    _send_trap(
        trap_oid   = OID_TRAP_INVALID,
        band       = band,
        kpi        = kpi,
        alarm_type = "INVALID",
        detail     = detail,
    )


def send_threshold_alarm(band: int, kpi: str, avg_value: float, threshold: float) -> None:
    """
    Send an SNMP trap for a KPI whose time-averaged value is below threshold.

    Args:
        band:      Band number.
        kpi:       KPI name (e.g. "SINR", "SS_RSRP").
        avg_value: The computed average value.
        threshold: The threshold it failed to meet.
    """
    detail = f"avg = {avg_value:.1f}, below threshold ({threshold:.1f})"
    _send_trap(
        trap_oid   = OID_TRAP_THRESHOLD,
        band       = band,
        kpi        = kpi,
        alarm_type = "THRESHOLD",
        detail     = detail,
    )