"""
Module: standin_kpi_collection.py
Purpose: Instantaneous KPI collection — queries the modem band by band and
         builds a SamplingSession containing one KPI reading per band.
         Called repeatedly by full_script.py to accumulate the 5 sessions
         that process_window() needs before averaging can occur.

         Three collection modes are supported depending on SIM presence
         and configured band types:
             Mode A — NR5G + LTE  (SIM present, NR5G bands configured)
             Mode B — LTE Only    (SIM present, no NR5G bands)
             Mode C — LTE Only, No SIM  ← only mode tested and validated

         Modes A and B involve AT+COPS commands that have NOT been tested
         against hardware. Mode C has been exercised in all deployed runs
         and is the only mode confirmed to work correctly end-to-end.

Dependencies: models.py, constants.py, modem.py, snmpSend.py
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


# ── LTE SINR Conversion Note ───────────────────────────────────────────────────────
# The raw SINR integer returned by AT+QENG for LTE is NOT in dB.
# It must be converted using the formula confirmed by Quectel support:
#     converted_dB = (1/5) × raw × 10 − 20
#     simplified:   converted_dB = (0.2 × raw × 10) − 20
#
# Example: raw value 16 → (0.2 × 16 × 10) − 20 = 12 dB
#
# This conversion is applied in parse_serving_cell() wherever LTE SINR
# is extracted — see the single-line and multi-line branches in Section C.
# NR5G SS-SINR is already returned in dB by the modem — no conversion needed.

# ══════════════════════════════════════════════════════════════════════════════
# Response Parser
# ══════════════════════════════════════════════════════════════════════════════
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

    NOTE: Only the single-line LTE path has been tested against hardware.
          The multi-line EN-DC path and NR5G-SA path are implemented but
          untested — validate before relying on them in production.

    Args:
        raw_response: Cleaned response string from at_command_comms().
                      Should not contain the trailing "OK" (already stripped
                      by at_command_comms before returning).
        band:         Band label string from the configured band list,
                      e.g. 'b2' for LTE Band 2, 'n78' for NR5G Band 78.
                      Used only for log messages — not parsed here.
    """

    # SEARCH means the modem found no cell on this band — this is normal when
    # a DAS port is inactive or the band is not deployed at this site.
    # Return None so the caller stores a dummy KPI sentinel rather than crashing.
    if "SEARCH" in raw_response:
        print(f"{_ts()} [PARSER] Band {band}: modem in SEARCH state — no cell found.")
        return None

    try:
        # ── Step 1: Collect all +QENG lines from the response ─────────────────
        # The raw response may contain blank lines, echo of the command, or
        # other AT output. We only want lines that start with '+QENG'.
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

        # All RAT strings the modem may return — used to determine format
        KNOWN_RATS = ('LTE', 'NR5G-SA', 'NR5G-NSA', 'WCDMA', 'GSM')

        if len(first_fields) >= 3 and first_fields[2] in KNOWN_RATS:
            # ── Single-line format ────────────────────────────────────────────
            # The first line contains both the state prefix ("servingcell", state)
            # AND the RAT and KPI fields. Use it directly as the data line.
            data_line   = qeng_lines[0]
            single_line = True

        elif len(qeng_lines) >= 2:
            # ── Multi-line format (EN-DC) ─────────────────────────────────────
            # The first line is only "servingcell","state" with no KPI data.
            # The actual LTE KPI fields are on the second +QENG line, which
            # starts directly with the RAT — no "servingcell" or state prefix.
            # This shifts all field indices down by 2 compared to single-line.
            # The NR5G-NSA third line is intentionally ignored — NR5G bands
            # are collected separately via their own band loop in instKPIcollection.
            data_line   = qeng_lines[1]
            single_line = False

        else:
            print(f"{_ts()} [PARSER] Band {band}: state header found but no data line follows.")
            return None

        # ── Step 3: Clean and split the selected data line ────────────────────
        # Strip the "+QENG:" prefix and all double-quotes so we're left with
        # a plain comma-separated field list ready for indexing.
        clean    = data_line.replace('+QENG:', '').replace('"', '').strip()
        raw_list = clean.split(',')

        # ── Step 4: Convert each field to int/float where possible ───────────
        # '-' is the modem's placeholder for unavailable/not-applicable fields.
        # We substitute 9999 so that the INVALID_SENTINEL check in alarms.py
        # fires naturally for those fields without needing special-case handling.
        parts = []
        for item in raw_list:
            item = item.strip()
            if item == '-':
                print(f"{_ts()} [PARSER] Band {band}: '-' value encountered — substituting sentinel 9999.")
                parts.append(9999)
                continue
            try:
                parts.append(int(item))       # Try int first (most KPI fields are integers)
            except ValueError:
                try:
                    parts.append(float(item)) # Fall back to float for decimal values
                except ValueError:
                    parts.append(item)        # Keep as string for RAT labels, duplex mode, etc.

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
                # NOTE: This path is untested against hardware.
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
        # Catches malformed responses where expected fields are missing or
        # the type conversion above produced an unexpected result.
        print(f"{_ts()} [PARSER] Band {band}: failed to parse. Error: {e}")
        print(f"         Raw was: {raw_response[:200]}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# AT Command Retry Wrappers
# These functions wrap at_command_comms() with retry logic appropriate for
# different failure contexts — transient band errors, critical startup commands,
# and in-session COPS mode changes each have different tolerance for failure.
# ══════════════════════════════════════════════════════════════════════════════

# ── Standard Retry (3 attempts) ──────────────────────────────────
def send_at_command_with_retry(command, timeout, max_retries=3):
    """
    Send an AT command and retry up to max_retries times on ERROR or TIMEOUT.

    Used for all non-critical AT commands within the band loops (band config,
    CFUN cycling, QENG queries). Gives the modem up to 3 chances before
    raising an Exception, which the band loop catches and converts to a
    dummy KPI so the session can continue even if one band fails.

    Serial port failures (SerialException) are re-raised immediately without
    retrying — USB-level disconnects are tracked separately via
    serial_failure_count in instKPIcollection() and evaluated by full_script.py
    after each session. Retrying a dead serial port wastes time and masks the
    underlying hardware problem.

    Args:
        command:     AT command string to send (e.g. 'AT+QENG="servingcell"').
        timeout:     Seconds to wait for a response, passed to at_command_comms.
        max_retries: Maximum number of attempts before raising. Default 3.

    Returns:
        The modem's cleaned response string on success.

    Raises:
        serial.SerialException: Immediately, without retrying, on USB failure.
        Exception: After all retry attempts return ERROR or TIMEOUT.
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


# ── Indefinite Retry for Critical Commands (startup only) ────────
# These constants control the timing and escalation behavior of the two
# indefinite-retry functions below (send_cops_command_until_success and
# send_cfun_until_success). Adjust here only — both functions reference
# these values so a single change applies everywhere.

_CRITICAL_RETRY_INITIAL_SLEEP   = 10   # seconds between retries before alert fires
_CRITICAL_RETRY_ESCALATED_SLEEP = 30   # seconds between retries after alert fires
_CRITICAL_ALERT_TIMEOUT         = 120  # seconds elapsed before sending runtime alarm
_CRITICAL_RESTART_TIMEOUT       = 300  # seconds elapsed before triggering Pi restart (5 minutes)

# ── In-Session COPS Retry Constants ──────────────────────────────
# Used by send_cops_command_in_session() during active KPI collection.
# A hard attempt ceiling is used here (unlike the indefinite startup retry)
# so a failing COPS command cannot stall the collection session.
# Worst case: 3 attempts × near-instant ERROR + 2 × 10s sleep ≈ 20–25 seconds.
_IN_SESSION_COPS_MAX_ATTEMPTS = 3
_IN_SESSION_COPS_RETRY_SLEEP  = 10   # seconds between in-session retry attempts

def _trigger_modem_restart(reason: str) -> None:
    """
    Send an SNMP trap describing the restart reason, then reboot the Pi.

    Called when the modem has been unresponsive long enough that a full
    Pi reboot is the only viable recovery path. The reboot resets the USB
    bus and cycles modem power, which resolves hangs that AT command retries
    cannot fix. This function never returns — the reboot terminates the process.

    The 2-second sleep before reboot gives the SNMP trap UDP packet time to
    leave the network stack before the process is killed.

    Args:
        reason: Human-readable description of why the restart was triggered.
                Included verbatim in the SNMP trap detail field so the operator
                can see the cause in the Infolink alarm log.
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
    Send a COPS AT command and retry indefinitely until the modem accepts it.

    Reserved for startup use only (called from full_script.py before collection
    begins). During active collection, use send_cops_command_in_session() instead,
    which has a hard attempt ceiling to avoid stalling a session.

    This function NEVER raises — the caller is always guaranteed a successful
    response before execution continues.

    NOTE: This function path (COPS commands at startup) has not been tested
    against hardware. Only Mode C (no SIM, no COPS) has been validated.

    Retry escalation:
        Phase 1  — 0 to 120 seconds:
            Retries every 10 seconds, logging each failed attempt.
        At 120 seconds:
            Sends one SNMP runtime alarm to Infolink. Sent exactly once per
            call — not repeated on subsequent retries.
        Phase 2  — after 120 seconds:
            Continues retrying every 30 seconds indefinitely.
        At 300 seconds:
            Calls _trigger_modem_restart() — sends a final trap and reboots
            the Pi. The reboot is the only recovery option for a modem that
            has been completely unresponsive for 5 minutes.

    Both ERROR responses and exceptions from at_command_comms() are treated
    identically — logged and retried.

    Args:
        command: COPS command string — 'AT+COPS=0' or 'AT+COPS=2'.
        timeout: Per-attempt timeout passed to at_command_comms. Default 180s.
                 AT+COPS=0 can take a long time when it succeeds, hence the
                 long timeout. A failing command typically returns ERROR almost
                 immediately, so the timeout is not the bottleneck.

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

            # Modem returned ERROR or TIMEOUT — log reason and fall through to
            # alert/restart checks and sleep before next attempt.
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
            # Alert fires once — notifies operator the modem is not responding
            # to a critical startup command and the script is stuck in retry.
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
            # 5 minutes with no modem response — reboot is the only recovery.
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



# ── COPS State Query ─────────────────────────────────────────────
def check_cops_mode() -> int | None:
    """
    Query AT+COPS? to determine the modem's current network selection mode.

    Used at the start of each Mode A session to check whether the modem is
    already in auto-registration mode (COPS=0) from the previous session's
    post-session reset. If it is, the redundant AT+COPS=0 command is skipped,
    saving several seconds per session.
    
    The response format varies by registration state:
        Registered (auto):  +COPS: 0,2,"OperatorName",7
        Detached:           +COPS: 2
        Not registered:     +COPS: 0

    Only the first field (mode) is parsed — this covers all forms correctly.

    Mode values:
        0 = automatic selection (auto-registration)
        1 = manual selection
        2 = deregistered / detached
        3 = format-only (no mode change)
        4 = manual with automatic fallback

    Returns:
        int: The current mode value if successfully parsed.
        None: If AT+COPS? failed, timed out, or the response could not be parsed.
              Callers should treat None as "mode unknown" and proceed as if
              the modem is not in the desired state.
    """
    try:
        response = send_at_command_with_retry("AT+COPS?", timeout=10)

        if response in ("ERROR", "TIMEOUT"):
            print(f"{_ts()} [COPS] AT+COPS? returned {response} — mode unknown.")
            return None

        for line in response.strip().split('\n'):
            line = line.strip()
            if '+COPS:' in line:
                # Strip prefix and split on comma — first field is always mode
                parts = line.replace('+COPS:', '').strip().split(',')
                try:
                    mode = int(parts[0].strip())
                    print(f"{_ts()} [COPS] AT+COPS? → current mode = {mode}")
                    return mode
                except (ValueError, IndexError):
                    print(f"{_ts()} [COPS] AT+COPS? mode field unparseable: '{line}' — treating as unknown.")
                    return None

        print(f"{_ts()} [COPS] AT+COPS? response contained no +COPS: line — treating as unknown.")
        return None

    except Exception as e:
        print(f"{_ts()} [COPS] check_cops_mode() failed: {e} — treating as unknown.")
        return None


# ── In-Session COPS Command (Hard Attempt Ceiling) ───────────────

def send_cops_command_in_session(command: str, timeout: int = 180) -> bool:
    """
    Sends a COPS command during active KPI collection with a hard retry ceiling.

    Unlike send_cops_command_until_success (which retries indefinitely and is
    reserved for startup), this function makes exactly _IN_SESSION_COPS_MAX_ATTEMPTS
    attempts with _IN_SESSION_COPS_RETRY_SLEEP seconds between each. If all
    attempts fail it returns False so the caller can insert dummy KPI values
    for the affected RAT section and continue the session rather than stalling.

    Both ERROR responses and exceptions from at_command_comms are handled
    identically — logged and retried until the ceiling is reached.

    The per-attempt timeout is kept at 180s (same as startup) because AT+COPS=0
    can take a long time when it does succeed. The ceiling controls how many
    times we try, not how long each attempt runs. In practice a failing COPS
    command returns ERROR almost immediately, so the real worst case is roughly:
        3 attempts × near-instant ERROR + 2 × 10s sleep ≈ 20–25 seconds.

    Args:
        command: The COPS command string e.g. 'AT+COPS=0' or 'AT+COPS=2'.
        timeout: Per-attempt timeout passed to at_command_comms. Default 180s.

    Returns:
        True  if the command succeeded within the attempt ceiling.
        False if all attempts failed — caller must insert dummy values and
              send a runtime alarm for the affected RAT section.
    """
    for attempt in range(1, _IN_SESSION_COPS_MAX_ATTEMPTS + 1):
        try:
            response = at_command_comms(command, timeout)

            if response not in ("ERROR", "TIMEOUT"):
                if attempt > 1:
                    print(f"{_ts()} [COPS] {command} succeeded on attempt "
                          f"{attempt}/{_IN_SESSION_COPS_MAX_ATTEMPTS}.")
                return True

            reason = "returned ERROR" if response == "ERROR" else "timed out — no modem response"
            print(f"{_ts()} [COPS] In-session attempt {attempt}/{_IN_SESSION_COPS_MAX_ATTEMPTS} "
                  f"— {command} {reason}.")

        except Exception as e:
            print(f"{_ts()} [COPS] In-session attempt {attempt}/{_IN_SESSION_COPS_MAX_ATTEMPTS} "
                  f"— {command} raised exception: {e}.")

        if attempt < _IN_SESSION_COPS_MAX_ATTEMPTS:
            print(f"{_ts()} [COPS] Retrying {command} in {_IN_SESSION_COPS_RETRY_SLEEP}s...")
            time.sleep(_IN_SESSION_COPS_RETRY_SLEEP)

    # All attempts exhausted
    print(f"{_ts()} [COPS] {command} failed after {_IN_SESSION_COPS_MAX_ATTEMPTS} attempts "
          f"— caller will insert dummy values for affected RAT section.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# SIM Card Detection
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
        # Any other failure — default to False to prevent COPS commands being
        # sent without a confirmed SIM, which would trigger the indefinite retry.
        print(f"{_ts()} [SIM] detect_sim() failed: {e} — assuming no SIM to prevent COPS hang.")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# Instantaneous KPI Collection
# ══════════════════════════════════════════════════════════════════════════════

def instKPIcollection(nr5g_bands, lte_bands, sim_present: bool = True):
    """
    Perform one full KPI collection pass across all configured bands and
    return the results packaged as a SamplingSession.

    Called 5 times by the outer loop in full_script.py to build the list of
    5 SamplingSession objects that process_window() needs before averaging.

    ── Collection Modes ──────────────────────────────────────────────────────
    Mode A — NR5G + LTE  (sim_present=True, nr5g_bands is not empty)
        AT+COPS=0  →  NR5G band loop  →  AT+COPS=2  →  LTE band loop
        →  AT+COPS=0 post-session reset for next session

    Mode B — LTE Only, SIM present  (sim_present=True, nr5g_bands is empty)
        AT+COPS=2  →  LTE band loop  →  done (no post-session reset needed)

    Mode C — LTE Only, No SIM  (sim_present=False)  ← ONLY MODE TESTED
        LTE band loop only — no COPS commands of any kind.
        Without a SIM the modem stays in LIMSRV state (limited service —
        camped on a cell but not registered). The modem returns real RF
        measurements (RSRP, RSRQ, RSSI, SINR) in LIMSRV state without
        needing registration. AT+COPS=2 and AT+COPS=0 both return ERROR
        without a SIM and must never be called. NR5G is skipped entirely
        regardless of configuration — NR5G NSA requires an LTE anchor
        registration and NR5G SA requires full 5G registration; neither is
        possible without a SIM.

    ── Dummy KPI Sentinel Values ─────────────────────────────────────────────
    When a band cannot be collected (SEARCH state, AT command failure, COPS
    failure), a dummy KPI object is inserted with all numeric fields set to
    9999. This preserves the positional structure that process_window() relies
    on — skipping a band entirely would corrupt the per-band averaging logic.
    The 9999 sentinel is detected by INVALID_SENTINEL checks in alarms.py.

    ── Failure Counters ──────────────────────────────────────────────────────
    Two counters are returned alongside the SamplingSession:
        command_failure_count: Bands lost to AT command failures (modem logic errors).
        serial_failure_count:  Bands lost to SerialException (USB-level disconnect).
    full_script.py evaluates these after each session to decide whether to send
    a modem-down alarm or trigger a USB reset.

    ── In-Session COPS Behavior ──────────────────────────────────────────────
    AT+COPS commands within this function use send_cops_command_in_session()
    (hard attempt ceiling, returns True/False) rather than the indefinite
    send_cops_command_until_success() used at startup. This prevents a single
    failing COPS command from stalling an entire collection session.
    AT+COPS=2 is sent exactly once per session in Modes A and B — never
    inside any band loop.

    Args:
        nr5g_bands:  List of NR5G band label strings, e.g. ['n2', 'n66'].
                     Pass an empty list [] for LTE-only modes.
                     Ignored entirely in Mode C regardless of contents.
        lte_bands:   List of LTE band label strings, e.g. ['b2', 'b5', 'b12'].
        sim_present: True if a SIM card is detected (from detect_sim()).
                     False forces Mode C — COPS commands are skipped entirely.
                     Default True to preserve backward compatibility.

    Returns:
        Tuple of (SamplingSession, command_failure_count, serial_failure_count):
            SamplingSession:       One reading per band (NR5G first if Mode A,
                                   then LTE). Always structurally complete.
            command_failure_count: int — bands lost to AT command/modem failures.
            serial_failure_count:  int — bands lost to serial port failures.
    """

    session_start = datetime.now()
    readings      = []
    
         # Tracks bands that failed due to AT command or modem logic errors.
    # Does NOT include bands that returned SEARCH — no cell found is normal,
    # not a failure, and does not increment this counter.
    command_failure_count = 0

    # Tracks bands that failed because SerialException was raised, meaning
    # the USB serial port itself was unreachable at the hardware level.
    # Kept separate from command_failure_count so full_script.py can distinguish
    # a USB disconnect (serial_failure_count > 0) from a modem logic failure
    # (command_failure_count > 0) and send the appropriate alarm type.
    serial_failure_count = 0

    # Set to True when AT+COPS=2 fails in Mode A or Mode B. Prevents the shared
    # LTE loop from running since dummy LTE values are already inserted by the
    # failing mode block and running the loop would overwrite them or operate in
    # an unknown registration state.
    skip_lte_loop = False

    # ── Mode Detection ────────────────────────────────────────────────────────
    # Mode A requires BOTH NR5G bands configured AND a SIM present.
    # If no SIM, mode_c takes priority regardless of nr5g_bands content.
    mode_a = bool(nr5g_bands) and sim_present  # True → NR5G + LTE collection
    mode_c = not sim_present                   # True → LTE only, no COPS commands

    if mode_a:
        print(f"\n{_ts()} [SESSION] Mode A (NR5G + LTE)  — starting collection at {session_start}")
    elif mode_c:
        print(f"\n{_ts()} [SESSION] Mode C (LTE Only, No SIM) — starting collection at {session_start}")
    else:
        print(f"\n{_ts()} [SESSION] Mode B (LTE Only)    — starting collection at {session_start}")

    # ══════════════════════════════════════════════════════════════════════════
    # Mode A — NR5G + LTE
    # NOTE: This mode has not been tested against hardware.
    # ══════════════════════════════════════════════════════════════════════════
    if mode_a:

        # ── Step 1: Verify or establish auto-registration for NR5G ───────────────
        # ── Step 1: Verify or establish auto-registration for NR5G ────────────
        # Check the current COPS mode before sending AT+COPS=0. If the modem
        # was left in auto mode by the previous session's post-session reset,
        # the command is redundant and skipping it saves time.
        # If check_cops_mode() returns None (mode unknown), we send AT+COPS=0
        # as a precaution — it's safer to send a redundant command than to
        # start the NR5G loop in an unknown registration state.
        print(f"{_ts()} [SESSION][A] Checking modem COPS mode before NR5G loop...")
        current_mode = check_cops_mode()

        if current_mode == 0:
            # Already in auto-registration mode — no command needed.
            print(f"{_ts()} [SESSION][A] Modem already in COPS=0 (auto) — skipping redundant command.")
            cops0_ok = True
        else:
            # Not in auto mode or mode unknown — attempt to set it.
            if current_mode is not None:
                print(f"{_ts()} [SESSION][A] Modem in COPS={current_mode} — sending AT+COPS=0...")
            else:
                print(f"{_ts()} [SESSION][A] COPS mode unknown — sending AT+COPS=0 as precaution...")
            cops0_ok = send_cops_command_in_session('AT+COPS=0', timeout=180)

        # ── NR5G section — gated on COPS=0 success ────────────────────────────────
        if cops0_ok:
            print(f"{_ts()} [SESSION][A] AT+COPS=0 confirmed — proceeding with NR5G band loop.")

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
                        print(f"{_ts()} [NR5G] Band {band}: modem in CFUN={cfun_value} — "
                            f"restoring full functionality...")
                        send_at_command_with_retry("AT+CFUN=1", 15)
                        time.sleep(3)
                    else:
                        print(f"{_ts()} [NR5G] Band {band}: modem already in full functionality — "
                            f"pre-flight skipped.")

                except Exception as preflight_e:
                    print(f"{_ts()} [NR5G] Band {band}: pre-flight CFUN check failed: {preflight_e} "
                        f"— attempting AT+CFUN=1 as precaution.")
                    try:
                        send_at_command_with_retry("AT+CFUN=1", 15)
                        time.sleep(3)
                    except Exception as restore_e:
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

                    kpi = None
                    for attempt in range(3):
                        raw_response = send_at_command_with_retry(AT_CMD_SERVING_CELL, 0.3)
                        kpi          = parse_serving_cell(raw_response, band)
                        if kpi is not None:
                            break
                        print(f"{_ts()} [NR5G] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                        time.sleep(1)

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
                    print(f"{_ts()} [NR5G] Band {band}: serial port failure — {e} — storing dummy KPI, continuing.")
                    serial_failure_count += 1
                    readings.append(dummy_kpi)
                    continue

                except Exception as e:
                    print(f"{_ts()} [NR5G] Band {band}: AT command failure — {e} — storing dummy KPI, continuing.")
                    send_runtime_alarm(
                        f"NR5G band {band}",
                        f"Band configuration failed after retries: {e}. Dummy KPI stored."
                    )
                    command_failure_count += 1
                    readings.append(dummy_kpi)
                    continue

        else:
            # COPS=0 failed — insert dummy KPI for every NR5G band so the session
            # remains structurally complete. process_window expects exactly one reading
            # per configured band in order — skipping bands entirely would corrupt
            # the positional matching used during window averaging.
            # command_failure_count is incremented by the full NR5G band count so
            # full_script.py's consecutive failure detector correctly accounts for
            # this entire section being lost to a modem mode failure.
            print(f"{_ts()} [SESSION][A] AT+COPS=0 failed — inserting dummy KPI for all "
                f"NR5G bands: {nr5g_bands}")
            for band in nr5g_bands:
                readings.append(NR5GKPI(
                    timestamp = datetime.now(),
                    rat       = "NR5G",
                    band      = int(band[1:]),
                    pci       = 9999,
                    arfcn     = 9999,
                    ss_rsrp   = 9999,
                    ss_rsrq   = 9999,
                    ss_sinr   = 9999,
                ))
            command_failure_count += len(nr5g_bands)
            send_runtime_alarm(
                "AT+COPS=0 in-session",
                f"Modem mode change to auto-registration failed after "
                f"{_IN_SESSION_COPS_MAX_ATTEMPTS} attempts during NR5G collection. "
                f"All NR5G bands ({', '.join(nr5g_bands)}) set to dummy values this session."
            )

        # ── Step 2: Detach for LTE ────────────────────────────────────────────
        # Attempted independently of the NR5G outcome — even if COPS=0 failed
        # above, COPS=2 is still tried. A modem that couldn't enter auto mode
        # may still be able to detach, allowing LTE collection to proceed.
        print(f"{_ts()} [SESSION][A] Sending AT+COPS=2 — detaching for LTE scanning...")
        cops2_ok = send_cops_command_in_session('AT+COPS=2', timeout=180)

        if cops2_ok:
            print(f"{_ts()} [SESSION][A] AT+COPS=2 accepted — waiting 10 seconds before LTE loop...")
            time.sleep(10)
        else:
            # COPS=2 failed — LTE collection cannot proceed safely. The modem's
            # registration state is unknown and may interfere with band-specific
            # LTE scanning. Insert dummies for all LTE bands and skip the LTE loop.
            print(f"{_ts()} [SESSION][A] AT+COPS=2 failed — inserting dummy KPI for all "
                f"LTE bands: {lte_bands}")
            skip_lte_loop = True
            for band in lte_bands:
                readings.append(LTEKPI(
                    timestamp = datetime.now(),
                    rat       = "LTE",
                    band      = int(band[1:]),
                    pci       = 9999,
                    earfcn    = 9999,
                    rsrp      = 9999,
                    rsrq      = 9999,
                    rssi      = 9999,
                    sinr      = 9999,
                ))
            command_failure_count += len(lte_bands)
            send_runtime_alarm(
                "AT+COPS=2 in-session",
                f"Modem detach command failed after {_IN_SESSION_COPS_MAX_ATTEMPTS} "
                f"attempts during Mode A LTE preparation. "
                f"All LTE bands ({', '.join(lte_bands)}) set to dummy values this session."
            )

    # ══════════════════════════════════════════════════════════════════════════
    # Mode C — LTE Only, No SIM  ← ONLY MODE TESTED AND VALIDATED
    # ══════════════════════════════════════════════════════════════════════════
    # Without a SIM the modem operates in LIMSRV state — camped on LTE cells
    # for emergency service but not network-registered. In LIMSRV state the
    # modem returns real RF measurements (RSRP, RSRQ, RSSI, SINR) via QENG,
    # which is sufficient for passive DAS signal monitoring.
    #
    # AT+COPS=2 and AT+COPS=0 MUST NOT be sent without a SIM — both return
    # ERROR immediately and would trigger send_cops_command_until_success()'s
    # indefinite retry loop, stalling the session permanently.
    #
    # NR5G is skipped entirely in Mode C regardless of nr5g_bands configuration.
    # NR5G NSA requires an LTE anchor registration; NR5G SA requires full 5G
    # registration. Neither is achievable without a SIM.
    #
    # The 10-second post-COPS=2 sleep used in Modes A and B is also skipped —
    # that sleep exists to let the network detach settle, which does not apply
    # here since no detach is issued.
    elif mode_c:
        print(f"[SESSION][C] No SIM detected — skipping AT+COPS=2. "
              f"LTE bands will scan in LIMSRV state and return real RF measurements.")

    # ══════════════════════════════════════════════════════════════════════════
    # Mode B — LTE Only
    # NOTE: This mode has not been tested against hardware.
    # ══════════════════════════════════════════════════════════════════════════
    else:

        # ── Step 1: Detach before LTE scanning ───────────────────────────────
        # In LTE-only mode there is no NR5G loop. COPS=2 runs once here
        # before the LTE loop. If it fails, all LTE bands receive dummy values
        # and the shared LTE loop is skipped via skip_lte_loop.
        print(f"{_ts()} [SESSION][B] Sending AT+COPS=2 — detaching for LTE-only band scanning...")
        cops2_ok = send_cops_command_in_session('AT+COPS=2', timeout=180)

        if cops2_ok:
            print(f"{_ts()} [SESSION][B] AT+COPS=2 accepted — waiting 10 seconds before LTE loop...")
            time.sleep(10)
        else:
            print(f"{_ts()} [SESSION][B] AT+COPS=2 failed — inserting dummy KPI for all "
                  f"LTE bands: {lte_bands}")
            skip_lte_loop = True
            for band in lte_bands:
                readings.append(LTEKPI(
                    timestamp = datetime.now(),
                    rat       = "LTE",
                    band      = int(band[1:]),
                    pci       = 9999,
                    earfcn    = 9999,
                    rsrp      = 9999,
                    rsrq      = 9999,
                    rssi      = 9999,
                    sinr      = 9999,
                ))
            command_failure_count += len(lte_bands)
            send_runtime_alarm(
                "AT+COPS=2 in-session",
                f"Modem detach command failed after {_IN_SESSION_COPS_MAX_ATTEMPTS} "
                f"attempts during Mode B LTE preparation. "
                f"All LTE bands ({', '.join(lte_bands)}) set to dummy values this session."
            )

     # ══════════════════════════════════════════════════════════════════════════
    # LTE Band Loop — shared by Mode A, Mode B, and Mode C
    # In Modes A and B, AT+COPS=2 has already been sent exactly once above
    # before reaching here. In Mode C, no COPS command is sent — the modem
    # is already in LIMSRV state and the loop runs directly.
    # skip_lte_loop is True when AT+COPS=2 failed in Mode A or Mode B —
    # dummy LTE values were already inserted in the failing mode block so
    # the session remains structurally complete. The loop is skipped entirely
    # to avoid overwriting those dummies or running band commands in an
    # unknown modem registration state.
    # ══════════════════════════════════════════════════════════════════════════
    if not skip_lte_loop:
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
    # Attempts to return the modem to auto-registration mode so the next
    # session's opening state check finds COPS=0 and can skip the command,
    # saving time at the start of the next NR5G collection pass.
    # Unlike startup COPS commands, failure here does not insert dummies —
    # this session's data has already been collected. The next session's
    # opening check_cops_mode() call will detect the wrong mode and send
    # AT+COPS=0 at that point via send_cops_command_in_session.
    # Mode B skips this entirely — there is no NR5G to prepare for.
    if mode_a:
        print(f"{_ts()} [SESSION][A] Post-session reset — sending AT+COPS=0...")
        reset_ok = send_cops_command_in_session('AT+COPS=0', timeout=180)
        if reset_ok:
            print(f"{_ts()} [SESSION][A] AT+COPS=0 reset accepted — waiting 10 seconds...")
            time.sleep(10)
        else:
            # Non-fatal — next session's opening state check handles recovery.
            # No dummy insertion needed — this session's data is already complete.
            print(f"{_ts()} [SESSION][A] Post-session AT+COPS=0 reset failed — "
                  f"next session will detect and correct mode via check_cops_mode().")
            send_runtime_alarm(
                "AT+COPS=0 post-session reset",
                f"Post-session COPS reset failed after {_IN_SESSION_COPS_MAX_ATTEMPTS} attempts. "
                f"Next session will attempt mode correction at startup. No data loss this session."
            )

    # ── Package and return ────────────────────────────────────────────────────
    # Wrap the completed readings list into a SamplingSession so the
    # outer loop can accumulate 5 sessions before passing to process_window.
    return SamplingSession(
        session_start = session_start,
        readings      = readings,
    ), command_failure_count, serial_failure_count
