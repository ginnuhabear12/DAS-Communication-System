"""
Module: alarms.py
Purpose: KPI window alarm processing for the DAS Communication System.
         Receives a window of 5 SamplingSession instances from full_script.py,
         evaluates each band's KPI health, triggers SNMP alarms where needed,
         and returns a list of AveragedKPI objects for storage by file_manager.py.

Processing steps per band:
    1. Collect the 5 readings for this band across all sessions (one per session)
    2. Check for invalid KPI values — if the last 3 of 5 samples exceed
       INVALID_SENTINEL, send an SNMP invalid alarm and store None for that KPI
    3. Average the remaining valid samples and compare against thresholds —
       if the average falls below the threshold, send an SNMP threshold alarm
    4. Populate AveragedLTEKPI or AveragedNR5GKPI objects with the results
       and return them to full_script.py for storage

NOTE: Only the LTE path (Mode C, no SIM) has been tested against hardware.
      The NR5G path in process_window() is implemented but untested.
"""

from datetime import datetime
from models import KPIReading, LTEKPI, NR5GKPI, AveragedLTEKPI, AveragedNR5GKPI, SamplingSession
from snmpSend import send_invalid_kpi_alarm, send_threshold_alarm

# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Sentinel threshold — KPI values above this are treated as invalid readings.
# 9999 is the sentinel value inserted by instKPIcollection() when a band
# returns no cell (SEARCH state) or a command fails. 500 is used here rather
# than 9999 so that any value clearly outside the physical measurement range
# is also caught, not just exact sentinel matches.
INVALID_SENTINEL = 500  # Values above this are considered invalid


# ══════════════════════════════════════════════════════════════════════════════
# KPI Evaluation Helper
# ══════════════════════════════════════════════════════════════════════════════
def check_kpi(kpi_name: str, values: list, threshold: float, band: int) -> float | None:
    """
    Evaluate one KPI metric across a 5-sample window.

    Called once per KPI per band by process_window(). Returns the averaged
    value if the data is valid and within threshold, or None if the KPI was
    determined to be invalid (too many sentinel values).

    Invalid check logic:
        If the last 3 of the 5 chronological samples all exceed INVALID_SENTINEL,
        the KPI is declared invalid for this window. The last-3 rule (rather than
        all-5) is intentional — a few stale readings at the start of a window
        should not mask a genuinely degraded signal at the end of the window.

    Averaging logic:
        Only samples at or below INVALID_SENTINEL contribute to the average.
        This allows partial windows (e.g. 3 valid out of 5) to still produce
        a meaningful average as long as the last-3 invalid check did not fire.

    Threshold check logic:
        If the computed average falls below the threshold, an SNMP threshold
        alarm is sent. A passing average produces no alarm — normal operation
        is silent.

    Return value semantics:
        None → KPI was invalid. process_window() stores None directly in the
               AveragedKPI object, which file_manager.py serializes as JSON null.
               This is the intended representation for missing/invalid data.
        float → Valid average. Stored in the AveragedKPI object for reporting.

    Args:
        kpi_name:  Name of the KPI being evaluated (e.g. "rsrp", "ss_sinr").
                   Used in log messages and SNMP alarm payloads.
        values:    List of exactly 5 float values in chronological order,
                   one per session. Sentinel values (9999) are included as-is —
                   this function filters them internally.
        threshold: Minimum acceptable averaged value for this KPI.
                   Passed in from LTE_THRESHOLDS or NR5G_THRESHOLDS in process_window().
        band:      Band number (integer). Used in log messages and alarm payloads.

    Returns:
        float: The averaged value across valid samples.
        None:  If the last 3 samples were all invalid, or if no valid samples exist.
    """

    # The last 3 values are the most recent — if all three are invalid,
    # the signal has been absent or broken for the tail end of the window,
    # which is treated as a sustained failure regardless of earlier samples.
    last_3 = values[2], values[3], values[4]

    # ── Step 1: Invalid check ─────────────────────────────────────────────────
    if all(v > INVALID_SENTINEL for v in last_3):
        invalid_count = sum(1 for v in values if v > INVALID_SENTINEL)
        print(f"{_ts()} [INVALID]   Band {band} | {kpi_name.upper()}: "
            f"{invalid_count} of {len(values)} samples invalid (last 3 consecutive)")

        # Return None immediately — do not fall through to averaging.
        # If all 5 values are sentinel (9999), valid_values below would be
        # an empty list, causing a ZeroDivisionError when computing the average.
        # Even with fewer than 5 invalid values, falling through here when the
        # last-3 check fired would produce a misleading average from stale data.
        # None is stored directly in the AveragedKPI field → JSON null in storage.
        send_invalid_kpi_alarm(band=band, kpi=kpi_name.upper(), invalid_count=invalid_count)
        return None

    # ── Step 2: Average valid samples ─────────────────────────────────────────
    valid_values = [v for v in values if v <= INVALID_SENTINEL]

    # Secondary ZeroDivisionError guard — handles the edge case where
    # valid_values is somehow empty even though the last-3 check above did
    # not fire (e.g. only the first 2 samples are valid but not the last 3).
    # Without this, len([]) = 0 would still cause a ZeroDivisionError.
    # Returns None for the same reason as above — the averaged object stores
    # None which becomes JSON null, the correct representation for no valid data.
    if not valid_values:
        print(f"{_ts()} [INVALID]   Band {band} | {kpi_name.upper()}: "
              f"no valid samples to average — returning None.")
        return None

    avg = sum(valid_values) / len(valid_values)

    # ── Step 3: Threshold check ───────────────────────────────────────────────
    # Compare the computed average against the deployment threshold.
    # Only fires if the average is strictly below the threshold — at or above
    # is normal operation and produces no alarm output.
    if avg < threshold:
        print(
            f"{_ts()} [THRESHOLD] Band {band} | {kpi_name.upper()}: "
            f"avg = {avg:.1f}, below threshold ({threshold:.1f})"
        )
        send_threshold_alarm(band=band, kpi=kpi_name.upper(), avg_value=avg, threshold=threshold)

    return avg

# ══════════════════════════════════════════════════════════════════════════════
# Window Processing
# ══════════════════════════════════════════════════════════════════════════════
def process_window(sessions: list[SamplingSession], LTE_THRESHOLDS: dict, NR5G_THRESHOLDS: dict) -> list:
    """
    Process a completed 5-session KPI window and return averaged results.

    Called by full_script.py once 5 SamplingSession objects have been
    accumulated. Iterates over every band position, evaluates each KPI
    via check_kpi(), and returns a list of AveragedLTEKPI or AveragedNR5GKPI
    objects — one per band — for file_manager.py to store.

    Band ordering assumption:
        All 5 sessions must contain readings in the same band order (NR5G
        first if present, then LTE). This is guaranteed by instKPIcollection(),
        which always appends bands in the same configured order and inserts
        dummy KPI objects to preserve position when a band fails.
        If band order were inconsistent across sessions, band_index would
        silently match the wrong readings across sessions.

    Threshold dicts format:
        LTE_THRESHOLDS  = {"rsrp": float, "rsrq": float, "rssi": float, "sinr": float}
        NR5G_THRESHOLDS = {"ss_rsrp": float, "ss_rsrq": float, "ss_sinr": float}
        Keys must match the field names used in check_kpi() calls below.

    NOTE: Only the LTE branch of this function has been tested against hardware.
          The NR5G branch is implemented but untested — validate before deployment
          with NR5G bands configured.

    Args:
        sessions:        List of exactly 5 SamplingSession instances in
                         chronological order (oldest first).
        LTE_THRESHOLDS:  Dict of minimum acceptable values for LTE KPIs.
        NR5G_THRESHOLDS: Dict of minimum acceptable values for NR5G KPIs.

    Returns:
        list: AveragedLTEKPI or AveragedNR5GKPI objects, one per band,
              in the same order as the readings within each session.
              Returned to full_script.py for passing to file_manager.py.
    """

    # ── Print all input values ────────────────────────────────────────────────
    print(f"{_ts()} ── Input Sessions ───────────────────────────────────────")
    for i, session in enumerate(sessions):
        print(f"{_ts()}   Session {i + 1} | Start: {session.session_start}")
        for reading in session.readings:
            if isinstance(reading, LTEKPI):
                print(
                    f"{_ts()}     Band: {reading.band} | RAT: {reading.rat} | "
                    f"Timestamp: {reading.timestamp} | "
                    f"RSRP: {reading.rsrp} | RSRQ: {reading.rsrq} | "
                    f"RSSI: {reading.rssi} | SINR: {reading.sinr}"
                )
            elif isinstance(reading, NR5GKPI):
                print(
                    f"{_ts()}     Band: {reading.band} | RAT: {reading.rat} | "
                    f"Timestamp: {reading.timestamp} | "
                    f"SS-RSRP: {reading.ss_rsrp} | SS-RSRQ: {reading.ss_rsrq} | "
                    f"SS-SINR: {reading.ss_sinr}"
                )
    print(f"{_ts()} ────────────────────────────────────────────────────")
    print()

    # Derive the number of bands from the first session.
    # All sessions are guaranteed to have the same band count and order
    # by instKPIcollection() — dummy KPIs are inserted to preserve positions
    # even when individual bands fail, so this count is always reliable.
    num_bands = len(sessions[0].readings)

    # Accumulates one averaged result per band before returning.
    # Built incrementally rather than assigned by index to avoid needing
    # to pre-allocate a fixed-size list.
    averaged_results = []  # Collect each band's averaged result before file operations

    for band_index in range(num_bands):

        # Collect the reading for this band from each of the 5 sessions
        band_readings = []
        for session in sessions:
            band_readings.append(session.readings[band_index])

        # band_readings now holds 5 readings all belonging to the same band,
        # one from each session in chronological order.
        # Grab shared info from the first reading for this band
        first = band_readings[0]
        band  = first.band

        # ── LTE Band Processing ───────────────────────────────────────────────
        if isinstance(first, LTEKPI):

            # Initialize the averaged result object with metadata from the window.
            # start_time and end_time bound the window for storage/reporting.
            # KPI fields (avg_rsrp, etc.) default to None and are filled by
            # check_kpi() below — None is preserved if the KPI was invalid.
            averaged = AveragedLTEKPI(
                start_time = sessions[0].session_start,
                end_time   = sessions[-1].session_start,
                rat        = first.rat,
                band       = band,
                pci        = first.pci,
                earfcn     = first.earfcn,
            )

            # Check each KPI in order — results stored directly in averaged object
            averaged.avg_rsrp = check_kpi(
                "rsrp",
                [r.rsrp for r in band_readings],
                LTE_THRESHOLDS["rsrp"],
                band,
            )
            averaged.avg_rsrq = check_kpi(
                "rsrq",
                [r.rsrq for r in band_readings],
                LTE_THRESHOLDS["rsrq"],
                band,
            )
            averaged.avg_rssi = check_kpi(
                "rssi",
                [r.rssi for r in band_readings],
                LTE_THRESHOLDS["rssi"],
                band,
            )
            averaged.avg_sinr = check_kpi(
                "sinr",
                [r.sinr for r in band_readings],
                LTE_THRESHOLDS["sinr"],
                band,
            )

        # ── NR5G Band Processing ──────────────────────────────────────────────
        # NOTE: This branch is untested — NR5G collection (Mode A) has not been
        # exercised against hardware. Validate before deploying with NR5G bands.
        elif isinstance(first, NR5GKPI):

            averaged = AveragedNR5GKPI(
                start_time = sessions[0].session_start,
                end_time   = sessions[-1].session_start,
                rat        = first.rat,
                band       = band,
                pci        = first.pci,
                arfcn      = first.arfcn,
            )

            averaged.avg_ss_rsrp = check_kpi(
                "ss_rsrp",
                [r.ss_rsrp for r in band_readings],
                NR5G_THRESHOLDS["ss_rsrp"],
                band,
            )
            averaged.avg_ss_rsrq = check_kpi(
                "ss_rsrq",
                [r.ss_rsrq for r in band_readings],
                NR5G_THRESHOLDS["ss_rsrq"],
                band,
            )
            averaged.avg_ss_sinr = check_kpi(
                "ss_sinr",
                [r.ss_sinr for r in band_readings],
                NR5G_THRESHOLDS["ss_sinr"],
                band,
            )

        # Append the fully populated averaged object for this band.
        averaged_results.append(averaged)

    # Return averaged results to the main script for file writing
    return averaged_results
