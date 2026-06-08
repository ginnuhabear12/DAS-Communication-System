"""
snmpTest.py - Quick SNMP Trap Connectivity Test
------------------------------------------------
Place this file in the same folder as snmpSend.py and run it.
Watch the pfSense packet capture on OPT1 (ovpns1) for UDP port 1162.

Usage:
    python snmpTest.py
"""

from snmpSend import send_invalid_kpi_alarm, send_threshold_alarm, send_runtime_alarm
import time

print("=" * 55)
print("  SNMP TRAP CONNECTIVITY TEST")
print("  Target: 10.8.0.1 : 1162")
print("=" * 55)

# ── Test 1: Invalid KPI Trap ──────────────────────────────
print("\n[1/3] Sending INVALID KPI trap...")
send_invalid_kpi_alarm(band=1, kpi="RSRP", invalid_count=3)
time.sleep(1)

# ── Test 2: Threshold Breach Trap ─────────────────────────
print("\n[2/3] Sending THRESHOLD trap...")
send_threshold_alarm(band=1, kpi="SINR", avg_value=-5.2, threshold=0.0)
time.sleep(1)

# ── Test 3: Runtime Alarm Trap ────────────────────────────
print("\n[3/3] Sending RUNTIME trap...")
send_runtime_alarm(component="snmpTest.py", detail="VPN connectivity test - traps working")

print("\n" + "=" * 55)
print("  All 3 traps fired.")
print("  Check pfSense packet capture on OPT1 for activity.")
print("=" * 55)