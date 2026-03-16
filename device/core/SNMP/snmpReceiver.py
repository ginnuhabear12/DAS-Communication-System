"""
snmpReceiver.py — SNMP Trap Receiver for DAS Communication System
-----------------------------------------------------------------
Listens for SNMPv2c traps sent from the Raspberry Pi and decodes
them into human-readable output.

Varbind OIDs expected (from snmpSend.py):
    1.3.6.1.4.1.12345.2.1.0  — band        (Integer32)
    1.3.6.1.4.1.12345.2.2.0  — KPI name    (OctetString)
    1.3.6.1.4.1.12345.2.3.0  — alarm type  (OctetString)
    1.3.6.1.4.1.12345.2.4.0  — detail msg  (OctetString)

No pysnmp manager/dispatcher needed — raw socket receive +
pysnmp BER decoder handles everything.
"""

import socket
from datetime import datetime

from pysnmp.proto import api as snmp_api

# ── Configuration ─────────────────────────────────────────────────────────────
LISTEN_IP   = "0.0.0.0"
LISTEN_PORT = 1162        # Change to 162 in production

# ── Our varbind OIDs (must match snmpSend.py) ─────────────────────────────────
OID_BAND   = (1, 3, 6, 1, 4, 1, 12345, 2, 1, 0)
OID_KPI    = (1, 3, 6, 1, 4, 1, 12345, 2, 2, 0)
OID_ALARM  = (1, 3, 6, 1, 4, 1, 12345, 2, 3, 0)
OID_DETAIL = (1, 3, 6, 1, 4, 1, 12345, 2, 4, 0)

# ── Trap OID suffixes for label lookup ────────────────────────────────────────
TRAP_LABELS = {
    (1, 3, 6, 1, 4, 1, 12345, 1, 1): "INVALID KPI",
    (1, 3, 6, 1, 4, 1, 12345, 1, 2): "THRESHOLD BREACH",
}


def decode_trap(data: bytes, addr: tuple) -> None:
    """
    Decode a raw SNMPv2c trap UDP payload and print human-readable output.

    Args:
        data: Raw UDP payload bytes.
        addr: (ip, port) tuple of the sender.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Detect SNMP version from the message wrapper ──────────────────────────
    try:
        msg_ver = int(snmp_api.decodeMessageVersion(data))
    except Exception as e:
        print(f"[{timestamp}] Could not determine SNMP version: {e}")
        return

    # Select the correct protocol module (we expect v2c = version index 1)
    if msg_ver in snmp_api.protoModules:
        proto = snmp_api.protoModules[msg_ver]
    else:
        print(f"[{timestamp}] Unsupported SNMP version: {msg_ver}")
        return

    # ── Decode the full message ───────────────────────────────────────────────
    req_msg, remainder = proto.apiFrame.decodeMessageVersion(data)
    pdu = proto.apiFrame.getPDU(req_msg)

    # ── Extract trap OID (SNMPTrap-v2 snmpTrapOID.0) ─────────────────────────
    trap_oid_str = "unknown"
    trap_label   = "UNKNOWN"

    var_bind_table = proto.apiPDU.getVarBindList(pdu)

    for var_bind in var_bind_table:
        oid, val = proto.apiVarBind.getOIDVal(var_bind)
        oid_tuple = tuple(oid)

        # snmpTrapOID.0 = 1.3.6.1.6.3.1.1.4.1.0
        if oid_tuple == (1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0):
            trap_oid_tuple = tuple(val)
            trap_label = TRAP_LABELS.get(trap_oid_tuple, f"OID {'.'.join(str(x) for x in trap_oid_tuple)}")
            break

    # ── Extract our custom varbinds ───────────────────────────────────────────
    varbind_map = {}
    for var_bind in var_bind_table:
        oid, val = proto.apiVarBind.getOIDVal(var_bind)
        oid_tuple = tuple(oid)
        if oid_tuple in (OID_BAND, OID_KPI, OID_ALARM, OID_DETAIL):
            varbind_map[oid_tuple] = str(val)

    band   = varbind_map.get(OID_BAND,   "?")
    kpi    = varbind_map.get(OID_KPI,    "?")
    alarm  = varbind_map.get(OID_ALARM,  "?")
    detail = varbind_map.get(OID_DETAIL, "?")

    # ── Print formatted output ────────────────────────────────────────────────
    print(f"\n{'─' * 55}")
    print(f"  TRAP RECEIVED  [{timestamp}]")
    print(f"{'─' * 55}")
    print(f"  From       : {addr[0]}:{addr[1]}")
    print(f"  Trap Type  : {trap_label}")
    print(f"  Band       : {band}")
    print(f"  KPI        : {kpi}")
    print(f"  Alarm      : {alarm}")
    print(f"  Detail     : {detail}")
    print(f"{'─' * 55}")


# ── Main listener loop ────────────────────────────────────────────────────────
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((LISTEN_IP, LISTEN_PORT))

print(f"UDP listener active on {LISTEN_IP}:{LISTEN_PORT} ... waiting")

try:
    while True:
        data, addr = sock.recvfrom(4096)
        try:
            decode_trap(data, addr)
        except Exception as e:
            # Fallback — if decode fails for any reason, show raw bytes
            print(f"\n[DECODE ERROR] {e}")
            print(f"  Raw bytes: {data.hex()}")
except KeyboardInterrupt:
    print("\nListener stopped.")
    sock.close()