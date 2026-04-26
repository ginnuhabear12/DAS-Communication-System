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

# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


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

    The RM520N-GL returns two distinct response formats per the Quectel
    RG520N&RM5x0N AT Commands Manual depending on active network mode:

    Single-line (LTE-only or NR5G-SA mode):
        +QENG: "servingcell",<state>,"LTE",<is_tdd>,<MCC>,<MNC>,<cellID>,
               <PCID>,<earfcn>,<freq_band_ind>,<UL_bw>,<DL_bw>,<TAC>,
               <RSRP>,<RSRQ>,<RSSI>,<SINR>,<CQI>,<tx_power>,<srxlev>

        Field indices (0-based, after stripping +QENG: and quotes):
        [0]=servingcell [1]=state [2]=RAT [3]=is_tdd [4]=MCC [5]=MNC
        [6]=cellID [7]=PCID [8]=earfcn [9]=freq_band_ind [10]=UL_bw
        [11]=DL_bw [12]=TAC [13]=RSRP [14]=RSRQ [15]=RSSI [16]=SINR

    Multi-line (EN-DC / LTE + NR5G-NSA mode):
        +QENG: "servingcell",<state>
        +QENG: "LTE",<is_tdd>,<MCC>,<MNC>,<cellID>,<PCID>,<earfcn>,
               <freq_band_ind>,<UL_bw>,<DL_bw>,<TAC>,<RSRP>,<RSRQ>,
               <RSSI>,<SINR>,<CQI>,<tx_power>,<srxlev>
        +QENG: "NR5G-NSA",...  (not used — NR5G NSA collected separately)

        Field indices for LTE data line (0-based, after stripping +QENG: and quotes):
        [0]=RAT [1]=is_tdd [2]=MCC [3]=MNC [4]=cellID [5]=PCID
        [6]=earfcn [7]=freq_band_ind [8]=UL_bw [9]=DL_bw [10]=TAC
        [11]=RSRP [12]=RSRQ [13]=RSSI [14]=SINR

    Args:
        raw_response: Cleaned string from at_command_comms().
        band:         Band string from configured bands e.g. 'b2', 'n2'.
    """

    # SEARCH means the modem found no cell on this band — caller stores sentinel
    if "SEARCH" in raw_response:
        print(f"{_ts()} [PARSER] Band {band}: modem in SEARCH state — no cell found.")
        return None

    try:
        # ── Step 1: Collect all +QENG lines from the response ─────────────────
        qeng_lines = [
            line.strip()
            for line in raw_response.strip().split('\n')
            if line.strip().startswith('+QENG')
        ]

        if not qeng_lines:
            print(f"{_ts()} [PARSER] Band {band}: no +QENG line found in response.")
            return None

        # ── Step 2: Detect format from the first line ─────────────────────────
        # Single-line: first line contains RAT at field index 2
        #   e.g. "servingcell","NOCONN","LTE","FDD",...
        # Multi-line (EN-DC): first line is state header only, LTE data follows
        #   e.g. "servingcell","NOCONN"  then  "LTE","FDD",...
        first_clean  = qeng_lines[0].replace('+QENG:', '').replace('"', '').strip()
        first_fields = [f.strip() for f in first_clean.split(',')]

        KNOWN_RATS = ('LTE', 'NR5G-SA', 'NR5G-NSA', 'WCDMA', 'GSM')

        if len(first_fields) >= 3 and first_fields[2] in KNOWN_RATS:
            # ── Single-line format ────────────────────────────────────────────
            # LTE-only or NR5G-SA mode — all fields on one line including the
            # "servingcell" and state prefix. Use first line as the data line.
            data_line   = qeng_lines[0]
            single_line = True

        elif len(qeng_lines) >= 2:
            # ── Multi-line format (EN-DC) ─────────────────────────────────────
            # Modem is in LTE + NR5G-NSA mode. First line is the state header
            # only. LTE KPI data is on the second +QENG line, which has no
            # "servingcell" or state prefix — indices shift by -2 relative to
            # single-line format. NR5G-NSA third line is ignored here since
            # NR5G bands are collected separately via their own band loop.
            data_line   = qeng_lines[1]
            single_line = False

        else:
            print(f"{_ts()} [PARSER] Band {band}: state header found but no data line follows.")
            return None

        # ── Step 3: Clean and split the selected data line ────────────────────
        clean    = data_line.replace('+QENG:', '').replace('"', '').strip()
        raw_list = clean.split(',')

        # ── Step 4: Convert each field to int/float where possible ───────────
        # '-' fields (unavailable values from modem) → sentinel 9999 so that
        # alarms.py INVALID_SENTINEL check fires naturally for those fields.
        parts = []
        for item in raw_list:
            item = item.strip()
            if item == '-':
                print(f"{_ts()} [PARSER] Band {band}: '-' value encountered — substituting sentinel 9999.")
                parts.append(9999)
                continue
            try:
                parts.append(int(item))
            except ValueError:
                try:
                    parts.append(float(item))
                except ValueError:
                    parts.append(item)  # keep as string e.g. "LTE", "FDD"

        # ── Step 5: Extract KPI fields using format-specific indices ──────────

        if single_line:
            # Indices include "servingcell" and state at [0] and [1]
            # RAT is at [2], all KPI fields offset by +2 vs multi-line
            rat = parts[2]

            if rat == "LTE":
                raw_sinr = parts[16] if len(parts) > 16 else 9999
                if isinstance(raw_sinr, (int, float)) and raw_sinr != 9999:
                    converted_sinr = (0.2 * raw_sinr * 10) - 20
                else:
                    converted_sinr = 9999.0

                return LTEKPI(
                    timestamp = datetime.now(),
                    rat       = parts[2],
                    band      = parts[9],   # freq_band_ind
                    pci       = parts[7],   # PCID
                    earfcn    = parts[8],   # earfcn
                    rsrp      = parts[13],
                    rsrq      = parts[14],
                    rssi      = parts[15],
                    sinr      = converted_sinr,
                )

            elif rat == "NR5G-SA":
                # NR5G-SA is always single-line per Quectel manual
                # SINR is already in dB for NR5G — no conversion needed
                return NR5GKPI(
                    timestamp = datetime.now(),
                    rat       = parts[2],
                    band      = parts[10],  # band
                    pci       = parts[7],   # PCID
                    arfcn     = parts[9],   # ARFCN
                    ss_rsrp   = parts[12],
                    ss_rsrq   = parts[13],
                    ss_sinr   = parts[14],
                )

            else:
                print(f"{_ts()} [PARSER] Band {band}: unrecognized RAT '{rat}' in single-line response.")
                return None

        else:
            # Multi-line EN-DC — second line has no "servingcell" or state prefix
            # RAT is at [0], all KPI fields shifted -2 relative to single-line
            rat = parts[0]

            if rat == "LTE":
                raw_sinr = parts[14] if len(parts) > 14 else 9999
                if isinstance(raw_sinr, (int, float)) and raw_sinr != 9999:
                    converted_sinr = (0.2 * raw_sinr * 10) - 20
                else:
                    converted_sinr = 9999.0

                return LTEKPI(
                    timestamp = datetime.now(),
                    rat       = parts[0],
                    band      = parts[7],   # freq_band_ind
                    pci       = parts[5],   # PCID
                    earfcn    = parts[6],   # earfcn
                    rsrp      = parts[11],
                    rsrq      = parts[12],
                    rssi      = parts[13],
                    sinr      = converted_sinr,
                )

            else:
                print(f"{_ts()} [PARSER] Band {band}: unrecognized RAT '{rat}' in multi-line response.")
                return None

    except (IndexError, ValueError, TypeError) as e:
        print(f"{_ts()} [PARSER] Band {band}: failed to parse. Error: {e}")
        print(f"         Raw was: {raw_response[:200]}")
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
            # Re-raise unconditionally. USB absence is tracked by a session-level
            # serial_failure_count returned from instKPIcollection and evaluated
            # in full_script.py after each session completes. Triggering an
            # immediate restart here was too aggressive for transient disconnects.
            raise

        if response not in ("ERROR", "TIMEOUT"):
            return response

        reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
        print(f"{_ts()} [RETRY] Attempt {attempt + 1}/{max_retries} — {command} {reason}.")
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
    print(f"{_ts()} [RESTART] {reason}")
    print(f"{_ts()} [RESTART] Sending trap and rebooting Pi...")
    try:
        send_runtime_alarm(
            component = "Pi restart",
            detail    = reason
        )
    except Exception as e:
        # Trap failure must never block the restart — log and proceed.
        print(f"{_ts()} [RESTART] Trap send failed: {e} — proceeding with reboot anyway.")
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
                    print(f"{_ts()} [COPS] {command} succeeded on attempt {attempt} "
                          f"({elapsed:.0f}s elapsed).")
                return response

            # Modem returned ERROR or timed out — fall through to retry logic below
            reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
            print(f"{_ts()} [COPS] Attempt {attempt} — {command} {reason}.")

        except Exception as e:
            # at_command_comms itself raised (e.g. serial port dropped) —
            # treat identically to an ERROR response and keep retrying.
            print(f"{_ts()} [COPS] Attempt {attempt} — {command} raised exception: {e}.")

        # ── Alert and restart checks (time-based) ────────────────────────────
        # Alert fires once at 120s — notifies operator the command is failing.
        # Restart fires at 300s (5 minutes) — if the modem hasn't responded in
        # 4 minutes it is not recoverable without a power cycle. The Pi restart
        # resets the USB bus and modem power, which is the only viable recovery.
        elapsed = time.time() - start_time

        if not alert_sent and elapsed >= _CRITICAL_ALERT_TIMEOUT:
            alert_detail = (
                f"no modem response after {elapsed:.0f}s — "
                f"script is retrying every {_CRITICAL_RETRY_ESCALATED_SLEEP}s"
            )
            print(f"{_ts()} [COPS] ALERT — {command} has been failing for {elapsed:.0f}s. "
                  f"Sending runtime alarm to Infolink...")
            try:
                send_runtime_alarm(component=command, detail=alert_detail)
            except Exception as snmp_e:
                print(f"{_ts()} [COPS] Runtime alarm send failed: {snmp_e} — continuing retries.")
            alert_sent = True

        if elapsed >= _CRITICAL_RESTART_TIMEOUT:
            _trigger_modem_restart(
                f"{command} has not responded after {elapsed:.0f}s — "
                f"modem is unresponsive. Restarting Pi to recover."
            )

        # ── Sleep before next attempt ─────────────────────────────────────────
        # Use escalated interval once the alert has fired, initial interval before
        sleep_time = _CRITICAL_RETRY_ESCALATED_SLEEP if alert_sent else _CRITICAL_RETRY_INITIAL_SLEEP
        print(f"{_ts()} [COPS] Retrying {command} in {sleep_time}s...")
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
                    print(f"{_ts()} [CFUN] {command} succeeded on attempt {attempt} "
                          f"({elapsed:.0f}s elapsed).")
                return response

            # Modem returned ERROR or timed out — fall through to retry logic below
            reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
            print(f"{_ts()} [CFUN] Attempt {attempt} — {command} {reason}.")

        except Exception as e:
            # at_command_comms itself raised (e.g. serial port dropped) —
            # treat identically to an ERROR response and keep retrying.
            print(f"{_ts()} [CFUN] Attempt {attempt} — {command} raised exception: {e}.")

        # ── Alert and restart checks (time-based) ────────────────────────────
        # Alert fires once at 120s — notifies operator the command is failing.
        # Restart fires at 300s (5 minutes) — same reasoning as COPS restart.
        elapsed = time.time() - start_time

        if not alert_sent and elapsed >= _CRITICAL_ALERT_TIMEOUT:
            alert_detail = (
                f"no modem response after {elapsed:.0f}s — "
                f"script is retrying every {_CRITICAL_RETRY_ESCALATED_SLEEP}s"
            )
            print(f"{_ts()} [CFUN] ALERT — {command} has been failing for {elapsed:.0f}s. "
                  f"Sending runtime alarm to Infolink...")
            try:
                send_runtime_alarm(component=command, detail=alert_detail)
            except Exception as snmp_e:
                print(f"{_ts()} [CFUN] Runtime alarm send failed: {snmp_e} — continuing retries.")
            alert_sent = True

        if elapsed >= _CRITICAL_RESTART_TIMEOUT:
            _trigger_modem_restart(
                f"{command} has not responded after {elapsed:.0f}s — "
                f"modem is unresponsive. Restarting Pi to recover."
            )

        # ── Sleep before next attempt ─────────────────────────────────────────
        sleep_time = _CRITICAL_RETRY_ESCALATED_SLEEP if alert_sent else _CRITICAL_RETRY_INITIAL_SLEEP
        print(f"{_ts()} [CFUN] Retrying {command} in {sleep_time}s...")
        time.sleep(sleep_time)


# ══════════════════════════════════════════════════════════════════════════════
# Segment 3D — SIM Card Detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_sim() -> bool:
    """
    Determines whether a SIM card is currently inserted in the modem.

    Uses AT+QSIMSTAT? as the primary check. This command returns
    +QSIMSTAT: <enable>,<sim_inserted> where sim_inserted = 1 means
    a card is present, 0 means no card. The command returns OK even
    without a SIM, making it reliable for state polling.

    Falls back to AT+CPIN? if QSIMSTAT fails or cannot be parsed:
        +CPIN: READY / SIM PIN / SIM PUK  → SIM present
        ERROR                              → SIM not inserted (CME ERROR: 10)

    In case of ambiguous failure (both commands fail) returns False.
    This is intentional — it is safer to skip COPS than to hang.

    Returns:
        True  if a SIM card is detected.
        False if no SIM is detected, or if detection is indeterminate.
    """
    try:
        response = send_at_command_with_retry("AT+QSIMSTAT?", timeout=5)

        if response not in ("ERROR", "TIMEOUT"):
            for line in response.strip().split('\n'):
                line = line.strip()
                if '+QSIMSTAT:' in line:
                    parts = line.replace('+QSIMSTAT:', '').strip().split(',')
                    if len(parts) >= 2:
                        try:
                            sim_inserted = int(parts[1].strip())
                            detected = (sim_inserted == 1)
                            print(
                                f"{_ts()} [SIM] AT+QSIMSTAT? → inserted={sim_inserted} "
                                f"({'SIM detected' if detected else 'no SIM'})"
                            )
                            return detected
                        except ValueError:
                            pass

        # AT+QSIMSTAT? failed or unparseable — fall back to AT+CPIN?
        print(f"{_ts()} [SIM] AT+QSIMSTAT? inconclusive — falling back to AT+CPIN?...")
        cpin_response = send_at_command_with_retry("AT+CPIN?", timeout=5)

        if cpin_response in ("ERROR", "TIMEOUT"):
            # CME ERROR: 10 (SIM not inserted) arrives here as "ERROR" because
            # at_command_comms() matches "ERROR" in the raw response string.
            print(f"{_ts()} [SIM] AT+CPIN? returned {cpin_response} — treating as no SIM.")
            return False

        if any(tok in cpin_response for tok in ("READY", "SIM PIN", "SIM PUK")):
            print(f"{_ts()} [SIM] AT+CPIN? → SIM detected ({cpin_response.strip()[:30]}).")
            return True

        print(
            f"{_ts()} [SIM] AT+CPIN? response unrecognized "
            f"('{cpin_response[:40]}') — assuming no SIM."
        )
        return False

    except serial.SerialException as e:
        # Serial port unreachable — cannot determine SIM state.
        # Return False; the serial failure counters in full_script.py handle this.
        print(f"{_ts()} [SIM] detect_sim() serial error: {e} — assuming no SIM.")
        return False

    except Exception as e:
        print(f"{_ts()} [SIM] detect_sim() failed: {e} — assuming no SIM to prevent COPS hang.")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# Segment 3C — Instantaneous KPI Collection
# ══════════════════════════════════════════════════════════════════════════════

def instKPIcollection(nr5g_bands, lte_bands, sim_present: bool = True):
    """
    Performs one full KPI collection pass across all configured bands
    and returns the results packaged as a SamplingSession.

    This function is called 5 times by the outer loop to build the
    list of 5 SamplingSession objects the averaging script expects.

    Collection mode is determined automatically by SIM state and whether
    nr5g_bands is populated:

        Mode A — NR5G + LTE (sim_present=True, nr5g_bands is not empty):
            AT+COPS=0  →  NR5G band loop  →  AT+COPS=2  →  LTE band loop
            →  AT+COPS=0 reset for next session

        Mode B — LTE Only, SIM present (sim_present=True, nr5g_bands is empty):
            AT+COPS=2  →  LTE band loop  →  done (no reset needed)

        Mode C — LTE Only, No SIM (sim_present=False):
            LTE band loop only — no COPS commands of any kind.
            Without a SIM the modem camps on LTE cells in LIMSRV state
            (registered for emergency calls only) and returns real RF
            measurements (RSRP, RSRQ, RSSI, SINR) without network
            registration. AT+COPS=2 and AT+COPS=0 both return ERROR
            without a SIM and must never be called. NR5G bands are
            skipped entirely regardless of configuration — NR5G NSA
            requires an LTE anchor registration, and NR5G SA requires
            full 5G registration; neither is possible without a SIM.

    AT+COPS commands always use send_cops_command_until_success so they
    retry indefinitely and never crash the script. They are never called
    in Mode C.
    AT+COPS=2 is sent exactly once per session in Modes A and B — never
    inside any band loop.

    Args:
        nr5g_bands:  List of NR5G band strings e.g. ['n2', 'n66'].
                     Pass an empty list [] for LTE-only modes.
                     Ignored entirely in Mode C (no SIM).
        lte_bands:   List of LTE band strings  e.g. ['b2', 'b5', 'b12'].
        sim_present: True if a SIM card is detected (default).
                     False forces Mode C regardless of nr5g_bands — COPS
                     commands are skipped and only LTE is collected.

    Returns:
        A SamplingSession containing one reading per band,
        NR5G readings first (if any, Mode A only), LTE readings second.
    """

    session_start = datetime.now()
    readings      = []
    command_failure_count = 0  # Tracks bands that failed via AT command exception
                               # (not bands that found no cell — those are normal).
                               # Returned to the main loop to detect modem-level
                               # failure patterns across consecutive sessions.
    # serial_failure_count — bands that failed because SerialException was raised,
    # meaning the serial port itself was unreachable at the hardware level.
    # Tracked separately from command failures so full_script.py can distinguish
    # a USB disconnect from a modem logic failure and send the appropriate alarm.
    serial_failure_count = 0

    # ── Mode Detection ────────────────────────────────────────────────────────
    # Mode A requires both NR5G bands configured AND a SIM present.
    # Without a SIM, NR5G cannot be collected regardless of configuration,
    # so mode_a is False even when nr5g_bands is populated. Mode C handles
    # the no-SIM case as an entirely separate branch below.
    # mode_a = True  → NR5G + LTE collection (SIM present, NR5G configured)
    # mode_a = False → LTE only or no-SIM collection
    # mode_c = True  → LTE only, no SIM (COPS skipped, LIMSRV state)
    mode_a = bool(nr5g_bands) and sim_present
    mode_c = not sim_present

    if mode_a:
        print(f"\n{_ts()} [SESSION] Mode A (NR5G + LTE)  — starting collection at {session_start}")
    elif mode_c:
        print(f"\n{_ts()} [SESSION] Mode C (LTE Only, No SIM) — starting collection at {session_start}")
    else:
        print(f"\n{_ts()} [SESSION] Mode B (LTE Only)    — starting collection at {session_start}")

    # ══════════════════════════════════════════════════════════════════════════
    # Mode A — NR5G + LTE
    # ══════════════════════════════════════════════════════════════════════════
    if mode_a:

        # ── Step 1: Enable auto-registration for NR5G ─────────────────────────
        # AT+COPS=0 tells the modem to register on any available network
        # including NR5G cells. Retried indefinitely — script cannot proceed
        # with NR5G collection until the modem accepts this.
        print(f"{_ts()} [SESSION][A] Sending AT+COPS=0 — enabling auto-registration for NR5G...")
        send_cops_command_until_success('AT+COPS=0', timeout=180)
        print(f"{_ts()} [SESSION][A] AT+COPS=0 accepted — proceeding with NR5G band loop.")

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
                    print(f"{_ts()} [NR5G] Band {band}: modem in CFUN={cfun_value} — "
                          f"restoring full functionality...")
                    send_at_command_with_retry("AT+CFUN=1", 15)
                    time.sleep(3)
                else:
                    print(f"{_ts()} [NR5G] Band {band}: modem already in full functionality — "
                          f"pre-flight skipped.")

            except Exception as preflight_e:
                # Query or restore failed — state is unknown.
                # Attempt CFUN=1 as a precaution before the band try block.
                print(f"{_ts()} [NR5G] Band {band}: pre-flight CFUN check failed: {preflight_e} "
                      f"— attempting AT+CFUN=1 as precaution.")
                try:
                    send_at_command_with_retry("AT+CFUN=1", 15)
                    time.sleep(3)
                except Exception as restore_e:
                    # Both the check and the restore failed — log and proceed.
                    # The main try block below will fail naturally if the modem
                    # is unresponsive and the dummy will be stored as normal.
                    print(f"{_ts()} [NR5G] Band {band}: pre-flight CFUN=1 also failed: {restore_e} "
                          f"— proceeding to band attempt.")

            try:
                print(f"{_ts()} [NR5G] Configuring band {band}...")
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
                    print(f"{_ts()} [NR5G] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                    time.sleep(1)

                # RAT and band verification — confirms the returned KPI actually
                # belongs to the band we configured. If the modem fell back to a
                # different RAT or band, we treat it as no valid reading.
                if kpi is not None and (not isinstance(kpi, NR5GKPI) or kpi.band != int(band_num)):
                    print(f"{_ts()} [NR5G] Band {band}: returned wrong RAT or band "
                          f"(got RAT={kpi.rat}, band={kpi.band}) — storing dummy.")
                    kpi = None

                if kpi is None:
                    print(f"{_ts()} [NR5G] Band {band}: no cell found after 3 attempts — storing dummy KPI.")
                    readings.append(dummy_kpi)
                else:
                    print(f"{_ts()} [NR5G] Band {band}: collected — "
                          f"SS-RSRP={kpi.ss_rsrp}, SS-RSRQ={kpi.ss_rsrq}, SS-SINR={kpi.ss_sinr}")
                    readings.append(kpi)

            except serial.SerialException as e:
                # Serial port unreachable — counted separately from AT command
                # failures so full_script.py can route to the USB diagnostic path.
                # No per-band runtime alarm here: the USB counter in full_script.py
                # sends a single alarm when the threshold is reached, avoiding
                # a flood of individual band traps for what is one hardware event.
                print(f"{_ts()} [NR5G] Band {band}: serial port failure — {e} — storing dummy KPI, continuing.")
                serial_failure_count += 1
                readings.append(dummy_kpi)
                continue

            except Exception as e:
                # AT command failure (ERROR, TIMEOUT, or unexpected exception) —
                print(f"{_ts()} [NR5G] Band {band}: AT command failure — {e} — storing dummy KPI, continuing.")
                print(f"[NR5G] Band {band}: AT command failure — {e} — storing dummy KPI, continuing.")
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
    # Mode C — LTE Only, No SIM
    # ══════════════════════════════════════════════════════════════════════════
    # Without a SIM the modem is in LIMSRV state — camped on LTE cells but
    # not registered. AT+COPS=2 and AT+COPS=0 both return ERROR without a
    # SIM and would cause send_cops_command_until_success to hang indefinitely.
    # Neither is sent here. The LTE band loop below runs identically to
    # Modes A and B — CFUN cycling, QENG queries, and band verification all
    # function normally in LIMSRV state. The 10-second inter-loop sleep used
    # in Modes A and B after AT+COPS=2 is also skipped — that sleep exists
    # to allow the network detach to settle, which does not apply here since
    # no detach command is issued and the modem is already in limited service.
    elif mode_c:
        print(f"[SESSION][C] No SIM detected — skipping AT+COPS=2. "
              f"LTE bands will scan in LIMSRV state and return real RF measurements.")

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
    # LTE Band Loop — shared by Mode A, Mode B, and Mode C
    # In Modes A and B, AT+COPS=2 has already been sent exactly once above
    # before reaching here. In Mode C, no COPS command is sent — the modem
    # is already in LIMSRV state and the loop runs directly.
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
                print(f"{_ts()} [LTE] Band {band}: modem in CFUN={cfun_value} — "
                      f"restoring full functionality...")
                send_at_command_with_retry("AT+CFUN=1", 15)
                time.sleep(3)
            else:
                print(f"{_ts()} [LTE] Band {band}: modem already in full functionality — "
                      f"pre-flight skipped.")

        except Exception as preflight_e:
            print(f"{_ts()} [LTE] Band {band}: pre-flight CFUN check failed: {preflight_e} "
                  f"— attempting AT+CFUN=1 as precaution.")
            try:
                send_at_command_with_retry("AT+CFUN=1", 15)
                time.sleep(3)
            except Exception as restore_e:
                print(f"{_ts()} [LTE] Band {band}: pre-flight CFUN=1 also failed: {restore_e} "
                      f"— proceeding to band attempt.")

        try:
            print(f"{_ts()} [LTE] Configuring band {band}...")
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
                print(f"{_ts()} [LTE] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                time.sleep(1)

            # Band verification — confirms the returned KPI actually belongs
            # to the band we configured. If the modem returned a different band,
            # we treat it as no valid reading.
            if kpi is not None and kpi.band != int(band_num):
                print(f"{_ts()} [LTE] Band {band}: returned wrong band "
                      f"(got band={kpi.band}) — storing dummy.")
                kpi = None

            if kpi is None:
                print(f"{_ts()} [LTE] Band {band}: no cell found after 3 attempts — storing dummy KPI.")
                readings.append(dummy_kpi)
            else:
                print(f"{_ts()} [LTE] Band {band}: collected — "
                      f"RSRP={kpi.rsrp}, RSRQ={kpi.rsrq}, SINR={kpi.sinr}")
                readings.append(kpi)

        except serial.SerialException as e:
            print(f"{_ts()} [LTE] Band {band}: serial port failure — {e} — storing dummy KPI, continuing.")
            serial_failure_count += 1
            readings.append(dummy_kpi)
            continue

        except Exception as e:
            print(f"{_ts()} [LTE] Band {band}: AT command failure — {e} — storing dummy KPI, continuing.")
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
    ), command_failure_count, serial_failure_count