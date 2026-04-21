"""
Module: full_script.py
Purpose: Main entry point — runs the full DAS verification pipeline.
         Handles startup, KPI collection, time averaging, alarms, and file updates.
Dependencies: standin_kpi_collection.py, alarms.py, file_manager.py, 
              models.py, constants.py, modem.py, snmpSend.py
Author:
"""


import json
from pathlib import Path
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


CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "device/testing/GUItest/config.json"

REQUIRED_FIELDS = [
    "site_name", "device_id", "poll_interval",
    "snmp_host", "snmp_community", "rat",
    "rssi_threshold_min", "rssi_threshold_max",
    "rsrp_threshold_min", "rsrp_threshold_max",
    "rsrq_threshold_min", "rsrq_threshold_max",
    "sinr_threshold_min", "sinr_threshold_max",
]

def load_config():
    """Load config.json and wait until all required fields are filled."""
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)

            # Find any missing or null fields
            missing = [k for k in REQUIRED_FIELDS if cfg.get(k) is None or cfg.get(k) == ""]

            # Also check RAT-specific fields
            if cfg.get("rat") == "LTE" and not cfg.get("earfcn"):
                missing.append("earfcn")
            elif cfg.get("rat") == "5G" and not cfg.get("nr_band"):
                missing.append("nr_band")

            if missing:
                print(f"[CONFIG] Waiting for missing fields: {missing}")
                time.sleep(5)
                continue

            print(f"[CONFIG] Config loaded successfully.")
            return cfg

        except FileNotFoundError:
            print("[CONFIG] config.json not found — retrying in 5s...")
            time.sleep(5)
        except json.JSONDecodeError:
            print("[CONFIG] config.json is invalid JSON — retrying in 5s...")
            time.sleep(5)

# ══════════════════════════════════════════════════════════════════════════════
# Load Config — wait until all fields are filled
# ══════════════════════════════════════════════════════════════════════════════
print("[STARTUP] Loading config...")
cfg = load_config()

# Apply config values
site_name      = cfg["site_name"]
device_id      = cfg["device_id"]
snmp_host      = cfg["snmp_host"]
snmp_community = cfg["snmp_community"]

# Build band lists from config
if cfg["rat"] == "LTE":
    lte_bands  = [f"b{cfg['earfcn']}"]   # adjust format to match your modem commands
    nr5g_bands = []
elif cfg["rat"] == "5G":
    nr5g_bands = [cfg["nr_band"]]
    lte_bands  = []

# Build thresholds from config
lte_thresholds = {
    "rssi": cfg["rssi_threshold_min"],   # or use min/max as needed
    "rsrp": cfg["rsrp_threshold_min"],
    "rsrq": cfg["rsrq_threshold_min"],
    "sinr": cfg["sinr_threshold_min"],
}

nr5g_thresholds = {
    "ss_rsrp": cfg["rsrp_threshold_min"],
    "ss_rsrq": cfg["rsrq_threshold_min"],
    "ss_sinr": cfg["sinr_threshold_min"],
}

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