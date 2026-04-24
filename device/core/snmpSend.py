"""
snmpSend.py — SNMP Trap Sender for DAS Communication System
------------------------------------------------------------
Provides three public alarm functions for use across the system:

    send_invalid_kpi_alarm()  — fires when a KPI has too many invalid samples
    send_threshold_alarm()    — fires when a time-averaged KPI is below threshold
    send_runtime_alarm()      — fires when a system/modem-level runtime failure
                                occurs (e.g. COPS command failure, file write
                                error, serial port issue). Has no band or KPI
                                context — carries component + detail instead.

OID Structure:
    Enterprise root  :  1.3.6.1.4.1.12345
    ├── .1  Trap OIDs
    │     ├── .1  trapInvalidKPI     (1.3.6.1.4.1.12345.1.1)
    │     ├── .2  trapThresholdKPI   (1.3.6.1.4.1.12345.1.2)
    │     └── .3  trapRuntime        (1.3.6.1.4.1.12345.1.3)  ← NEW
    └── .2  Varbind OIDs
          ├── .1.0  band             Integer32  — band number        (KPI traps)
          ├── .2.0  kpi              OctetString — KPI name          (KPI traps)
          ├── .3.0  alarmType        OctetString — alarm category    (KPI traps)
          ├── .4.0  detail           OctetString — human-readable detail (all traps)
          └── .5.0  component        OctetString — what failed       (runtime traps)

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
NMS_IP    = "10.8.0.1"
NMS_PORT  = 1162               # Change to 162 in production
COMMUNITY = "public"

# ── OID Definitions ───────────────────────────────────────────────────────────
_ROOT = "1.3.6.1.4.1.12345"

OID_TRAP_INVALID   = f"{_ROOT}.1.1"   # Trap type: invalid KPI samples
OID_TRAP_THRESHOLD = f"{_ROOT}.1.2"   # Trap type: threshold breach
OID_TRAP_RUNTIME   = f"{_ROOT}.1.3"   # Trap type: system/modem runtime failure

OID_VAR_BAND      = f"{_ROOT}.2.1.0"  # Varbind: band number    (Integer32)  — KPI traps
OID_VAR_KPI       = f"{_ROOT}.2.2.0"  # Varbind: KPI name       (OctetString) — KPI traps
OID_VAR_ALARM     = f"{_ROOT}.2.3.0"  # Varbind: alarm type     (OctetString) — KPI traps
OID_VAR_DETAIL    = f"{_ROOT}.2.4.0"  # Varbind: detail message (OctetString) — all traps
OID_VAR_COMPONENT = f"{_ROOT}.2.5.0"  # Varbind: what failed    (OctetString) — runtime traps


# ══════════════════════════════════════════════════════════════════════════════
# Internal: KPI Trap Sender (band + kpi context)
# ══════════════════════════════════════════════════════════════════════════════

async def _send_kpi_trap_async(
    trap_oid:   str,
    band:       int,
    kpi:        str,
    alarm_type: str,
    detail:     str,
) -> None:
    """
    Builds and sends one SNMPv2c KPI trap. Called internally — do not call directly.
    Used by send_invalid_kpi_alarm and send_threshold_alarm.

    Args:
        trap_oid:   OID that identifies the trap type (INVALID or THRESHOLD).
        band:       Band number.
        kpi:        KPI name string (e.g. "RSRP", "SS_RSRP").
        alarm_type: Alarm category string ("INVALID" or "THRESHOLD").
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
        print(f"[SNMP ERROR] KPI trap failed ({alarm_type} | Band {band} | {kpi}): {errorIndication}")
    elif errorStatus:
        print(f"[SNMP ERROR] KPI trap failed ({alarm_type} | Band {band} | {kpi}): {errorStatus.prettyPrint()}")
    else:
        print(f"[SNMP SENT]  {alarm_type} | Band {band} | {kpi} | {detail}")


def _send_kpi_trap(trap_oid: str, band: int, kpi: str, alarm_type: str, detail: str) -> None:
    """Synchronous wrapper around _send_kpi_trap_async for use in non-async code."""
    asyncio.run(_send_kpi_trap_async(trap_oid, band, kpi, alarm_type, detail))


# ══════════════════════════════════════════════════════════════════════════════
# Internal: Runtime Trap Sender (no band/kpi context)
# ══════════════════════════════════════════════════════════════════════════════

async def _send_runtime_trap_async(component: str, detail: str) -> None:
    """
    Builds and sends one SNMPv2c runtime trap. Called internally — do not call directly.
    Used by send_runtime_alarm for system/modem level failures that have no
    band or KPI context (e.g. COPS command failure, file write error).

    Varbinds sent:
        OID_VAR_COMPONENT — what failed (e.g. "AT+COPS=0", "GUI file write")
        OID_VAR_DETAIL    — what happened (e.g. "no response after 120s")

    Args:
        component: The system component or command that failed.
        detail:    Human-readable description of the failure.
    """
    snmpEngine = SnmpEngine()

    errorIndication, errorStatus, errorIndex, varBinds = await sendNotification(
        snmpEngine,
        CommunityData(COMMUNITY, mpModel=1),
        UdpTransportTarget((NMS_IP, NMS_PORT)),
        ContextData(),
        "trap",
        NotificationType(
            ObjectIdentity(OID_TRAP_RUNTIME)
        ).addVarBinds(
            (OID_VAR_COMPONENT, OctetString(component)),
            (OID_VAR_DETAIL,    OctetString(detail)),
        ),
    )

    snmpEngine.closeDispatcher()

    if errorIndication:
        print(f"[SNMP ERROR] Runtime trap failed ({component}): {errorIndication}")
    elif errorStatus:
        print(f"[SNMP ERROR] Runtime trap failed ({component}): {errorStatus.prettyPrint()}")
    else:
        print(f"[SNMP SENT]  RUNTIME | {component} | {detail}")


def _send_runtime_trap(component: str, detail: str) -> None:
    """Synchronous wrapper around _send_runtime_trap_async for use in non-async code."""
    asyncio.run(_send_runtime_trap_async(component, detail))


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def send_invalid_kpi_alarm(band: int, kpi: str, invalid_count: int) -> None:
    """
    Send an SNMP trap for a KPI with too many consecutive invalid samples.

    Args:
        band:          Band number.
        kpi:           KPI name (e.g. "RSRP", "SS_RSRP").
        invalid_count: Number of consecutive invalid samples detected (typically 3).
    """
    detail = f"{invalid_count} of 5 samples invalid (last 3 consecutive)"
    _send_kpi_trap(
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
    _send_kpi_trap(
        trap_oid   = OID_TRAP_THRESHOLD,
        band       = band,
        kpi        = kpi,
        alarm_type = "THRESHOLD",
        detail     = detail,
    )


def send_runtime_alarm(component: str, detail: str) -> None:
    """
    Send an SNMP trap for a system or modem-level runtime failure.

    This is the general-purpose alarm for failures that have no band or KPI
    context — use it anywhere in the codebase when a command, file operation,
    or system component fails and Infolink needs to be notified.

    Examples:
        send_runtime_alarm("AT+COPS=0",     "no modem response after 120s — retrying")
        send_runtime_alarm("AT+CFUN=1",     "modem returned ERROR after 3 attempts")
        send_runtime_alarm("GUI file write","disk full — GUI not updated this cycle")
        send_runtime_alarm("serial port",   "connection lost — attempting reconnect")

    Args:
        component: The command, module, or system component that failed.
                   Keep it short and specific so the NMS can filter by it.
        detail:    Human-readable description of what went wrong.
    """
    _send_runtime_trap(component=component, detail=detail)


