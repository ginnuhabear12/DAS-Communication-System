"""
alarms.py — KPI Window Alarm Processing for DAS Communication System
---------------------------------------------------------------------
Processes a window of 5 SamplingSession instances to evaluate KPI health.

Steps:
    1. Loop through each band index across all 5 sessions
    2. Check for invalid KPI values (last 3 > INVALID_SENTINEL) → SNMP invalid alarm
    3. Average remaining valid values → check against thresholds → SNMP threshold alarm
    4. Populate AveragedKPI objects with results
"""


from models import KPIReading, LTEKPI, NR5GKPI, AveragedLTEKPI, AveragedNR5GKPI, SamplingSession
from snmpSend import send_invalid_kpi_alarm, send_threshold_alarm


from models import KPIReading, LTEKPI, NR5GKPI, AveragedLTEKPI, AveragedNR5GKPI, SamplingSession

# ── Thresholds ────────────────────────────────────────────────────────────────
# KPI values below these limits trigger a threshold alarm.
# Adjust per deployment requirements.

INVALID_SENTINEL = 500  # Values above this are considered invalid


# ── Helper: Check and process one KPI across 5 readings ───────────────────────
def check_kpi(kpi_name: str, values: list, threshold: float, band: int) -> float | None:
    """
    Evaluate one KPI across a 5-sample window.

    Checks if the last 3 values are all invalid (> INVALID_SENTINEL).
    If so, sends an SNMP invalid alarm and returns None.
    If not, averages the valid values, checks against threshold,
    sends an SNMP threshold alarm if needed, and returns the average.

    Args:
        kpi_name:  Name of the KPI being checked (e.g. "rsrp", "ss_sinr").
        values:    List of 5 float values in chronological order.
        threshold: The minimum acceptable value for this KPI.
        band:      Band number, used in alarm messages.

    Returns:
        The averaged float value, or None if the KPI was invalid.
    """
    last_3 = values[2], values[3], values[4]

    # ── Step 1: Invalid check ─────────────────────────────────────────────────
    if all(v > INVALID_SENTINEL for v in last_3):
        invalid_count = sum(1 for v in values if v > INVALID_SENTINEL)
        print(f"[INVALID]   Band {band} | {kpi_name.upper()}: "
            f"{invalid_count} of {len(values)} samples invalid (last 3 consecutive)")
        # FIX (Issue 3): The original code was missing 'return None' here.
        # Without it, execution fell through to the averaging step below
        # regardless of whether this invalid check fired. If all 5 values
        # were invalid (9999), valid_values would be an empty list and
        # dividing by len([]) = 0 would cause a ZeroDivisionError that
        # crashed process_window entirely, losing all remaining band results.
        # Returning None here is correct and intentional — the caller
        # (process_window) stores None directly into the averaged KPI object,
        # which file_manager.py serializes as JSON null. This is the expected
        # behavior for invalid KPI data and requires no extra handling at the
        # call site.
        #send_invalid_kpi_alarm(band=band, kpi=kpi_name.upper(), invalid_count=invalid_count)
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
        print(f"[INVALID]   Band {band} | {kpi_name.upper()}: "
              f"no valid samples to average — returning None.")
        return None

    avg = sum(valid_values) / len(valid_values)

    # ── Step 3: Threshold check ───────────────────────────────────────────────
    if avg < threshold:
        print(
            f"[THRESHOLD] Band {band} | {kpi_name.upper()}: "
            f"avg = {avg:.1f}, below threshold ({threshold:.1f})"
        )
        #send_threshold_alarm(band=band, kpi=kpi_name.upper(), avg_value=avg, threshold=threshold)

    return avg


def process_window(sessions: list[SamplingSession], LTE_THRESHOLDS: dict, NR5G_THRESHOLDS: dict) -> list:
    """
    Process a 5-session KPI window and trigger alarms where necessary.

    Args:
        sessions: A list of exactly 5 SamplingSession instances, each
                  containing readings for all configured bands in order.
    """

    # ── Print all input values ────────────────────────────────────────────────
    print("── Input Sessions ──────────────────────────────────────")
    for i, session in enumerate(sessions):
        print(f"  Session {i + 1} | Start: {session.session_start}")
        for reading in session.readings:
            if isinstance(reading, LTEKPI):
                print(
                    f"    Band: {reading.band} | RAT: {reading.rat} | "
                    f"Timestamp: {reading.timestamp} | "
                    f"RSRP: {reading.rsrp} | RSRQ: {reading.rsrq} | "
                    f"RSSI: {reading.rssi} | SINR: {reading.sinr}"
                )
            elif isinstance(reading, NR5GKPI):
                print(
                    f"    Band: {reading.band} | RAT: {reading.rat} | "
                    f"Timestamp: {reading.timestamp} | "
                    f"SS-RSRP: {reading.ss_rsrp} | SS-RSRQ: {reading.ss_rsrq} | "
                    f"SS-SINR: {reading.ss_sinr}"
                )
    print("────────────────────────────────────────────────────────")
    print()

    # Number of bands is derived from the first session —
    # all sessions are confirmed to have the same band order and count
    num_bands = len(sessions[0].readings)
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

        # ── LTE ──────────────────────────────────────────────────────────────
        if isinstance(first, LTEKPI):

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

        # ── NR5G ─────────────────────────────────────────────────────────────
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

        # averaged is now fully populated for this band —
        # ready for storage when that logic is implemented
        
        # Append completed averaged object — prevents overwrite on next band iteration
        averaged_results.append(averaged)

    # Return averaged results to the main script for file writing
    return averaged_results