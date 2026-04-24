"""
Module: kpi_collection.py
Purpose: Instantaneous KPI collection — queries the modem band by band and
         builds a SamplingSession containing one KPI reading per band.
Dependencies: models/core.py, models/constants.py, models/rules.py, atCommandExample.py
Author:
"""

from datetime import datetime
from models import LTEKPI, NR5GKPI, SamplingSession
from constants import AT_CMD_5G_BAND_CONFIG, AT_CMD_LTE_BAND_CONFIG, AT_CMD_SERVING_CELL
from modem import at_command_comms, PORT
from snmpSend import send_runtime_alarm
import time
import serial
import os
import subprocess


# ── LTE SINR Conversion ───────────────────────────────────────────────────────
# The raw SINR integer from AT+QENG for LTE is NOT in dB.
# Formula confirmed by Quectel support: Y = (1/5) × X × 10 − 20
# Example: raw value 16 → (0.2 × 16 × 10) − 20 = 12 dB
# NR5G SINR is already returned in dB — no conversion needed.


def parse_serving_cell(raw_response: str, band: str) -> LTEKPI | NR5GKPI | None:
    """
    Parses the raw string returned by AT+QENG="servingcell".
    Returns an LTEKPI or NR5GKPI object, or None if the modem
    is in SEARCH state (no cell found) or the response is unreadable.

    Args:
        raw_response: Cleaned string from at_command_comms().
        band:         Band string from BANDS list e.g. 'b2', 'n2'.
    """

    # SEARCH means the modem found no cell on this band — caller will store sentinel values
    if "SEARCH" in raw_response:
        print(f"[PARSER] Band {band}: modem in SEARCH state — no cell found.")
        return None

    try:
        # Step 1: Find the line that actually contains the QENG data.
        # raw_response may have the echo of the command on the first line,
        # so we look for the line starting with +QENG.
        qeng_line = ""
        for line in raw_response.strip().split('\n'):
            if line.strip().startswith('+QENG'):
                qeng_line = line.strip()
                break

        if not qeng_line:
            print(f"[PARSER] Band {band}: no +QENG line found in response.")
            return None

        # Step 2: Strip the "+QENG: " prefix and all quotes, then split on commas.
        clean    = qeng_line.replace('+QENG:', '').replace('"', '').strip()
        raw_list = clean.split(',')

        # Step 3: Convert each field to int if possible, keep as string otherwise.
        # "-" values (invalid/unavailable fields) will stay as strings.
        parts = []
        for item in raw_list:
            item = item.strip()
            if item == '-':
                print(f"[PARSER] Band {band}: '-' value encountered — substituting sentinel 9999.")
                parts.append(9999)
                continue
            try:
                parts.append(int(item))
            except ValueError:
                try:
                    parts.append(float(item))
                except ValueError:
                    parts.append(item)  # keep as string e.g. "LTE", "FDD"

        # After stripping +QENG: and quotes, the fields line up as:
        # [0]=servingcell, [1]=state, [2]=RAT, [3]=duplex,
        # [4]=MCC, [5]=MNC, [6]=cellID, [7]=PCI, [8]=EARFCN or ARFCN,
        # [9]=freq_band(LTE) or TAC(NR5G), ...
        #
        # LTE full map:
        # [7]=PCI, [8]=EARFCN, [9]=freq_band, [10]=UL_bw, [11]=DL_bw,
        # [12]=TAC, [13]=RSRP, [14]=RSRQ, [15]=RSSI, [16]=SINR_raw
        #
        # NR5G-SA full map:
        # [7]=PCI, [8]=TAC, [9]=ARFCN, [10]=band, [11]=DL_bw,
        # [12]=RSRP, [13]=RSRQ, [14]=SINR

        rat = parts[2]  # "LTE" or "NR5G-SA"

        # ── LTE ──────────────────────────────────────────────────────────────
        if rat == "LTE":
            # Raw SINR from modem is NOT in dB — must convert.
            # Formula: Y = (1/5) × X × 10 − 20  (confirmed by Quectel support)
            raw_sinr       = parts[16]
            converted_sinr = (0.2 * raw_sinr * 10) - 20 if isinstance(raw_sinr, (int, float)) else 0.0

            return LTEKPI(
                timestamp = datetime.now(),
                rat       = "LTE",
                band      = int(band[1:]),   # strip 'b' → integer e.g. 'b2' → 2
                pci       = parts[7],
                earfcn    = parts[8],
                rsrp      = parts[13],
                rsrq      = parts[14],
                rssi      = parts[15],
                sinr      = converted_sinr,
            )

        # ── NR5G-SA ──────────────────────────────────────────────────────────
        elif rat == "NR5G-SA":
            # NR5G SINR is already in dB — no conversion needed
            return NR5GKPI(
                timestamp = datetime.now(),
                rat       = "NR5G",
                band      = int(band[1:]),   # strip 'n' → integer e.g. 'n2' → 2
                pci       = parts[7],
                arfcn     = parts[9],
                ss_rsrp   = parts[12],
                ss_rsrq   = parts[13],
                ss_sinr   = parts[14],
            )

        else:
            print(f"[PARSER] Band {band}: unrecognized RAT '{rat}' in response.")
            return None

    except (IndexError, ValueError, TypeError) as e:
        print(f"[PARSER] Band {band}: failed to parse. Error: {e}")
        print(f"         Raw was: {raw_response[:120]}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Segment 3A — AT Command Retry Wrapper
# ══════════════════════════════════════════════════════════════════════════════

def send_at_command_with_retry(command, timeout, max_retries=3):
    """
    Sends an AT command and retries up to max_retries times if the
    modem responds with ERROR.

    Rather than accepting a single ERROR and moving on, this gives
    the modem up to 3 chances before raising an Exception.
    The Exception is caught by the band loop so the session
    continues even if one band fails.

    Args:
        command:     The AT command string to send.
        timeout:     Timeout in seconds passed to at_command_comms.
        max_retries: Number of attempts before raising. Default is 3.

    Returns:
        The modem's response string if successful.

    Raises:
        Exception: If all retry attempts return ERROR.
    """
    for attempt in range(max_retries):
        try:
            response = at_command_comms(command, timeout)
        except serial.SerialException as e:
            # SerialException means the serial layer itself failed — not just a
            # bad modem response. Check whether the USB device is still present.
            # If the port has disappeared, the modem has physically disconnected
            # or the USB driver has dropped it — a Pi restart re-enumerates the
            # USB bus and reloads the driver, which is the only viable recovery.
            if not os.path.exists(PORT):
                _trigger_modem_restart(
                    f"Modem not detected on USB port {PORT} — device disconnected "
                    f"or USB driver failure. Restarting Pi to recover."
                )
            # Port exists but SerialException still raised — re-raise so the
            # calling retry loop (COPS or CFUN) handles it as a normal failure.
            raise

        if response not in ("ERROR", "TIMEOUT"):
            return response

        reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
        print(f"[RETRY] Attempt {attempt + 1}/{max_retries} — {command} {reason}.")
        time.sleep(0.3)

    raise Exception(f"[MODEM ALERT] Command failed after {max_retries} attempts: {command}")


# ══════════════════════════════════════════════════════════════════════════════
# Segment 3B — Infinite Retry for Critical AT Commands
# ══════════════════════════════════════════════════════════════════════════════

# Retry timing constants — adjust here only, referenced throughout both functions.
_CRITICAL_RETRY_INITIAL_SLEEP   = 10   # seconds between retries before alert fires
_CRITICAL_RETRY_ESCALATED_SLEEP = 30   # seconds between retries after alert fires
_CRITICAL_ALERT_TIMEOUT         = 120  # seconds elapsed before sending runtime alarm
_CRITICAL_RESTART_TIMEOUT       = 300  # seconds elapsed before triggering Pi restart (5 minutes)

def _trigger_modem_restart(reason: str) -> None:
    """
    Sends an SNMP runtime trap describing the restart reason, waits briefly
    to allow the trap to transmit, then issues a system reboot.

    Called when the modem has been unresponsive long enough that a Pi restart
    is the only viable recovery path. Never returns — the reboot terminates
    the process.

    Args:
        reason: Human-readable description of why the restart was triggered.
                Included verbatim in the SNMP trap detail field.
    """
    print(f"[RESTART] {reason}")
    print(f"[RESTART] Sending trap and rebooting Pi...")
    try:
        send_runtime_alarm(
            component = "Pi restart",
            detail    = reason
        )
    except Exception as e:
        # Trap failure must never block the restart — log and proceed.
        print(f"[RESTART] Trap send failed: {e} — proceeding with reboot anyway.")
    time.sleep(2)  # Allow trap UDP packet time to transmit before process dies
    subprocess.run(['sudo', 'reboot'])

def send_cops_command_until_success(command: str, timeout: int = 180) -> str:
    """
    Sends a COPS AT command (AT+COPS=0 or AT+COPS=2) and retries indefinitely
    until the modem accepts it. This function NEVER raises — the caller is
    always guaranteed a successful response before execution continues.

    Retry behavior:
        Phase 1 — First 120 seconds:
            Retries every 10 seconds, logging each failed attempt.

        Alert threshold — at 120 seconds elapsed:
            Sends one SNMP runtime alarm to Infolink via send_runtime_alarm()
            identifying the command and how long it has been failing.
            The alarm is sent exactly once per call — not repeated.

        Phase 2 — After alert fires:
            Continues retrying every 30 seconds indefinitely until the modem
            responds. Sleep interval stays at 30 seconds for all remaining
            attempts.

    Both modem ERROR responses and exceptions from at_command_comms (e.g. a
    serial port issue) are handled identically — logged and retried.

    Args:
        command: The COPS command string e.g. 'AT+COPS=0' or 'AT+COPS=2'.
        timeout: Timeout in seconds passed to at_command_comms. Default 180s.

    Returns:
        The modem's response string once the command succeeds.
    """
    attempt    = 0
    alert_sent = False
    start_time = time.time()

    while True:
        attempt += 1

        try:
            response = at_command_comms(command, timeout)

            if response not in ("ERROR", "TIMEOUT"):
                # Success — log how many attempts it took if more than one
                if attempt > 1:
                    elapsed = time.time() - start_time
                    print(f"[COPS] {command} succeeded on attempt {attempt} "
                          f"({elapsed:.0f}s elapsed).")
                return response

            # Modem returned ERROR or timed out — fall through to retry logic below
            reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
            print(f"[COPS] Attempt {attempt} — {command} {reason}.")

        except Exception as e:
            # at_command_comms itself raised (e.g. serial port dropped) —
            # treat identically to an ERROR response and keep retrying.
            print(f"[COPS] Attempt {attempt} — {command} raised exception: {e}.")

        # ── Alert and restart checks (time-based) ────────────────────────────
        # Alert fires once at 120s — notifies operator the command is failing.
        # Restart fires at 240s (4 minutes) — if the modem hasn't responded in
        # 4 minutes it is not recoverable without a power cycle. The Pi restart
        # resets the USB bus and modem power, which is the only viable recovery.
        elapsed = time.time() - start_time

        if not alert_sent and elapsed >= _CRITICAL_ALERT_TIMEOUT:
            alert_detail = (
                f"no modem response after {elapsed:.0f}s — "
                f"script is retrying every {_CRITICAL_RETRY_ESCALATED_SLEEP}s"
            )
            print(f"[COPS] ALERT — {command} has been failing for {elapsed:.0f}s. "
                  f"Sending runtime alarm to Infolink...")
            try:
                send_runtime_alarm(component=command, detail=alert_detail)
            except Exception as snmp_e:
                print(f"[COPS] Runtime alarm send failed: {snmp_e} — continuing retries.")
            alert_sent = True

        if elapsed >= _CRITICAL_RESTART_TIMEOUT:
            _trigger_modem_restart(
                f"{command} has not responded after {elapsed:.0f}s — "
                f"modem is unresponsive. Restarting Pi to recover."
            )

        # ── Sleep before next attempt ─────────────────────────────────────────
        # Use escalated interval once the alert has fired, initial interval before
        sleep_time = _CRITICAL_RETRY_ESCALATED_SLEEP if alert_sent else _CRITICAL_RETRY_INITIAL_SLEEP
        print(f"[COPS] Retrying {command} in {sleep_time}s...")
        time.sleep(sleep_time)


def send_cfun_until_success(command: str = "AT+CFUN=1", timeout: int = 15) -> str:
    """
    Sends AT+CFUN=1 (or a specified CFUN command) and retries indefinitely
    until the modem accepts it. This function NEVER raises — the caller is
    always guaranteed a successful response before execution continues.

    Modeled identically after send_cops_command_until_success. Used for
    critical modem functionality commands during startup where the script
    cannot safely proceed until the modem is in the correct state.

    Retry behavior:
        Phase 1 — First 120 seconds:
            Retries every 10 seconds, logging each failed attempt.

        Alert threshold — at 120 seconds elapsed:
            Sends one SNMP runtime alarm identifying the command and elapsed
            time. The alarm is sent exactly once per call — not repeated.

        Phase 2 — After alert fires:
            Continues retrying every 30 seconds indefinitely until the modem
            responds.

    Both modem ERROR responses and exceptions from at_command_comms are
    handled identically — logged and retried.

    Args:
        command: The CFUN command string. Default is 'AT+CFUN=1'.
        timeout: Timeout in seconds passed to at_command_comms. Default 15s.

    Returns:
        The modem's response string once the command succeeds.
    """
    attempt    = 0
    alert_sent = False
    start_time = time.time()

    while True:
        attempt += 1

        try:
            response = at_command_comms(command, timeout)

            if response not in ("ERROR", "TIMEOUT"):
                # Success — log how many attempts it took if more than one
                if attempt > 1:
                    elapsed = time.time() - start_time
                    print(f"[CFUN] {command} succeeded on attempt {attempt} "
                          f"({elapsed:.0f}s elapsed).")
                return response

            # Modem returned ERROR or timed out — fall through to retry logic below
            reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
            print(f"[CFUN] Attempt {attempt} — {command} {reason}.")

        except Exception as e:
            # at_command_comms itself raised (e.g. serial port dropped) —
            # treat identically to an ERROR response and keep retrying.
            print(f"[CFUN] Attempt {attempt} — {command} raised exception: {e}.")

        # ── Alert and restart checks (time-based) ────────────────────────────
        # Alert fires once at 120s — notifies operator the command is failing.
        # Restart fires at 240s (4 minutes) — same reasoning as COPS restart.
        elapsed = time.time() - start_time

        if not alert_sent and elapsed >= _CRITICAL_ALERT_TIMEOUT:
            alert_detail = (
                f"no modem response after {elapsed:.0f}s — "
                f"script is retrying every {_CRITICAL_RETRY_ESCALATED_SLEEP}s"
            )
            print(f"[CFUN] ALERT — {command} has been failing for {elapsed:.0f}s. "
                  f"Sending runtime alarm to Infolink...")
            try:
                send_runtime_alarm(component=command, detail=alert_detail)
            except Exception as snmp_e:
                print(f"[CFUN] Runtime alarm send failed: {snmp_e} — continuing retries.")
            alert_sent = True

        if elapsed >= _CRITICAL_RESTART_TIMEOUT:
            _trigger_modem_restart(
                f"{command} has not responded after {elapsed:.0f}s — "
                f"modem is unresponsive. Restarting Pi to recover."
            )

        # ── Sleep before next attempt ─────────────────────────────────────────
        sleep_time = _CRITICAL_RETRY_ESCALATED_SLEEP if alert_sent else _CRITICAL_RETRY_INITIAL_SLEEP
        print(f"[CFUN] Retrying {command} in {sleep_time}s...")
        time.sleep(sleep_time)


# ══════════════════════════════════════════════════════════════════════════════
# Segment 3C — Instantaneous KPI Collection
# ══════════════════════════════════════════════════════════════════════════════

def instKPIcollection(nr5g_bands, lte_bands):
    """
    Performs one full KPI collection pass across all configured bands
    and returns the results packaged as a SamplingSession.

    This function is called 5 times by the outer loop to build the
    list of 5 SamplingSession objects the averaging script expects.

    Collection mode is determined automatically by whether nr5g_bands
    is populated:

        Mode A — NR5G + LTE (nr5g_bands is not empty):
            AT+COPS=0  →  NR5G band loop  →  AT+COPS=2  →  LTE band loop
            →  AT+COPS=0 reset for next session

        Mode B — LTE Only (nr5g_bands is empty):
            AT+COPS=2  →  LTE band loop  →  done (no reset needed)

    AT+COPS commands always use send_cops_command_until_success so they
    retry indefinitely and never crash the script.
    AT+COPS=2 is sent exactly once per session in both modes — never
    inside any band loop.

    Args:
        nr5g_bands: List of NR5G band strings e.g. ['n2', 'n66'].
                    Pass an empty list [] to run in LTE-only mode.
        lte_bands:  List of LTE band strings  e.g. ['b2', 'b5', 'b12'].

    Returns:
        A SamplingSession containing one reading per band,
        NR5G readings first (if any), LTE readings second.
    """

    session_start = datetime.now()
    readings      = []
    command_failure_count = 0  # Tracks bands that failed via AT command exception
                               # (not bands that found no cell — those are normal).
                               # Returned to the main loop to detect modem-level
                               # failure patterns across consecutive sessions.

    # ── Mode Detection ────────────────────────────────────────────────────────
    # Determined once here so every subsequent branch is a simple bool check.
    # mode_a = True  → NR5G + LTE collection
    # mode_a = False → LTE only collection
    mode_a = bool(nr5g_bands)

    if mode_a:
        print(f"\n[SESSION] Mode A (NR5G + LTE) — starting collection at {session_start}")
    else:
        print(f"\n[SESSION] Mode B (LTE Only)   — starting collection at {session_start}")

    # ══════════════════════════════════════════════════════════════════════════
    # Mode A — NR5G + LTE
    # ══════════════════════════════════════════════════════════════════════════
    if mode_a:

        # ── Step 1: Enable auto-registration for NR5G ─────────────────────────
        # AT+COPS=0 tells the modem to register on any available network
        # including NR5G cells. Retried indefinitely — script cannot proceed
        # with NR5G collection until the modem accepts this.
        print("[SESSION][A] Sending AT+COPS=0 — enabling auto-registration for NR5G...")
        send_cops_command_until_success('AT+COPS=0', timeout=180)
        print("[SESSION][A] AT+COPS=0 accepted — proceeding with NR5G band loop.")

        # ── Step 2: NR5G Band Loop ────────────────────────────────────────────
        for band in nr5g_bands:

            band_num  = band[1:]
            dummy_kpi = NR5GKPI(
                timestamp = datetime.now(),
                rat       = "NR5G",
                band      = int(band_num),
                pci       = 9999,
                arfcn     = 9999,
                ss_rsrp   = 9999,
                ss_rsrq   = 9999,
                ss_sinr   = 9999,
            )

            # ── Pre-flight CFUN state check ───────────────────────────────────
            # Queries the modem's current functionality mode before each band
            # attempt. AT+CFUN=1 and its sleep only run if the modem is NOT
            # already in full functionality — typically caused by a previous
            # band's CFUN cycle failing mid-sequence and leaving the modem
            # stuck in CFUN=0, which would cause all subsequent band commands
            # to return ERROR and cascade dummy values across remaining bands.
            # On the normal path (modem already in CFUN=1) this costs one AT
            # query with no sleep, avoiding unnecessary delay per band.
            # If the query itself fails, state is unknown — attempt CFUN=1
            # as a precaution since it's safer than proceeding blind.
            try:
                cfun_response = send_at_command_with_retry("AT+CFUN?", 5)
                cfun_value    = None

                for line in cfun_response.strip().split('\n'):
                    line = line.strip()
                    if line.startswith('+CFUN:'):
                        try:
                            cfun_value = int(line.replace('+CFUN:', '').strip())
                        except ValueError:
                            pass
                        break

                if cfun_value != 1:
                    # Modem is not in full functionality — restore before proceeding.
                    # Covers both CFUN=0 (minimum) and None (value unreadable).
                    print(f"[NR5G] Band {band}: modem in CFUN={cfun_value} — "
                          f"restoring full functionality...")
                    send_at_command_with_retry("AT+CFUN=1", 15)
                    time.sleep(3)
                else:
                    print(f"[NR5G] Band {band}: modem already in full functionality — "
                          f"pre-flight skipped.")

            except Exception as preflight_e:
                # Query or restore failed — state is unknown.
                # Attempt CFUN=1 as a precaution before the band try block.
                print(f"[NR5G] Band {band}: pre-flight CFUN check failed: {preflight_e} "
                      f"— attempting AT+CFUN=1 as precaution.")
                try:
                    send_at_command_with_retry("AT+CFUN=1", 15)
                    time.sleep(3)
                except Exception as restore_e:
                    # Both the check and the restore failed — log and proceed.
                    # The main try block below will fail naturally if the modem
                    # is unresponsive and the dummy will be stored as normal.
                    print(f"[NR5G] Band {band}: pre-flight CFUN=1 also failed: {restore_e} "
                          f"— proceeding to band attempt.")

            try:
                print(f"[NR5G] Configuring band {band}...")
                send_at_command_with_retry(AT_CMD_5G_BAND_CONFIG + band_num, 0.3)
                time.sleep(2)
                print(send_at_command_with_retry("AT+CFUN=0", 15))
                time.sleep(3)
                print(send_at_command_with_retry("AT+CFUN=1", 15))
                time.sleep(3)

                # ── Serving Cell Query with Retry ─────────────────────────────
                # SEARCH is a transient state — the modem may still be scanning.
                # We try up to 3 times with a 1 second wait between each attempt.
                kpi = None
                for attempt in range(3):
                    raw_response = send_at_command_with_retry(AT_CMD_SERVING_CELL, 0.3)
                    kpi          = parse_serving_cell(raw_response, band)
                    if kpi is not None:
                        break
                    print(f"[NR5G] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                    time.sleep(1)

                # RAT and band verification — confirms the returned KPI actually
                # belongs to the band we configured. If the modem fell back to a
                # different RAT or band, we treat it as no valid reading.
                if kpi is not None and (not isinstance(kpi, NR5GKPI) or kpi.band != int(band_num)):
                    print(f"[NR5G] Band {band}: returned wrong RAT or band "
                          f"(got RAT={kpi.rat}, band={kpi.band}) — storing dummy.")
                    kpi = None

                if kpi is None:
                    print(f"[NR5G] Band {band}: no cell found after 3 attempts — storing dummy KPI.")
                    readings.append(dummy_kpi)
                else:
                    print(f"[NR5G] Band {band}: collected — "
                          f"SS-RSRP={kpi.ss_rsrp}, SS-RSRQ={kpi.ss_rsrq}, SS-SINR={kpi.ss_sinr}")
                    readings.append(kpi)

            except Exception as e:
                # Band configuration failed after all retries — log, trap, store
                # dummy to preserve index alignment, and continue to next band.
                print(f"[NR5G] Band {band}: failed — {e} — storing dummy KPI, continuing.")
                send_runtime_alarm(
                    f"NR5G band {band}",
                    f"Band configuration failed after retries: {e}. Dummy KPI stored."
                )
                command_failure_count += 1
                readings.append(dummy_kpi)
                continue

        # ── Step 3: Detach for LTE (Mode A only, sent once between loops) ─────
        # AT+COPS=2 detaches from the NR5G network so the modem can be directed
        # to specific LTE bands without NR5G interference.
        # Sent exactly once here — never inside the LTE loop below.
        print("[SESSION][A] Sending AT+COPS=2 — detaching from NR5G for LTE scanning...")
        send_cops_command_until_success('AT+COPS=2', timeout=180)
        print("[SESSION][A] AT+COPS=2 accepted — waiting 10 seconds before LTE loop...")
        time.sleep(10)

    # ══════════════════════════════════════════════════════════════════════════
    # Mode B — LTE Only
    # ══════════════════════════════════════════════════════════════════════════
    else:

        # ── Step 1: Detach before LTE scanning ───────────────────────────────
        # In LTE-only mode there is no NR5G loop, so AT+COPS=2 runs here at
        # the very start — once, before the LTE loop, never again this session.
        print("[SESSION][B] Sending AT+COPS=2 — detaching for LTE-only band scanning...")
        send_cops_command_until_success('AT+COPS=2', timeout=180)
        print("[SESSION][B] AT+COPS=2 accepted — waiting 10 seconds before LTE loop...")
        time.sleep(10)

    # ══════════════════════════════════════════════════════════════════════════
    # LTE Band Loop — shared by both Mode A and Mode B
    # AT+COPS=2 has already been sent exactly once above before reaching here.
    # ══════════════════════════════════════════════════════════════════════════
    for band in lte_bands:

        band_num  = band[1:]
        dummy_kpi = LTEKPI(
            timestamp = datetime.now(),
            rat       = "LTE",
            band      = int(band_num),
            pci       = 9999,
            earfcn    = 9999,
            rsrp      = 9999,
            rsrq      = 9999,
            rssi      = 9999,
            sinr      = 9999,
        )

        # ── Pre-flight CFUN state check ───────────────────────────────────────
        # Identical logic to the NR5G loop pre-flight above — queries CFUN state
        # and only restores full functionality if needed. Prevents a cascaded
        # failure across LTE bands if a previous CFUN cycle left the modem in
        # CFUN=0.
        try:
            cfun_response = send_at_command_with_retry("AT+CFUN?", 5)
            cfun_value    = None

            for line in cfun_response.strip().split('\n'):
                line = line.strip()
                if line.startswith('+CFUN:'):
                    try:
                        cfun_value = int(line.replace('+CFUN:', '').strip())
                    except ValueError:
                        pass
                    break

            if cfun_value != 1:
                print(f"[LTE] Band {band}: modem in CFUN={cfun_value} — "
                      f"restoring full functionality...")
                send_at_command_with_retry("AT+CFUN=1", 15)
                time.sleep(3)
            else:
                print(f"[LTE] Band {band}: modem already in full functionality — "
                      f"pre-flight skipped.")

        except Exception as preflight_e:
            print(f"[LTE] Band {band}: pre-flight CFUN check failed: {preflight_e} "
                  f"— attempting AT+CFUN=1 as precaution.")
            try:
                send_at_command_with_retry("AT+CFUN=1", 15)
                time.sleep(3)
            except Exception as restore_e:
                print(f"[LTE] Band {band}: pre-flight CFUN=1 also failed: {restore_e} "
                      f"— proceeding to band attempt.")

        try:
            print(f"[LTE] Configuring band {band}...")
            send_at_command_with_retry(AT_CMD_LTE_BAND_CONFIG + band_num, 0.3)
            time.sleep(2)
            print(send_at_command_with_retry("AT+CFUN=0", 15))
            time.sleep(3)
            print(send_at_command_with_retry("AT+CFUN=1", 15))
            time.sleep(3)

            # ── Serving Cell Query with Retry ─────────────────────────────────
            # SEARCH is a transient state — the modem may still be scanning.
            # We try up to 3 times with a 1 second wait between each attempt.
            kpi = None
            for attempt in range(3):
                raw_response = send_at_command_with_retry(AT_CMD_SERVING_CELL, 0.3)
                kpi          = parse_serving_cell(raw_response, band)
                if kpi is not None:
                    break
                print(f"[LTE] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                time.sleep(1)

            # Band verification — confirms the returned KPI actually belongs
            # to the band we configured. If the modem returned a different band,
            # we treat it as no valid reading.
            if kpi is not None and kpi.band != int(band_num):
                print(f"[LTE] Band {band}: returned wrong band "
                      f"(got band={kpi.band}) — storing dummy.")
                kpi = None

            if kpi is None:
                print(f"[LTE] Band {band}: no cell found after 3 attempts — storing dummy KPI.")
                readings.append(dummy_kpi)
            else:
                print(f"[LTE] Band {band}: collected — "
                      f"RSRP={kpi.rsrp}, RSRQ={kpi.rsrq}, SINR={kpi.sinr}")
                readings.append(kpi)

        except Exception as e:
            # Band configuration failed after all retries — log, trap, store
            # dummy to preserve index alignment, and continue to next band.
            print(f"[LTE] Band {band}: failed — {e} — storing dummy KPI, continuing.")
            send_runtime_alarm(
                f"LTE band {band}",
                f"Band configuration failed after retries: {e}. Dummy KPI stored."
            )
            command_failure_count += 1
            readings.append(dummy_kpi)
            continue

    # ══════════════════════════════════════════════════════════════════════════
    # Post-Session Reset — Mode A only
    # ══════════════════════════════════════════════════════════════════════════
    # Reset to auto-registration so the next session can reach NR5G cells.
    # Mode B skips this entirely — there is no NR5G to prepare for.
    if mode_a:
        print("[SESSION][A] Sending AT+COPS=0 — resetting modem for next session NR5G...")
        send_cops_command_until_success('AT+COPS=0', timeout=180)
        print("[SESSION][A] AT+COPS=0 accepted — waiting 10 seconds...")
        time.sleep(10)

    # ── Package and return ────────────────────────────────────────────────────
    # Wrap the completed readings list into a SamplingSession so the
    # outer loop can accumulate 5 sessions before passing to process_window.
    return SamplingSession(
        session_start = session_start,
        readings      = readings,
    ), command_failure_count