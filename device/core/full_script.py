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
from standin_kpi_collection import (
    instKPIcollection,
    send_at_command_with_retry,
    send_cops_command_until_success,
    send_cfun_until_success,
    _trigger_modem_restart
)
from alarms import process_window
from file_manager import update_gui_json, append_to_daily_file
from models import LTEKPI, NR5GKPI, SamplingSession, AveragedLTEKPI, AveragedNR5GKPI
from snmpSend import send_invalid_kpi_alarm, send_runtime_alarm, send_threshold_alarm
from constants import (
    AT_CMD_FULL_FUNCTIONALITY,
    AT_CMD_COPS_AUTO,
    SAMPLES_PER_SESSION,
    SAMPLE_INTERVAL_SECONDS,
)
from datetime import datetime, timedelta
import time
import subprocess

CONFIG_PATH = Path("/home/das/DAS-Communication-System/device/GUI/config.json")

REQUIRED_FIELDS = [
    "site_name", "device_id", "poll_interval",
    "snmp_host",  
    "rssi_threshold_min", 
    "rsrp_threshold_min", 
    "rsrq_threshold_min", 
    "sinr_threshold_min", 
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

def start_vpn(ovpn_path):
    print("[VPN] Starting OpenVPN...")
    process = subprocess.Popen(
        ["sudo", "openvpn", "--config", ovpn_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Give it time to connect
    time.sleep(10)
    print("[VPN] OpenVPN should be connected.")
    return process

# Start VPN BEFORE everything else
vpn_process = start_vpn("/home/das/DAS-Communication-System/device/GUI/vpn/client.ovpn")




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

# ── AT+CFUN=1 — Full modem reset ─────────────────────────────────────────────
# Critical — the script cannot safely proceed until the modem confirms full
# functionality. Uses indefinite retry with SNMP alert at 120s so the operator
# is notified if the modem is unresponsive at startup.
print("[STARTUP] Sending AT+CFUN=1 — full modem reset...")
send_cfun_until_success("AT+CFUN=1", timeout=15)
print("[STARTUP] AT+CFUN=1 accepted — waiting 15 seconds for modem to fully boot...")
time.sleep(15)

# ── AT+QNWPREFCFG — Mode preference ──────────────────────────────────────────
# Less critical than CFUN — if this fails the modem may already be in AUTO
# mode from a previous run. Retried with send_at_command_with_retry (3 attempts),
# then a trap is sent and startup continues rather than halting indefinitely.
print("[STARTUP] Setting mode preference to AUTO (LTE + NR5G)...")
try:
    send_at_command_with_retry('AT+QNWPREFCFG="mode_pref",AUTO', 3)
    print("[STARTUP] Mode preference set to AUTO.")
except Exception as e:
    print(f"[STARTUP] AT+QNWPREFCFG failed after retries: {e} — "
          f"modem may already be in correct mode, continuing.")
    send_runtime_alarm(
        "AT+QNWPREFCFG",
        f"Mode preference command failed at startup: {e}. "
        f"Modem may already be in AUTO mode — continuing."
    )

print("[STARTUP] Waiting 10 seconds for mode switch to settle...")
time.sleep(10)

# ── AT+COPS=0 — Auto-registration ────────────────────────────────────────────
# Critical — uses indefinite retry matching the pattern used inside
# instKPIcollection so startup behavior is consistent with runtime behavior.
print("[STARTUP] Sending AT+COPS=0 — enabling auto-registration...")
send_cops_command_until_success(AT_CMD_COPS_AUTO, timeout=180)
print("[STARTUP] AT+COPS=0 accepted — waiting 10 seconds for registration to settle...")
time.sleep(10)

print("[STARTUP] Modem initialized — beginning collection loop.")



# ══════════════════════════════════════════════════════════════════════════════
# Main Collection Loop
# ══════════════════════════════════════════════════════════════════════════════
sessions      = []   # Holds up to 5 SamplingSession objects before averaging
session_count = 0    # Tracks which session we are on (1–5)

# ── Option B: Consecutive command failure session tracking ────────────────────
# Counts consecutive sessions where every band failed via AT command exception
# rather than simply not finding a cell. If all bands fail via command error
# across this many consecutive sessions, the modem is not processing commands
# at all and a Pi restart is the only recovery.
# Resets to 0 any time a session has at least one band that responded normally
# (either a valid reading or a SEARCH-state dummy — both mean AT comms work).
_CONSECUTIVE_FAILURE_RESTART_THRESHOLD = 2
_consecutive_command_failure_sessions  = 0

while True:

    session_count += 1
    print(f"\n[MAIN] Starting session {session_count} of {SAMPLES_PER_SESSION}")
    session_start = datetime.now()

    # ── Collection ────────────────────────────────────────────────────────────
    # instKPIcollection is wrapped in its own try/except separate from the
    # processing block below. This is intentional — a failure here should
    # preserve any sessions already accumulated in this window rather than
    # wiping the counter and starting over.
    #
    # On unexpected failure, a fully-typed dummy session is built and appended
    # in place of the real one rather than decrementing session_count and
    # skipping the slot. This is intentional for two reasons:
    #
    #   1. Structure integrity: process_window expects exactly 5 sessions, each
    #      containing one typed KPI object per configured band in order. Inserting
    #      a dummy that mirrors this structure exactly means process_window and
    #      check_kpi receive valid typed objects regardless of what failed here.
    #      No special-case handling is needed anywhere downstream.
    #
    #   2. Alarm propagation: all dummy values are set to 9999, which is above
    #      INVALID_SENTINEL in alarms.py. This means the invalid alarm path in
    #      check_kpi fires naturally for every KPI on every band in this session
    #      — the operator is alerted through the normal alarm mechanism without
    #      any additional logic needed here.
    #
    # Band order in dummy_readings must match instKPIcollection exactly:
    # NR5G bands first, LTE bands second. process_window uses positional index
    # to match readings across all 5 sessions, so a mismatch here would silently
    # pair the wrong bands together during averaging.
    try:
        session, cmd_failures = instKPIcollection(nr5g_bands, lte_bands)
        sessions.append(session)
        print(f"[MAIN] Session {session_count} collected — "
              f"{len(session.readings)} bands read, "
              f"{cmd_failures} band(s) failed via AT command error.")

        total_bands = len(nr5g_bands) + len(lte_bands)

        if total_bands > 0 and cmd_failures == total_bands:
            # Every single band failed via AT command exception — not a coverage
            # issue, this means the modem rejected or didn't respond to every
            # configuration command this session.
            _consecutive_command_failure_sessions += 1
            print(f"[MAIN] All bands failed via command error — "
                  f"consecutive failure sessions: "
                  f"{_consecutive_command_failure_sessions}/"
                  f"{_CONSECUTIVE_FAILURE_RESTART_THRESHOLD}")

            if _consecutive_command_failure_sessions >= _CONSECUTIVE_FAILURE_RESTART_THRESHOLD:
                _trigger_modem_restart(
                    f"All bands failed via AT command error for "
                    f"{_consecutive_command_failure_sessions} consecutive sessions — "
                    f"modem is not processing commands. Restarting Pi to recover."
                )
        else:
            # At least one band had a normal AT response this session —
            # reset the consecutive failure counter.
            if _consecutive_command_failure_sessions > 0:
                print(f"[MAIN] AT comms recovered — resetting consecutive failure counter.")
            _consecutive_command_failure_sessions = 0

    except Exception as e:
        print(f"[MAIN] Session {session_count} collection failed unexpectedly: {e} "
              f"— building dummy session to maintain window integrity.")
        send_runtime_alarm(
            "instKPIcollection",
            f"Unexpected failure on session {session_count}: {e}. "
            f"Dummy session inserted — all bands will read as invalid this window."
        )

        dummy_readings = []

        # NR5G dummy readings — one per configured band, sentinel values throughout.
        # int(band[1:]) strips the 'n' prefix to match how instKPIcollection
        # builds real readings e.g. 'n2' → 2.
        for band in nr5g_bands:
            dummy_readings.append(NR5GKPI(
                timestamp = session_start,
                rat       = "NR5G",
                band      = int(band[1:]),
                pci       = 9999,
                arfcn     = 9999,
                ss_rsrp   = 9999,
                ss_rsrq   = 9999,
                ss_sinr   = 9999,
            ))

        # LTE dummy readings — one per configured band, sentinel values throughout.
        # int(band[1:]) strips the 'b' prefix e.g. 'b12' → 12.
        for band in lte_bands:
            dummy_readings.append(LTEKPI(
                timestamp = session_start,
                rat       = "LTE",
                band      = int(band[1:]),
                pci       = 9999,
                earfcn    = 9999,
                rsrp      = 9999,
                rsrq      = 9999,
                rssi      = 9999,
                sinr      = 9999,
            ))

        session = SamplingSession(
            session_start = session_start,
            readings      = dummy_readings,
        )
        sessions.append(session)
        print(f"[MAIN] Dummy session inserted for session {session_count} — "
              f"{len(dummy_readings)} bands set to sentinel 9999.")

    # ── Window Processing ─────────────────────────────────────────────────────
    # Separated from the collection try/except above so a processing failure
    # does not interact with collection accounting. The finally block resets
    # the window unconditionally — whether processing succeeded or failed,
    # stale sessions must never carry over into the next window since they
    # would corrupt the averaging and alarm results for a fresh 5-session window.
    if session_count == SAMPLES_PER_SESSION:
        try:
            print(f"\n[MAIN] 5 sessions collected — running time averaging and alarms...")
            averaged_results = process_window(sessions, lte_thresholds, nr5g_thresholds)
            update_gui_json(averaged_results)
            append_to_daily_file(averaged_results)
            print(f"[MAIN] Window processed — resetting for next collection window.")

        except Exception as e:
            print(f"[MAIN] Window processing failed: {e} — resetting window.")
            send_runtime_alarm("process_window", f"Window processing failed: {e}")

        finally:
            # Always resets regardless of outcome above — success, exception,
            # or any other exit path from the try/except.
            sessions      = []
            session_count = 0

    # ── Timing ────────────────────────────────────────────────────────────────
    # session_start was captured before instKPIcollection ran so collection
    # time, processing time, and dummy session build time are all accounted
    # for in the remaining sleep. This block always runs regardless of what
    # happened above so the sample schedule is never disrupted by a failure.
    next_session   = session_start + timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    sleep_duration = (next_session - datetime.now()).total_seconds()

    if sleep_duration > 0:
        print(f"[MAIN] Sleeping {sleep_duration:.1f}s until next session...")
        time.sleep(sleep_duration)
    else:
        # Collection or processing overran the window — continue immediately
        # without sleeping. The overrun is logged so the operator can identify
        # if collection is consistently taking longer than SAMPLE_INTERVAL_SECONDS.
        print(f"[MAIN] Warning: overran window by {abs(sleep_duration):.1f}s — "
              f"starting next session immediately.")
