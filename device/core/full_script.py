"""
Module: full_script.py
Purpose: Main entry point for the DAS monitoring system.
         Orchestrates the full pipeline on every 5-session averaging window:
             1. Startup  — VPN, modem initialization, SIM detection, config load
             2. Collect  — calls instKPIcollection() once per session (5 sessions per window)
             3. Average  — calls process_window() after 5 sessions to evaluate KPIs
             4. Alarm    — threshold and invalid alarms fired inside process_window()
             5. Store    — updates device_data.json (GUI) and daily KPI log file
             6. Reload   — reloads config after each window so GUI changes take effect

Pipeline data flow:
    load_config()
        → instKPIcollection()   [standin_kpi_collection.py]  → SamplingSession
        → process_window()      [alarms.py]                  → list[AveragedKPI]
        → update_gui_json()     [file_manager.py]
        → append_to_daily_file()[file_manager.py]

Dependencies:
    standin_kpi_collection.py — KPI collection, AT command retry wrappers, SIM detection
    alarms.py                 — window averaging, invalid and threshold alarm logic
    file_manager.py           — GUI JSON and daily KPI file writes
    models.py                 — LTEKPI, NR5GKPI, SamplingSession, AveragedKPI types
    constants.py              — AT command strings, timing constants, CONFIG_PATH
    modem.py                  — serial port reference (PORT)
    snmpSend.py               — runtime alarm sender

NOTE: Only the no-SIM LTE-only path (Mode C) has been tested end-to-end.
      The SIM-present paths (Mode A: NR5G + LTE, Mode B: LTE only with SIM)
      are implemented but untested — validate before deploying with a SIM inserted.
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

# ══════════════════════════════════════════════════════════════════════════════
# Section A: Timestamp Helper
# ══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ══════════════════════════════════════════════════════════════════════════════
# Section B: Config Loading
# ══════════════════════════════════════════════════════════════════════════════

# Fields that must be present and non-null in config.json before the script
# proceeds. The script blocks in load_config() until all are satisfied,
# so the operator must complete GUI setup before collection can begin.
REQUIRED_FIELDS = [
    "site_name", "device_id",
    "snmp_host", "monitored_bands",
    "rssi_threshold_min",
    "rsrp_threshold_min",
    "rsrq_threshold_min",
    "sinr_threshold_min",
]

def load_config():
    """
    Load config.json and block until all required fields are present and non-null.

    Retries every 5 seconds on any of the following conditions:
        - File not found (GUI hasn't written the config yet)
        - Invalid JSON (file is being written and not yet complete)
        - One or more required fields are missing or null
        - monitored_bands is present but empty (no bands selected in GUI)

    This blocking behavior is intentional — the system cannot collect KPIs
    without knowing which bands to monitor and where to send SNMP traps.
    The operator must complete the GUI configuration form before collection
    can begin.

    Returns:
        dict: The fully populated config dictionary once all required fields
              are present and non-null.
    """
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
            # Config file hasn't been written yet — GUI may not have been set up.
            print(f"{_ts()} [CONFIG] config.json not found — retrying in 5s...")
            time.sleep(5)
        except json.JSONDecodeError:
            # File exists but contains invalid JSON — may be mid-write by the GUI.
            print(f"{_ts()} [CONFIG] config.json is invalid JSON — retrying in 5s...")
            time.sleep(5)

# ══════════════════════════════════════════════════════════════════════════════
# Section C: VPN Management
# All VPN functions use the system 'ip' command to check tun0 interface state
# and subprocess to manage the openvpn process directly.
# ══════════════════════════════════════════════════════════════════════════════
def start_vpn(ovpn_path):
    """
    Launch OpenVPN as a background subprocess using the provided .ovpn config.
    Called once at startup before anything else — SNMP traps require VPN
    connectivity to reach the NMS, so VPN must be started first.

    The 10-second sleep gives OpenVPN time to complete the TLS handshake
    and establish the tun0 interface before other startup steps run.

    Args:
        ovpn_path: Absolute path to the .ovpn client config file.

    Returns:
        subprocess.Popen: The running OpenVPN process handle, retained by the
                          caller so it can be killed during a restart.
    """
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
    Check whether the OpenVPN tunnel interface (tun0) is currently active.

    Uses 'ip addr show tun0' — returncode 0 means the interface exists,
    non-zero means it is absent. This is the same check used by init_system.py
    and check_and_update_vpn_status() for consistency.

    Returns:
        bool: True if tun0 is present and VPN is active, False otherwise.
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
    Kill any existing OpenVPN process and start a fresh connection.

    Called automatically by check_and_update_vpn_status() when tun0 is absent.
    The 2-second pause between kill and restart gives the OS time to release
    the TUN device and network resources before the new process claims them.
    The 10-second sleep after Popen mirrors start_vpn() for the same reason.

    A runtime alarm is sent if the restart itself fails (e.g. openvpn binary
    missing, permission error) since that requires manual operator intervention.

    Args:
        ovpn_path: Absolute path to the .ovpn client config file.

    Returns:
        subprocess.Popen: The new OpenVPN process handle, or None if restart failed.
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
    Check VPN state, update device_data.json, and attempt restart if down.

    Called at the end of every collection session in the main loop.
    Checking per-session (rather than per-window) means the GUI vpn_status
    field stays current and auto-recovery is attempted promptly if the tunnel drops.

    Recovery flow:
        1. Check tun0 via check_vpn_connected()
        2. If down: update GUI status, send alarm, call restart_vpn()
        3. Wait 5 seconds then recheck — update GUI with final state
        4. If still down after restart: send a second alarm so the operator
           knows automatic recovery failed and manual intervention is needed

    Args:
        ovpn_path: Absolute path to the .ovpn client config file.
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

# ══════════════════════════════════════════════════════════════════════════════
# Section D: Startup Sequence
# Runs once when the script launches. Order matters — VPN must come first
# so SNMP traps can reach the NMS during modem initialization.
# ══════════════════════════════════════════════════════════════════════════════

# ── Step 1: VPN ───────────────────────────────────────────────────────────────
# Started before everything else — SNMP traps sent during modem init require
# the VPN tunnel to be up to reach the NMS. If VPN is down, traps are lost.
vpn_process = start_vpn("/home/das/DAS-Communication-System/device/GUI/vpn/client.ovpn")

# ── Step 2: Config Load ───────────────────────────────────────────────────────
# Blocks until all required fields are present — see load_config() above.
print(f"{_ts()} [STARTUP] Loading config...")
cfg = load_config()

# Apply config values
site_name      = cfg["site_name"]
device_id      = cfg["device_id"]
snmp_host      = cfg["snmp_host"]
#snmp_community = cfg["snmp_community"]

# ── Step 3: Band and Threshold Parsing ────────────────────────────────────────
# Split monitored_bands from config into separate LTE and NR5G lists.
# Band label convention: 'b' prefix = LTE (e.g. 'b4', 'b12').
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

# ── Step 4: Modem Initialization ──────────────────────────────────────────────
# AT+CFUN=1 is a hardware-level reset that works regardless of SIM state.
# It must complete before SIM detection — the modem may not respond to
# AT+QSIMSTAT? reliably until it has been placed in full functionality mode.
print(f"{_ts()} [STARTUP] Sending AT+CFUN=1 — full modem reset...")
send_cfun_until_success("AT+CFUN=1", timeout=15)
print(f"{_ts()} [STARTUP] AT+CFUN=1 accepted — waiting 15 seconds for modem to fully boot...")
time.sleep(15) # Allow modem firmware to fully initialize before issuing further commands

# ── Step 5: SIM Detection and Network Registration ────────────────────────────
# SIM state determines which startup commands are safe to send.
# AT+COPS=0 and AT+COPS=2 both return ERROR without a SIM, which would cause
# send_cops_command_until_success() to retry indefinitely and hang the script.
# sim_present is set here once and re-evaluated at the top of every collection
# session so the device recovers automatically when a SIM is inserted later.
print(f"{_ts()} [STARTUP] Checking for SIM card...")
sim_present = detect_sim()

if sim_present:
    # SIM present — run full network initialization sequence.
    print(f"{_ts()} [STARTUP] SIM detected — running full modem initialization.")

    # Set mode preference to AUTO so the modem can use both LTE and NR5G.
    # Failure here is non-fatal — the modem may already be in AUTO mode
    # from a previous session. A runtime alarm is sent but execution continues.
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
# Section E: Main Collection Loop
# Runs indefinitely. Each iteration is one collection session.
# Every SAMPLES_PER_SESSION (5) sessions, the window is averaged and results
# are stored and reported. Config is reloaded after each completed window.
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

_USB_SERIAL_FAILURE_THRESHOLD        = 2   # Sessions before Pi restart on serial failure
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
                 
            # QNWPREFCFG failure is non-fatal here — same reasoning as startup.
            try:
                send_at_command_with_retry('AT+QNWPREFCFG="mode_pref",AUTO', 3)
            except Exception as e:
                print(f"{_ts()} [MAIN] QNWPREFCFG failed after SIM insertion: {e} — continuing.")

            # COPS=0 uses indefinite retry — must succeed before collection continues.
            send_cops_command_until_success(AT_CMD_COPS_AUTO, timeout=180)
            time.sleep(10)
            print(f"{_ts()} [MAIN] Modem re-initialized — session {session_count} will "
                  f"use {'Mode A' if nr5g_bands else 'Mode B'}.")

    # ── Band List Refresh ──────────────────────────────────────────────────────
    # Re-derive lte_bands and nr5g_bands from the current cfg each session.
    # cfg is reloaded after each completed 5-session window (see Section F),
    # so this refresh picks up any band changes the operator made in the GUI
    # since the last window completed. Within a window, cfg doesn't change,
    # so this is effectively a no-op between reloads.
    monitored_bands = cfg["monitored_bands"]
    lte_bands  = [b for b in monitored_bands if b.startswith("b")]
    nr5g_bands = [b for b in monitored_bands if b.startswith("n")]
    total_bands             = len(nr5g_bands) + len(lte_bands)
    session_cmd_failures    = 0
    session_serial_failures = 0



    # ── KPI Collection ────────────────────────────────────────────────────────
    # instKPIcollection() is wrapped in its own try/except separate from the
    # window processing block below. Separating them ensures a collection
    # failure does not reset sessions[] or session_count — sessions already
    # accumulated in this window are preserved and the window completes normally
    # using the dummy session inserted here in place of the failed one.
    #
    # Dummy session design:
    #   Structure integrity: process_window() and check_kpi() expect exactly
    #   5 SamplingSession objects, each containing one typed KPI object per
    #   configured band in NR5G-first, LTE-second order. The dummy mirrors
    #   this structure exactly so no special-case handling is needed downstream.
    #
    #   Alarm propagation: all dummy numeric fields are set to 9999, which
    #   exceeds INVALID_SENTINEL (500) in alarms.py. This means check_kpi()
    #   fires the invalid alarm path naturally for every KPI on every band
    #   in this session — the operator is alerted through the normal mechanism.
    #
    #   Band ordering: NR5G bands are appended first, LTE bands second —
    #   matching instKPIcollection() exactly. process_window() uses positional
    #   index (band_index) to match readings across sessions, so a mismatch
    #   here would silently corrupt band averaging.
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
            # USB serial port is unreachable — a Pi reboot resets the USB bus
            # and modem power, which is the only automated recovery for a
            # hardware-level disconnect or driver failure.
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
            # Modem is on the serial port but not responding to any AT command —
            # Pi restart resets the USB bus and modem power as the only recovery.
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
            # process_window() → check_kpi() per band → fires SNMP alarms internally
            # → returns list[AveragedLTEKPI | AveragedNR5GKPI] for storage
                 
            averaged_results = process_window(sessions, lte_thresholds, nr5g_thresholds)
            update_gui_json(averaged_results)       # Overwrites device_data.json for GUI
            append_to_daily_file(averaged_results)  # Appends entry to YYYYMMDD_kpi.json
            print(f"{_ts()} [MAIN] Window processed — resetting for next collection window.")

        except Exception as e:
            print(f"{_ts()} [MAIN] Window processing failed: {e} — resetting window.")
            send_runtime_alarm("process_window", f"Window processing failed: {e}")

        finally:
            # Always reset — stale sessions from a failed window must not carry
            # into the next window regardless of whether processing succeeded.
            sessions      = []
            session_count = 0

        # ── Section F: Config Reload After Each Window ─────────────────────────
        # Placed inside the session_count == SAMPLES_PER_SESSION block but
        # outside the try/except/finally above so it runs after the window
        # resets regardless of processing outcome. Reloading here (once per
        # window rather than once per session) means band and threshold changes
        # the operator makes in the GUI take effect at the next window boundary
        # without interrupting an in-progress 5-session collection.
        try:
            old_monitored_bands = monitored_bands.copy()

            cfg = load_config()

            site_name = cfg["site_name"]
            device_id = cfg["device_id"]
            snmp_host = cfg["snmp_host"]

            # Rebuild band lists and thresholds from the freshly loaded config.
            # If the operator changed bands in the GUI, the new lists take effect
            # starting with the very next session of the new window.
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
            # Config reload failure is non-fatal — collection continues with
            # the previous band list and thresholds. The operator is alerted
            # so they know their GUI change may not have taken effect yet.
            print(f"{_ts()} [CONFIG] Config reload failed after averaging window: {e}")
            send_runtime_alarm(
                "config reload",
                f"Failed to reload config after averaging window: {e}. "
                f"Continuing with previous monitored bands: {monitored_bands}"
            )

    # ── Session Timing ────────────────────────────────────────────────────────
    # Compute remaining sleep time based on session_start, which was captured
    # before instKPIcollection() ran. This means collection time, dummy session
    # build time, processing time, and file write time are all deducted from
    # the sleep, keeping the session schedule as close to SAMPLE_INTERVAL_SECONDS
    # as possible regardless of how long each step took.
    # This block always executes — timing is never skipped due to a failure above.
    next_session   = session_start + timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    sleep_duration = (next_session - datetime.now()).total_seconds()

    if sleep_duration > 0:
        print(f"{_ts()} [MAIN] Sleeping {sleep_duration:.1f}s until next session...")
        time.sleep(sleep_duration)
    else:
        # Collection or processing overran SAMPLE_INTERVAL_SECONDS — start the
        # next session immediately without sleeping. The overrun is logged so
        # the operator can identify if the pipeline is consistently too slow
        # for the configured interval and SAMPLE_INTERVAL_SECONDS needs tuning.
        print(f"{_ts()} [MAIN] Warning: overran window by {abs(sleep_duration):.1f}s — "
              f"starting next session immediately.")

    # ── VPN Status Check ──────────────────────────────────────────────────────
    # Check VPN connection at the end of each collection cycle and update
    # device_data.json accordingly. If VPN is down, attempt automatic restart.
    # This ensures VPN status is always current and recovery is attempted.
    check_and_update_vpn_status("/home/das/DAS-Communication-System/device/GUI/vpn/client.ovpn")
