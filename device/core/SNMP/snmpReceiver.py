"""
snmpReceiver.py — SNMP Trap Receiver for DAS Communication System
-----------------------------------------------------------------
Listens for SNMPv2c traps sent from the Raspberry Pi and decodes
them into human-readable output.

Compatible with pysnmp 6.x — uses pyasn1 BER decoder with v2c.Message()
spec instead of the removed apiFrame attribute.

Varbind OIDs expected (from snmpSend.py):
    1.3.6.1.4.1.12345.2.1.0  — band        (Integer32)
    1.3.6.1.4.1.12345.2.2.0  — KPI name    (OctetString)
    1.3.6.1.4.1.12345.2.3.0  — alarm type  (OctetString)
    1.3.6.1.4.1.12345.2.4.0  — detail msg  (OctetString)
"""

import socket
from datetime import datetime

<<<<<<< HEAD
# LISTEN_IP = "0.0.0.0"
# LISTEN_PORT = 9162
=======
#LISTEN_IP = "0.0.0.0"
#LISTEN_PORT = 1162
>>>>>>> d5069a0fb745b8b8e3987977b5a167a9772dae5f

from pyasn1.codec.ber import decoder as ber_decoder
from pysnmp.proto.api import v2c


# ── Configuration ─────────────────────────────────────────────────────────────
LISTEN_IP   = "0.0.0.0"
LISTEN_PORT = 1162        # Change to 162 in production

# ── Our varbind OIDs (must match snmpSend.py) ─────────────────────────────────
OID_BAND   = (1, 3, 6, 1, 4, 1, 12345, 2, 1, 0)
OID_KPI    = (1, 3, 6, 1, 4, 1, 12345, 2, 2, 0)
OID_ALARM  = (1, 3, 6, 1, 4, 1, 12345, 2, 3, 0)
OID_DETAIL = (1, 3, 6, 1, 4, 1, 12345, 2, 4, 0)

# ── Trap OID → label mapping ──────────────────────────────────────────────────
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

    # ── Decode the raw bytes using pyasn1 BER decoder with v2c Message spec ──
    msg, _ = ber_decoder.decode(data, asn1Spec=v2c.Message())

    # ── Extract PDU and varbind list ──────────────────────────────────────────
    pdu       = v2c.apiMessage.getPDU(msg)
    var_binds = v2c.apiPDU.getVarBinds(pdu)

    # ── Walk varbinds to extract trap OID and our custom fields ──────────────
    trap_label  = "UNKNOWN"
    varbind_map = {}

    for oid, val in var_binds:
        oid_tuple = tuple(oid)

        # snmpTrapOID.0 carries the trap type OID as its value
        if oid_tuple == (1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0):
            trap_oid_tuple = tuple(val)
            trap_label = TRAP_LABELS.get(
                trap_oid_tuple,
                f"OID {'.'.join(str(x) for x in trap_oid_tuple)}"
            )

        # Our custom varbinds
        elif oid_tuple in (OID_BAND, OID_KPI, OID_ALARM, OID_DETAIL):
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
        print(f"Got packet from {addr}, {len(data)} bytes")
        try:
            decode_trap(data, addr)
        except Exception as e:
            print(f"\n[DECODE ERROR] {e}")
            print(f"  Raw bytes: {data.hex()}")
except KeyboardInterrupt:
    print("\nListener stopped.")
    sock.close()
