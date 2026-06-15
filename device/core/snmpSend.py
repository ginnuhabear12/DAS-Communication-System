"""
Module: snmpSend.py
Purpose: SNMP trap sender for the DAS Communication System.
         Provides three public alarm functions used across all modules
         to report KPI and system failures to the Network Management System (NMS).

Public API (call these from other modules):
    send_invalid_kpi_alarm()  — KPI has too many consecutive invalid samples
    send_threshold_alarm()    — time-averaged KPI has fallen below threshold
    send_runtime_alarm()      — system or modem-level failure with no band/KPI context
                                (e.g. COPS command failure, file write error, serial issue)

OID Structure:
    Enterprise root  :  1.3.6.1.4.1.12345
    ├── .1  Trap type OIDs — identify which alarm category this trap represents
    │     ├── .1  trapInvalidKPI    (1.3.6.1.4.1.12345.1.1)
    │     ├── .2  trapThresholdKPI  (1.3.6.1.4.1.12345.1.2)
    │     └── .3  trapRuntime       (1.3.6.1.4.1.12345.1.3)
    └── .2  Varbind OIDs — data fields carried inside each trap
          ├── .1.0  band       Integer32   — band number              (KPI traps only)
          ├── .2.0  kpi        OctetString — KPI name                 (KPI traps only)
          ├── .3.0  alarmType  OctetString — alarm category           (KPI traps only)
          ├── .4.0  detail     OctetString — human-readable detail    (all traps)
          └── .5.0  component  OctetString — failed system component  (runtime traps only)

Production checklist (must be done before live deployment):
    [ ] Set NMS_IP to the company NMS server IP (currently falls back to 10.8.0.1)
    [ ] Change NMS_PORT from 1162 (test) to 162 (production standard)
    [ ] Replace enterprise OID root 12345 with your assigned IANA PEN if applicable
    [ ] Change COMMUNITY from "public" to a deployment-specific community string

NOTE: SNMP trap delivery has been tested in the LTE-only (Mode C, no SIM) path.
      The trap format and OID structure have not been validated against the final
      NMS configuration — confirm with the network team before production deployment.
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
import json
import os
from constants import CONFIG_PATH
import time


# ══════════════════════════════════════════════════════════════════════════════
# NMS Configuration
# Loads the SNMP manager IP from the GUI config file at module import time.
# All other SNMP parameters are hardcoded here — see production checklist above.
# ══════════════════════════════════════════════════════════════════════════════
_FALLBACK_IP         = "10.8.0.1"  # Used when config cannot be read — VPN gateway default
_RETRY_SLEEP_SECONDS = 2           # Seconds to wait before retrying a failed config read

# SNMP_READY is exported and read by file_manager.py to decide whether to show
# "RUNNING" or "DOWN" in the GUI snmp_status field. Set to False on any config
# load failure so the GUI accurately reflects that SNMP may not be functional.
SNMP_READY = True

# Attempt to load the NMS IP from the config file at import time.
# Each exception type is handled separately because the correct fallback
# strategy differs — OSError may be transient (retry once), while
# JSONDecodeError and KeyError are deterministic failures (no retry).
try:
    with open(CONFIG_PATH, "r") as f:
        _config = json.load(f)
    NMS_IP = _config["snmp_host"]

except OSError as e:
    # Transient hardware glitch — retry once.
    print(f"[SNMP WARNING] Config read OSError: {e} — retrying in {_RETRY_SLEEP_SECONDS}s...")
    time.sleep(_RETRY_SLEEP_SECONDS)
    try:
        with open(CONFIG_PATH, "r") as f:
            _config = json.load(f)
        NMS_IP = _config["snmp_host"]
        print(f"[SNMP] Config read retry succeeded.")
    except Exception as retry_e:
        # Retry also failed — fall back to the hardcoded VPN gateway IP.
        # Traps will still be attempted but may not reach the NMS.
        print(f"[SNMP WARNING] Config read retry failed: {retry_e} — using fallback NMS_IP {_FALLBACK_IP}")
        NMS_IP     = _FALLBACK_IP
        SNMP_READY = False

except json.JSONDecodeError as e:
    # Corrupt file — retrying reads the same corrupt content, pointless.
    print(f"[SNMP WARNING] Config file malformed (JSONDecodeError): {e} — using fallback NMS_IP {_FALLBACK_IP}")
    NMS_IP     = _FALLBACK_IP
    SNMP_READY = False

except KeyError:
    # snmp_host key missing — operator misconfiguration.
    print(f"[SNMP WARNING] 'snmp_host' missing from config — using fallback NMS_IP {_FALLBACK_IP}")
    NMS_IP     = _FALLBACK_IP
    SNMP_READY = False

except PermissionError as e:
    # Permission error — retrying won't fix it.
    print(f"[SNMP WARNING] Config read PermissionError: {e} — using fallback NMS_IP {_FALLBACK_IP}")
    NMS_IP     = _FALLBACK_IP
    SNMP_READY = False

except Exception as e:
    print(f"[SNMP WARNING] Config read unexpected error ({type(e).__name__}): {e} — using fallback NMS_IP {_FALLBACK_IP}")
    NMS_IP     = _FALLBACK_IP
    SNMP_READY = False

NMS_PORT  = 1162      # Trap destination port 
COMMUNITY = "public"  # SNMPv2c community string 

# ── OID Definitions ───────────────────────────────────────────────────────────
# Enterprise root: 1.3.6.1.4.1.12345
# Replace 12345 with your assigned PEN for production.

# Trap type OIDs — identify which kind of alarm this trap represents
OID_TRAP_INVALID   = "1.3.6.1.4.1.12345.1.1"   # trapInvalidKPI
OID_TRAP_THRESHOLD = "1.3.6.1.4.1.12345.1.2"   # trapThresholdKPI
OID_TRAP_RUNTIME   = "1.3.6.1.4.1.12345.1.3"   # trapRuntime

# Varbind OIDs — the data fields carried inside each trap
OID_VAR_BAND      = "1.3.6.1.4.1.12345.2.1.0"  # band number (Integer32)
OID_VAR_KPI       = "1.3.6.1.4.1.12345.2.2.0"  # KPI name (OctetString)
OID_VAR_ALARM     = "1.3.6.1.4.1.12345.2.3.0"  # alarm category (OctetString)
OID_VAR_DETAIL    = "1.3.6.1.4.1.12345.2.4.0"  # human-readable detail (OctetString)
OID_VAR_COMPONENT = "1.3.6.1.4.1.12345.2.5.0"  # failed component (OctetString)


# ══════════════════════════════════════════════════════════════════════════════
# Internal: KPI Trap Sender (band + kpi context)
# ══════════════════════════════════════════════════════════════════════════════

# ── KPI Trap (band + KPI context) ────────────────────────────────
async def _send_kpi_trap_async(
    trap_oid:   str,
    band:       int,
    kpi:        str,
    alarm_type: str,
    detail:     str,
) -> None:
    """
    Async core that builds and sends one SNMPv2c KPI trap.
    Not called directly — use send_invalid_kpi_alarm() or send_threshold_alarm().

    Varbinds sent:
        OID_VAR_BAND   — band number (Integer32)
        OID_VAR_KPI    — KPI name string (OctetString)
        OID_VAR_ALARM  — alarm category string (OctetString)
        OID_VAR_DETAIL — human-readable detail string (OctetString)

    Args:
        trap_oid:   OID identifying the trap type (INVALID or THRESHOLD).
        band:       Band number the alarm applies to.
        kpi:        KPI name string (e.g. "RSRP", "SS_SINR").
        alarm_type: Alarm category string ("INVALID" or "THRESHOLD").
        detail:     Human-readable description of the alarm condition.
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

    # closeDispatcher() releases the asyncio transport created by SnmpEngine.
    # Required when using asyncio to prevent resource leak warnings.
    snmpEngine.closeDispatcher()

    # errorIndication covers transport-level failures (host unreachable, timeout).
    # errorStatus covers SNMP protocol-level errors from the agent/NMS.
    # Neither raises an exception — pysnmp returns them as return values.
    if errorIndication:
        print(f"[SNMP ERROR] KPI trap failed ({alarm_type} | Band {band} | {kpi}): {errorIndication}")
    elif errorStatus:
        print(f"[SNMP ERROR] KPI trap failed ({alarm_type} | Band {band} | {kpi}): {errorStatus.prettyPrint()}")
    else:
        print(f"[SNMP SENT]  {alarm_type} | Band {band} | {kpi} | {detail}")


def _send_kpi_trap(trap_oid: str, band: int, kpi: str, alarm_type: str, detail: str) -> None:
    """
    Synchronous wrapper around _send_kpi_trap_async().
    Runs the async function in a new event loop via asyncio.run() so callers
    do not need to use async/await. Blocks until the trap is sent or fails.
    """
    asyncio.run(_send_kpi_trap_async(trap_oid, band, kpi, alarm_type, detail))


# ── Runtime Trap (system/modem context, no band or KPI) ──────────
async def _send_runtime_trap_async(component: str, detail: str) -> None:
    """
    Async core that builds and sends one SNMPv2c runtime trap.
    Not called directly — use send_runtime_alarm().

    Used for system and modem-level failures that have no band or KPI context
    (e.g. COPS command failure, file write error, Pi restart, serial port loss).

    Varbinds sent:
        OID_VAR_COMPONENT — what failed (e.g. "AT+COPS=0", "GUI JSON write")
        OID_VAR_DETAIL    — what happened (e.g. "no response after 120s")

    Note: band, kpi, and alarmType varbinds are intentionally omitted —
    runtime traps have no RF context and using the KPI OIDs would be misleading.

    Args:
        component: The system component or command that failed.
        detail:    Human-readable description of the failure condition.
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
    """
    Synchronous wrapper around _send_runtime_trap_async().
    Runs the async function in a new event loop via asyncio.run() so callers
    do not need to use async/await. Blocks until the trap is sent or fails.
    """
    asyncio.run(_send_runtime_trap_async(component, detail))


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# These are the only functions that should be imported and called by other
# modules. They validate/format their arguments and delegate to the internal
# senders in Section C.
# ══════════════════════════════════════════════════════════════════════════════

def send_invalid_kpi_alarm(band: int, kpi: str, invalid_count: int) -> None:
    """
    Send an SNMP trap reporting a KPI with too many consecutive invalid samples.

    Called by check_kpi() in alarms.py when the last 3 of 5 samples for a
    given KPI on a given band all exceed the INVALID_SENTINEL threshold (500).
    The trap uses OID_TRAP_INVALID so the NMS can distinguish it from a
    threshold violation.

    Args:
        band:          Band number the invalid KPI belongs to.
        kpi:           KPI name string (e.g. "RSRP", "RSRQ", "SS_SINR").
        invalid_count: Number of samples in the window that exceeded
                       INVALID_SENTINEL — included in the detail string
                       for operator context.
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
    Send an SNMP trap reporting a KPI whose 5-sample average is below threshold.

    Called by check_kpi() in alarms.py when a valid average is computed but
    falls below the deployment threshold for that KPI. The trap uses
    OID_TRAP_THRESHOLD so the NMS can distinguish it from an invalid-data alarm.

    Args:
        band:      Band number the threshold violation applies to.
        kpi:       KPI name string (e.g. "SINR", "RSRP", "SS_RSRQ").
        avg_value: The computed 5-sample average that fell below threshold.
        threshold: The minimum acceptable value that was not met.
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

    This is the general-purpose alarm for any failure that has no band or KPI
    context. Import and call this from any module when a command, file
    operation, or system component fails and the NMS needs to be notified.

    The trap uses OID_TRAP_RUNTIME, which carries only component and detail
    varbinds — no band, kpi, or alarmType fields are included since those
    concepts don't apply to system-level events.

    Usage examples:
        send_runtime_alarm("AT+COPS=0",      "no modem response after 120s — retrying")
        send_runtime_alarm("AT+CFUN=1",      "modem returned ERROR after 3 attempts")
        send_runtime_alarm("GUI JSON write",  "disk full — GUI not updated this cycle")
        send_runtime_alarm("serial port",     "SerialException — USB may be disconnected")
        send_runtime_alarm("Pi restart",      "modem unresponsive after 300s — rebooting")

    Args:
        component: The command, module, or system component that failed.
                   Keep it short and consistent — the NMS may filter or
                   group alarms by this field.
        detail:    Human-readable description of what went wrong and any
                   recovery action being taken.
    """
    _send_runtime_trap(component=component, detail=detail)


