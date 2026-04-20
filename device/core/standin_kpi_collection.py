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
from modem import at_command_comms
import time



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
        clean = qeng_line.replace('+QENG:', '').replace('"', '').strip()
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
            raw_sinr = parts[16]
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
        response = at_command_comms(command, timeout)

        if response != "ERROR":
            return response

        print(f"[RETRY] Attempt {attempt + 1}/{max_retries} failed for: {command}")
        time.sleep(0.3)

    raise Exception(f"[MODEM ALERT] Command failed after {max_retries} attempts: {command}")



# ══════════════════════════════════════════════════════════════════════════════
# Segment 3C — Instantaneous KPI Collection
# ══════════════════════════════════════════════════════════════════════════════

def instKPIcollection(nr5g_bands, lte_bands):
    """
    Performs one full KPI collection pass across all configured bands
    and returns the results packaged as a SamplingSession.

    This function is called 5 times by the outer loop to build the
    list of 5 SamplingSession objects the averaging script expects.

    Args:
        nr5g_bands: List of NR5G band strings e.g. ['n2', 'n66']
        lte_bands:  List of LTE band strings  e.g. ['b2', 'b5', 'b12']

    Returns:
        A SamplingSession containing one reading per band,
        NR5G readings first, LTE readings second.
    """

    # Record the moment this session starts.
    # This timestamps the SamplingSession so the averaging script
    # knows when each of the 5 sessions occurred.
    session_start = datetime.now()
    print(f"\n[SESSION] Starting collection at {session_start}")

    # This list collects one KPI reading per band in order.
    # It is critical that every band always appends something —
    # either a valid KPI object or None — so that the index positions
    # stay consistent across all 5 sessions for the averaging script.
    readings = []

    # Auto-register so the modem can reach NR5G cells.
    # This must happen before the NR5G loop begins.
    print("[SESSION] Sending AT+COPS=0 — enabling auto-registration for NR5G...")
    send_at_command_with_retry('AT+COPS=0', 180)


    # ── NR5G Band Loop ────────────────────────────────────────────────────────────
# Loops through each NR5G band, configures the modem, queries the serving
# cell, and appends the result to the readings list.
# AT+COPS=0 was already sent in the setup so the modem can reach NR5G cells.

    for band in nr5g_bands:

    # Extract the numeric part of the band string (e.g. 'n2' → '2')
        band_num = band[1:]

    # Initialize a dummy NR5GKPI immediately with sentinel values (9999).
    # This is appended if band configuration fails or no cell is found.
    # 9999 is above INVALID_SENTINEL in alarms.py so it will correctly
    # trigger an invalid alarm in the averaging script.
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

        try:
            # Configure the modem to search on this specific NR5G band.
            # AT_CMD_5G_BAND_CONFIG ends with a comma so band_num is
            # concatenated directly onto the end of the string.
            print(f"[NR5G] Configuring band {band}...")
            send_at_command_with_retry(AT_CMD_5G_BAND_CONFIG + band_num, 0.3)
            print(at_command_comms("AT+CFUN=0", 15))
            time.sleep(3)
            print(at_command_comms("AT+CFUN=1", 15))
            time.sleep(3)

            # Wait 2 seconds for the modem to complete its cell search
            # on the newly configured band before querying serving cell info.
            

            # ── Serving Cell Query with Retry ─────────────────────────────────
            # SEARCH is a transient state — the modem may still be scanning.
            # We try up to 3 times with a 1 second wait between each attempt.
            kpi = None
            for attempt in range(3):
                raw_response = send_at_command_with_retry(AT_CMD_SERVING_CELL, 0.3)
                kpi = parse_serving_cell(raw_response, band)

                if kpi is not None:
                    # Valid reading received — no need to retry
                    break

                print(f"[NR5G] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                time.sleep(1)

            # RAT and band verification — confirms the returned KPI actually
            # belongs to the band we configured. If the modem fell back to a
            # different RAT or a different band, we treat it as no valid reading.
            # This check is the same pattern used in the LTE loop below.
            if kpi is not None and (not isinstance(kpi, NR5GKPI) or kpi.band != int(band_num)):
                print(f"[NR5G] Band {band}: returned wrong RAT or band (got RAT={kpi.rat}, band={kpi.band}) — storing dummy.")
                kpi = None

            # If all 3 attempts returned None, the band is genuinely
            # unavailable — append the dummy KPI initialized at the top
            if kpi is None:
                print(f"[NR5G] Band {band}: no cell found after 3 attempts — storing dummy KPI.")
                readings.append(dummy_kpi)
            else:
                print(f"[NR5G] Band {band}: collected — SS-RSRP={kpi.ss_rsrp}, SS-RSRQ={kpi.ss_rsrq}, SS-SINR={kpi.ss_sinr}")
                readings.append(kpi)

        except Exception as e:
            # Band configuration failed after all retries.
            # Log the failure, append the dummy KPI to preserve index
            # alignment across all 5 sessions, and move to the next band.
            print(f"[NR5G] Band {band}: configuration failed — {e}")
            readings.append(dummy_kpi)
            continue

    # ── LTE Band Loop ─────────────────────────────────────────────────────────────
    # Detach from network before LTE loop so the modem can be directed
    # to specific LTE bands without NR5G interference.
    print("[SESSION] Sending AT+COPS=2 — detaching for LTE band scanning...")
    send_at_command_with_retry('AT+COPS=2', 180)
    print("Waiting 10 seconds after mode switch")
    time.sleep(10)

    for band in lte_bands:

        # Extract the numeric part of the band string (e.g. 'b2' → '2')
        band_num = band[1:]

        # Initialize a dummy LTEKPI with sentinel values (9999).
        # This is appended if band configuration fails or no cell is found.
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

        try:
            # Configure the modem to search on this specific LTE band.
            print(f"[LTE] Configuring band {band}...")
            send_at_command_with_retry(AT_CMD_LTE_BAND_CONFIG + band_num, 0.3)

            # Wait 2 seconds for the modem to complete its cell search
            # on the newly configured band before querying serving cell info.
            print(at_command_comms("AT+CFUN=0", 15))
            time.sleep(3)
            print(at_command_comms("AT+CFUN=1", 15))
            time.sleep(3)

            # ── Serving Cell Query with Retry ─────────────────────────────────
            # SEARCH is a transient state — the modem may still be scanning.
            # We try up to 3 times with a 1 second wait between each attempt.
            kpi = None
            for attempt in range(3):
                raw_response = send_at_command_with_retry(AT_CMD_SERVING_CELL, 0.3)
                kpi = parse_serving_cell(raw_response, band)

                if kpi is not None:
                    # Valid reading received — no need to retry
                    break

                print(f"[LTE] Band {band}: SEARCH on attempt {attempt + 1}/3 — waiting 1s...")
                time.sleep(1)

            # Band verification — confirms the returned KPI actually belongs
            # to the band we configured. If the modem returned a different band,
            # we treat it as no valid reading.
            if kpi is not None and kpi.band != int(band_num):
                print(f"[LTE] Band {band}: returned wrong band (got band={kpi.band}) — storing dummy.")
                kpi = None

            # If all 3 attempts returned None, the band is genuinely
            # unavailable — append the dummy KPI initialized at the top.
            if kpi is None:
                print(f"[LTE] Band {band}: no cell found after 3 attempts — storing dummy KPI.")
                readings.append(dummy_kpi)
            else:
                print(f"[LTE] Band {band}: collected — RSRP={kpi.rsrp}, RSRQ={kpi.rsrq}, SINR={kpi.sinr}")
                readings.append(kpi)

        except Exception as e:
            # Band configuration failed after all retries.
            # Log the failure, append the dummy KPI to preserve index
            # alignment across all 5 sessions, and move to the next band.
            print(f"[LTE] Band {band}: configuration failed — {e}")
            readings.append(dummy_kpi)
            continue

    # ── Post-LTE Reset ────────────────────────────────────────────────────────
    # Reset modem to auto-registration for NR5G on the next session.
    # Skipped entirely if no NR5G bands are configured — avoids an
    # unnecessary AT+COPS=0 and 10 second cooldown for LTE-only setups.
    if nr5g_bands:
        print("[SESSION] Sending AT+COPS=0 — resetting modem for next session NR5G...")
        send_at_command_with_retry('AT+COPS=0', 180)
        print("[SESSION] Waiting 10 seconds after mode switch...")
        time.sleep(10)

    # ── Package and return ────────────────────────────────────────────────────
    # Wrap the completed readings list into a SamplingSession so the
    # outer loop can accumulate 5 sessions before passing to process_window.
    return SamplingSession(
        session_start = session_start,
        readings      = readings,
    )