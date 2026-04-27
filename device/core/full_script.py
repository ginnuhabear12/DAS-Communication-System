"""
Module: full_script.py
Purpose: Main entry point — runs the full DAS verification pipeline.
         Handles startup, KPI collection, time averaging, alarms, and file updates.
Dependencies: standin_kpi_collection.py, alarms.py, file_manager.py, 
              models.py, constants.py, modem.py, snmpSend.py
Author:
"""


import json
from standin_kpi_collection import (
    instKPIcollection,
    send_at_command_with_retry,
    send_cops_command_until_success,
    send_cfun_until_success,
    _trigger_modem_restart,
    detect_sim
)
from alarms import process_window
from file_manager import update_gui_json, append_to_daily_file, update_vpn_status
from models import LTEKPI, NR5GKPI, SamplingSession, AveragedLTEKPI, AveragedNR5GKPI
from snmpSend import send_invalid_kpi_alarm, send_runtime_alarm, send_threshold_alarm
from constants import (
    AT_CMD_FULL_FUNCTIONALITY,
    AT_CMD_COPS_AUTO,
    SAMPLES_PER_SESSION,
    SAMPLE_INTERVAL_SECONDS,
    CONFIG_PATH
)
from datetime import datetime, timedelta
import time
import os
from modem import PORT
import subprocess

# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


REQUIRED_FIELDS = [
    "site_name", "device_id",
    "snmp_host", "monitored_bands",
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

            # Also check that monitored_bands is not an empty list
            if not cfg.get("monitored_bands"):
                missing.append("monitored_bands (no bands selected)")

            if missing:
                print(f"{_ts()} [CONFIG] Waiting for missing fields: {missing}")
                time.sleep(5)
                continue

            print(f"{_ts()} [CONFIG] Config loaded successfully.")
            return cfg

        except FileNotFoundError:
            print(f"{_ts()} [CONFIG] config.json not found — retrying in 5s...")
            time.sleep(5)
        except json.JSONDecodeError:
            print(f"{_ts()} [CONFIG] config.json is invalid JSON — retrying in 5s...")
            time.sleep(5)

# ══════════════════════════════════════════════════════════════════════════════
# Load Config — wait until all fields are filled
# ══════════════════════════════════════════════════════════════════════════════

def start_vpn(ovpn_path):
    print(f"{_ts()} [VPN] Starting OpenVPN...")
    process = subprocess.Popen(
        ["sudo", "openvpn", "--config", ovpn_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Give it time to connect
    time.sleep(10)
    print(f"{_ts()} [VPN] OpenVPN should be connected.")
    return process


def check_vpn_connected():
    """
    Check if VPN tunnel (tun0) is active.
    Returns: True if connected, False otherwise
    """
    try:
        result = subprocess.run(
            ["ip", "addr", "show", "tun0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_connected = result.returncode == 0
        status_str = "ACTIVE" if is_connected else "DOWN"
        print(f"{_ts()} [VPN] VPN check: {status_str} (returncode: {result.returncode})")
        return is_connected
    except Exception as e:
        print(f"{_ts()} [VPN] VPN check error: {e}")
        return False


def restart_vpn(ovpn_path):
    """
    Kill any existing OpenVPN process and restart VPN connection.
    """
    try:
        print(f"{_ts()} [VPN] Attempting to restart VPN...")
        
        # Kill existing OpenVPN process
        subprocess.run(
            ["sudo", "killall", "openvpn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        print(f"{_ts()} [VPN] Killed existing OpenVPN process(es).")
        
        # Wait a moment before restarting
        time.sleep(2)
        
        # Start new VPN process
        process = subprocess.Popen(
            ["sudo", "openvpn", "--config", ovpn_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Give it time to connect
        time.sleep(10)
        print(f"{_ts()} [VPN] OpenVPN restart initiated.")
        return process
        
    except Exception as e:
        print(f"{_ts()} [VPN] VPN restart failed: {e}")
        send_runtime_alarm(
            "VPN restart",
            f"Failed to restart VPN: {e}. Manual intervention may be required."
        )
        return None


def check_and_update_vpn_status(ovpn_path):
    """
    Check VPN connection status and update device_data.json.
    If VPN is down, attempt to restart it.
    """
    try:
        is_connected = check_vpn_connected()
        
        if is_connected:
            # VPN is active
            update_vpn_status("ACTIVE")
        else:
            # VPN is down - update status and attempt restart
            print(f"{_ts()} [VPN] VPN is DOWN - updating status and attempting restart...")
            update_vpn_status("DOWN")
            send_runtime_alarm(
                "VPN connection",
                "VPN tunnel (tun0) is down. Attempting automatic restart."
            )
            restart_vpn(ovpn_path)
            
            # Check again after restart attempt
            time.sleep(5)
            is_connected_after = check_vpn_connected()
            if is_connected_after:
                print(f"{_ts()} [VPN] VPN reconnected successfully!")
                update_vpn_status("ACTIVE")
            else:
                print(f"{_ts()} [VPN] VPN still down after restart attempt.")
                update_vpn_status("DOWN")
                send_runtime_alarm(
                    "VPN connection",
                    "VPN tunnel remains down after restart attempt. Check .ovpn file and network connectivity."
                )
                
    except Exception as e:
        print(f"{_ts()} [VPN] VPN status check/update error: {e}")
        send_runtime_alarm(
            "VPN status check",
            f"Error checking VPN status: {e}"
        )

# Start VPN BEFORE everything else
vpn_process = start_vpn("/home/das/DAS-Communication-System/device/GUI/vpn/client.ovpn")


print(f"{_ts()} [STARTUP] Loading config...")
cfg = load_config()

# Apply config values
site_name      = cfg["site_name"]
device_id      = cfg["device_id"]
snmp_host      = cfg["snmp_host"]
#snmp_community = cfg["snmp_community"]

# Build band lists from monitored_bands — 'b' prefix = LTE, 'n' prefix = NR5G
monitored_bands = cfg["monitored_bands"]
lte_bands  = [b for b in monitored_bands if b.startswith("b")]
nr5g_bands = [b for b in monitored_bands if b.startswith("n")]

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
# Works regardless of SIM state — hardware-level command.
print(f"{_ts()} [STARTUP] Sending AT+CFUN=1 — full modem reset...")
send_cfun_until_success("AT+CFUN=1", timeout=15)
print(f"{_ts()} [STARTUP] AT+CFUN=1 accepted — waiting 15 seconds for modem to fully boot...")
time.sleep(15)

# ── SIM Card Detection ────────────────────────────────────────────────────────
# Checked immediately after CFUN=1 confirms the modem is functional.
# AT+COPS=0 and AT+COPS=2 both return ERROR without a SIM, which would cause
# send_cops_command_until_success to retry indefinitely and hang the script.
# We detect SIM state here once and route all subsequent logic accordingly.
# The sim_present flag is re-evaluated at the top of each collection session
# so the device recovers automatically when a SIM is inserted.
print(f"{_ts()} [STARTUP] Checking for SIM card...")
sim_present = detect_sim()

if sim_present:
    print(f"{_ts()} [STARTUP] SIM detected — running full modem initialization.")

    # ── AT+QNWPREFCFG — Mode preference ──────────────────────────────────────
    print(f"{_ts()} [STARTUP] Setting mode preference to AUTO (LTE + NR5G)...")
    try:
        send_at_command_with_retry('AT+QNWPREFCFG="mode_pref",AUTO', 3)
        print(f"{_ts()} [STARTUP] Mode preference set to AUTO.")
    except Exception as e:
        print(f"{_ts()} [STARTUP] AT+QNWPREFCFG failed after retries: {e} — "
              f"modem may already be in correct mode, continuing.")
        send_runtime_alarm(
            "AT+QNWPREFCFG",
            f"Mode preference command failed at startup: {e}. "
            f"Modem may already be in AUTO mode — continuing."
        )

    print(f"{_ts()} [STARTUP] Waiting 10 seconds for mode switch to settle...")
    time.sleep(10)

    # ── AT+COPS=0 — Auto-registration ────────────────────────────────────────
    print(f"{_ts()} [STARTUP] Sending AT+COPS=0 — enabling auto-registration...")
    send_cops_command_until_success(AT_CMD_COPS_AUTO, timeout=180)
    print(f"{_ts()} [STARTUP] AT+COPS=0 accepted — waiting 10 seconds for registration to settle...")
    time.sleep(10)

else:
    # No SIM at startup — skip all COPS and QNWPREFCFG commands.
    # QNWPREFCFG might succeed without a SIM, but COPS will not, and running
    # COPS here would hang. We skip everything and let the collection loop
    # handle recovery via periodic SIM re-detection.
    print(f"{_ts()} [STARTUP] No SIM detected — skipping network initialization commands.")
    print(f"{_ts()} [STARTUP] Running in no-SIM mode. All KPI readings will be "
          f"invalid until a SIM is inserted. The device polls for SIM insertion "
          f"at the start of each collection session.")
    send_runtime_alarm(
        "SIM card",
        "No SIM card detected at startup. Network registration skipped. "
        "KPI readings will be invalid. Device will recover automatically "
        "when a SIM is inserted."
    )

print(f"{_ts()} [STARTUP] Modem initialized — beginning collection loop.")



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

_USB_SERIAL_FAILURE_THRESHOLD        = 2
_consecutive_serial_failure_sessions = 0

while True:

    session_count += 1
    print(f"\n{_ts()} [MAIN] Starting session {session_count} of {SAMPLES_PER_SESSION}")
    session_start = datetime.now()

    # ── SIM Re-check ─────────────────────────────────────────────────────────
    # In Mode C (no SIM): checks for SIM insertion before every session so
    # the device recovers automatically the moment a SIM is installed without
    # requiring a restart. When a SIM is detected, re-runs the modem init
    # sequence (QNWPREFCFG + COPS=0) identically to the startup path so the
    # modem is in the correct registered state before the next collection.
    # In Modes A and B (SIM present): this block is skipped entirely each
    # session — sim_present is True and the condition never fires.
    if not sim_present:
        print(f"{_ts()} [MAIN] Mode C — checking for SIM insertion before session {session_count}...")
        sim_present = detect_sim()

        if sim_present:
            print(f"{_ts()} [MAIN] SIM now detected — re-initializing modem...")
            send_runtime_alarm(
                "SIM card",
                "SIM card inserted. Re-initializing modem and switching "
                "to full KPI collection mode."
            )
            try:
                send_at_command_with_retry('AT+QNWPREFCFG="mode_pref",AUTO', 3)
            except Exception as e:
                print(f"{_ts()} [MAIN] QNWPREFCFG failed after SIM insertion: {e} — continuing.")
            send_cops_command_until_success(AT_CMD_COPS_AUTO, timeout=180)
            time.sleep(10)
            print(f"{_ts()} [MAIN] Modem re-initialized — session {session_count} will "
                  f"use {'Mode A' if nr5g_bands else 'Mode B'}.")

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
    monitored_bands = cfg["monitored_bands"]
    lte_bands  = [b for b in monitored_bands if b.startswith("b")]
    nr5g_bands = [b for b in monitored_bands if b.startswith("n")]
    total_bands             = len(nr5g_bands) + len(lte_bands)
    session_cmd_failures    = 0
    session_serial_failures = 0

    try:
        session, session_cmd_failures, session_serial_failures = instKPIcollection(nr5g_bands, lte_bands, sim_present=sim_present)
        sessions.append(session)
        print(f"{_ts()} [MAIN] Session {session_count} collected — "
              f"{len(session.readings)} bands read, "
              f"{session_cmd_failures} AT command failure(s), "
              f"{session_serial_failures} serial failure(s).")

    except Exception as e:
        # instKPIcollection raised before returning counts. Attributed entirely
        # to command failures — a complete crash before the band loops return
        # is a software or modem-state issue, not a hardware disconnect.
        # serial_failure_count stays 0 so the USB counter is not affected.
        session_cmd_failures    = total_bands
        session_serial_failures = 0

        print(f"{_ts()} [MAIN] Session {session_count} collection failed unexpectedly: {e} "
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

        sessions.append(SamplingSession(
            session_start = session_start,
            readings      = dummy_readings,
        ))
        print(f"{_ts()} [MAIN] Dummy session inserted for session {session_count} — "
              f"{len(dummy_readings)} bands set to sentinel 9999.")

    # ── Serial Failure Counter ────────────────────────────────────────────────
    # Increments when every band in the session raised SerialException —
    # the serial port was unreachable at the hardware level for the entire
    # session. Resets the moment any band completes without a serial error,
    # including bands that returned SEARCH (modem replied, just found no cell).
    # Alarm directs the operator to physical connection and USB enumeration.
    if total_bands > 0 and session_serial_failures == total_bands:
        _consecutive_serial_failure_sessions += 1
        print(f"{_ts()} [MAIN] All bands failed via serial port error — "
              f"consecutive serial failure sessions: "
              f"{_consecutive_serial_failure_sessions}/{_USB_SERIAL_FAILURE_THRESHOLD}")

        if _consecutive_serial_failure_sessions >= _USB_SERIAL_FAILURE_THRESHOLD:
            send_runtime_alarm(
                "serial port",
                f"All bands failed via SerialException for "
                f"{_consecutive_serial_failure_sessions} consecutive sessions — "
                f"USB serial port disconnected or driver failure. "
                f"Check physical modem connection and USB enumeration on the Pi."
            )
            _trigger_modem_restart(
                f"Serial port unreachable for {_consecutive_serial_failure_sessions} "
                f"consecutive sessions — USB hardware disconnected or driver failure."
            )
    else:
        if _consecutive_serial_failure_sessions > 0:
            print(f"{_ts()} [MAIN] Serial port recovered — resetting serial failure counter.")
        _consecutive_serial_failure_sessions = 0

    # ── AT Command Failure Counter ────────────────────────────────────────────
    # Increments when every band failed via AT command error (ERROR, TIMEOUT,
    # or unexpected exception) while the serial port was communicating.
    # Kept separate from the serial counter: command failures point to modem
    # firmware or state, not physical hardware — the restart message reflects
    # this so the operator knows where to look when investigating manually.
    if total_bands > 0 and session_cmd_failures == total_bands:
        _consecutive_command_failure_sessions += 1
        print(f"{_ts()} [MAIN] All bands failed via AT command error — "
              f"consecutive failure sessions: "
              f"{_consecutive_command_failure_sessions}/"
              f"{_CONSECUTIVE_FAILURE_RESTART_THRESHOLD}")

        if _consecutive_command_failure_sessions >= _CONSECUTIVE_FAILURE_RESTART_THRESHOLD:
            _trigger_modem_restart(
                f"All bands failed via AT command error for "
                f"{_consecutive_command_failure_sessions} consecutive sessions — "
                f"modem present on serial port but not processing commands. "
                f"Check modem firmware state and AT interface."
            )
    else:
        if _consecutive_command_failure_sessions > 0:
            print(f"{_ts()} [MAIN] AT comms recovered — resetting consecutive failure counter.")
        _consecutive_command_failure_sessions = 0

    # ── Window Processing ─────────────────────────────────────────────────────
    # Separated from the collection try/except above so a processing failure
    # does not interact with collection accounting. The finally block resets
    # the window unconditionally — whether processing succeeded or failed,
    # stale sessions must never carry over into the next window since they
    # would corrupt the averaging and alarm results for a fresh 5-session window.
    if session_count == SAMPLES_PER_SESSION:
        try:
            print(f"\n{_ts()} [MAIN] 5 sessions collected — running time averaging and alarms...")
            averaged_results = process_window(sessions, lte_thresholds, nr5g_thresholds)
            update_gui_json(averaged_results)
            append_to_daily_file(averaged_results)
            print(f"{_ts()} [MAIN] Window processed — resetting for next collection window.")

        except Exception as e:
            print(f"{_ts()} [MAIN] Window processing failed: {e} — resetting window.")
            send_runtime_alarm("process_window", f"Window processing failed: {e}")

        finally:
            sessions      = []
            session_count = 0

                # ── Reload config after every completed averaging window ──────────────
        # This makes GUI band changes take effect at the beginning of the next
        # big loop/window, after the current 5-session average has completed.
        try:
            old_monitored_bands = monitored_bands.copy()

            cfg = load_config()

            site_name = cfg["site_name"]
            device_id = cfg["device_id"]
            snmp_host = cfg["snmp_host"]

            monitored_bands = cfg["monitored_bands"]
            lte_bands  = [b for b in monitored_bands if b.startswith("b")]
            nr5g_bands = [b for b in monitored_bands if b.startswith("n")]

            lte_thresholds = {
                "rssi": cfg["rssi_threshold_min"],
                "rsrp": cfg["rsrp_threshold_min"],
                "rsrq": cfg["rsrq_threshold_min"],
                "sinr": cfg["sinr_threshold_min"],
            }

            nr5g_thresholds = {
                "ss_rsrp": cfg["rsrp_threshold_min"],
                "ss_rsrq": cfg["rsrq_threshold_min"],
                "ss_sinr": cfg["sinr_threshold_min"],
            }

            if monitored_bands != old_monitored_bands:
                print(f"{_ts()} [CONFIG] GUI band configuration changed.")
                print(f"{_ts()} [CONFIG] Previous bands: {old_monitored_bands}")
                print(f"{_ts()} [CONFIG] New bands for next window: {monitored_bands}")
            else:
                print(f"{_ts()} [CONFIG] Config reloaded — bands unchanged: {monitored_bands}")

        except Exception as e:
            print(f"{_ts()} [CONFIG] Config reload failed after averaging window: {e}")
            send_runtime_alarm(
                "config reload",
                f"Failed to reload config after averaging window: {e}. "
                f"Continuing with previous monitored bands: {monitored_bands}"
            )

    # ── Timing ────────────────────────────────────────────────────────────────
    # session_start was captured before instKPIcollection ran so collection
    # time, processing time, and dummy session build time are all accounted
    # for in the remaining sleep. This block always runs regardless of what
    # happened above so the sample schedule is never disrupted by a failure.
    next_session   = session_start + timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    sleep_duration = (next_session - datetime.now()).total_seconds()

    if sleep_duration > 0:
        print(f"{_ts()} [MAIN] Sleeping {sleep_duration:.1f}s until next session...")
        time.sleep(sleep_duration)
    else:
        # Collection or processing overran the window — continue immediately
        # without sleeping. The overrun is logged so the operator can identify
        # if collection is consistently taking longer than SAMPLE_INTERVAL_SECONDS.
        print(f"{_ts()} [MAIN] Warning: overran window by {abs(sleep_duration):.1f}s — "
              f"starting next session immediately.")

    # ── VPN Status Check ──────────────────────────────────────────────────────
    # Check VPN connection at the end of each collection cycle and update
    # device_data.json accordingly. If VPN is down, attempt automatic restart.
    # This ensures VPN status is always current and recovery is attempted.
    check_and_update_vpn_status("/home/das/DAS-Communication-System/device/GUI/vpn/client.ovpn")
