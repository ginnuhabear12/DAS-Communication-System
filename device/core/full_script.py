"""
Module: full_script.py
Purpose: Main entry point — runs the full DAS verification pipeline.
         Handles startup, KPI collection, time averaging, alarms, and file updates.
Dependencies: standin_kpi_collection.py, alarms.py, file_manager.py, 
              models.py, constants.py, modem.py, snmpSend.py
Author:
"""

from standin_kpi_collection import instKPIcollection, send_at_command_with_retry
from alarms import process_window
from file_manager import update_gui_json, append_to_daily_file
from models import LTEKPI, NR5GKPI, SamplingSession, AveragedLTEKPI, AveragedNR5GKPI
from snmpSend import send_invalid_kpi_alarm, send_threshold_alarm
from constants import (
    AT_CMD_FULL_FUNCTIONALITY,
    AT_CMD_COPS_AUTO,
    SAMPLES_PER_SESSION,
    SAMPLE_INTERVAL_SECONDS,
)
from datetime import datetime, timedelta
import time


# ══════════════════════════════════════════════════════════════════════════════
# Startup — Modem Initialization
# ══════════════════════════════════════════════════════════════════════════════

print("[STARTUP] Sending AT+CFUN=1 — full modem reset...")
send_at_command_with_retry(AT_CMD_FULL_FUNCTIONALITY, 15)

print("[STARTUP] Waiting 15 seconds for modem to fully boot...")
time.sleep(15)

print("[STARTUP] Setting mode preference to AUTO (LTE + NR5G)...")
send_at_command_with_retry('AT+QNWPREFCFG="mode_pref",AUTO', 3)

print("[STARTUP] Waiting 10 seconds for mode switch to settle...")
time.sleep(10)

print("[STARTUP] Sending AT+COPS=0 — enabling auto-registration...")
send_at_command_with_retry(AT_CMD_COPS_AUTO, 180)

print("[STARTUP] Waiting 10 seconds for registration to settle...")
time.sleep(10)

print("[STARTUP] Modem initialized — beginning collection loop.")

# ══════════════════════════════════════════════════════════════════════════════
# Band Configuration — hardcoded for testing
# ══════════════════════════════════════════════════════════════════════════════
nr5g_bands = []   # e.g. ['n2', 'n66'] — fill in before testing
lte_bands  = []   # e.g. ['b2', 'b5', 'b12'] — fill in before testing

# ── Thresholds — set before testing ──────────────────────────────────────────
lte_thresholds = {
    "rsrp": -110.0,
    "rsrq": -15.0,
    "rssi": -95.0,
    "sinr": 0.0,
}

nr5g_thresholds = {
    "ss_rsrp": -110.0,
    "ss_rsrq": -15.0,
    "ss_sinr": 0.0,
}

# ══════════════════════════════════════════════════════════════════════════════
# Main Collection Loop
# ══════════════════════════════════════════════════════════════════════════════
sessions      = []   # Holds up to 5 SamplingSession objects before averaging
session_count = 0    # Tracks which session we are on (1–5)

while True:

    session_count += 1
    print(f"\n[MAIN] Starting session {session_count} of {SAMPLES_PER_SESSION}")

    # Record session start time so we can calculate sleep duration after collection.
    # This is captured before the call so collection time is accounted for in the wait.
    session_start = datetime.now()

    # Run one full KPI collection pass across all configured bands.
    # Returns a SamplingSession containing one reading per band in order.
    session = instKPIcollection(nr5g_bands, lte_bands)
    sessions.append(session)
    print(f"[MAIN] Session {session_count} collected — {len(session.readings)} bands read.")

    # ── Session 5 Processing ──────────────────────────────────────────────────
    # Once 5 sessions are collected, run time averaging, alarms, and file writes.
    # After processing, reset the sessions list and counter for the next window.
    if session_count == SAMPLES_PER_SESSION:
        print(f"\n[MAIN] 5 sessions collected — running time averaging and alarms...")

        averaged_results = process_window(sessions, lte_thresholds, nr5g_thresholds)
        update_gui_json(averaged_results)
        append_to_daily_file(averaged_results)

        print(f"[MAIN] Window processed — resetting for next collection window.")

        # Reset for the next 5-session window
        sessions      = []
        session_count = 0

    # ── Timing ────────────────────────────────────────────────────────────────
    # Calculate how long to sleep until the next minute mark.
    # session_start was captured before instKPIcollection ran, so collection
    # time and processing time are both accounted for in the remaining sleep.
    next_session   = session_start + timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    sleep_duration = (next_session - datetime.now()).total_seconds()

    if sleep_duration > 0:
        print(f"[MAIN] Sleeping {sleep_duration:.1f}s until next session...")
        time.sleep(sleep_duration)
    else:
        # Collection or processing overran the minute window.
        # Log it and continue immediately without sleeping.
        print(f"[MAIN] Warning: overran window by {abs(sleep_duration):.1f}s — starting next session immediately.")