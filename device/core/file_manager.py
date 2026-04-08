"""
file_manager.py — KPI File Management for DAS Communication System
------------------------------------------------------------------
Handles two writes per 5-minute window:
    1. Overwrite device_data.json  → updates the live GUI
    2. Append to kpi_YYYY-MM-DD.json → builds the daily KPI log on microSD
    3. Cleanup → enforces 7-day rolling retention
"""

import json
import os
import glob
from datetime import date
from models import AveragedLTEKPI, AveragedNR5GKPI

# ── File Paths ────────────────────────────────────────────────────────────────
# Directory where daily KPI files are stored on the microSD
KPI_DIR = "/home/das/DAS-Communication-System/device/core/kpi_data"

# Path to the JSON file that feeds the live GUI
# SUBJECT TO CHANGE — this is a test file, final path to be confirmed with partner
# SUBJECT TO CHANGE — the JSON structure inside this file (bands array, field names)
# may be updated once partner confirms GUI can handle multi-band data
GUI_JSON_PATH = "/home/das/DAS-Communication-System/device/core/device_data_test.json"

# Maximum number of daily KPI files to keep before deleting the oldest
MAX_DAYS = 7

def _averaged_to_dict(avg) -> dict:
    """
    Converts an AveragedLTEKPI or AveragedNR5GKPI object into a dictionary
    that matches the structure of device_data_test.json for GUI display.
    None values (invalid KPIs) are preserved as null in the JSON file.

    SUBJECT TO CHANGE — field names and structure should be confirmed 
    with partner once GUI multi-band support is implemented.
    """
    # Fields common to both LTE and NR5G bands
    base = {
        "rat":  avg.rat,
        "band": avg.band,
        "pci":  avg.pci,
    }

    # LTE-specific fields — includes rssi which NR5G does not report
    if isinstance(avg, AveragedLTEKPI):
        base.update({
            "earfcn":   avg.earfcn,
            "avg_rssi": avg.avg_rssi,
            "avg_rsrp": avg.avg_rsrp,
            "avg_rsrq": avg.avg_rsrq,
            "avg_sinr": avg.avg_sinr,
        })

    # NR5G-specific fields — no rssi as modem does not report it for NR5G
    elif isinstance(avg, AveragedNR5GKPI):
        base.update({
            "arfcn":       avg.arfcn,
            "avg_ss_rsrp": avg.avg_ss_rsrp,
            "avg_ss_rsrq": avg.avg_ss_rsrq,
            "avg_ss_sinr": avg.avg_ss_sinr,
        })

    return base


def update_gui_json(averaged_results: list) -> None:
    """
    Overwrites the bands section of device_data_test.json with the latest
    time-averaged values. All other fields (device_status, modem_status, etc.)
    are preserved exactly as they are so the GUI is not disrupted.

    Args:
        averaged_results: List of AveragedLTEKPI / AveragedNR5GKPI objects
                          collected from one 5-minute window in alarms.py
    
    SUBJECT TO CHANGE — JSON file path and structure to be confirmed
    with partner once GUI multi-band support is implemented.
    """
    # Read the existing file so we can preserve the non-KPI fields
    # that the GUI depends on (device_status, modem_status, etc.)
    try:
        with open(GUI_JSON_PATH, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # If file is missing or corrupted, start fresh from an empty structure
        data = {
            "device_status": "ONLINE",
            "modem_status":  "CONNECTED",
            "vpn_status":    "ACTIVE",
            "snmp_status":   "RUNNING",
            "site_name":     "DAS",
            "alert_message": "No active alarms",
            "bands":         [],
            "logs":          []
        }

    if not averaged_results: # safety check — if we have no results, we shouldn't update the GUI with empty data
        return
    # Update only the fields that change every window
    data["last_update"] = averaged_results[0].end_time.strftime("%Y-%m-%d %H:%M:%S")
    data["bands"]       = [_averaged_to_dict(avg) for avg in averaged_results]

   

    # Write the updated data back to the file
    with open(GUI_JSON_PATH, "w") as f:
        json.dump(data, f, indent=4)

    print(f"[FILE] GUI JSON updated at {data['last_update']}")


def append_to_daily_file(averaged_results: list) -> None:
    """
    Appends the current window's time-averaged KPI values to today's daily file.
    
    This function handles three scenarios automatically:
        1. Normal operation  — today's file exists, just append to it
        2. New calendar day  — today's file doesn't exist yet, create it first
        3. Fresh system boot — no files exist at all, create today's file fresh
    
    After appending, enforces the 7-day retention policy by deleting
    the oldest file if more than MAX_DAYS files are present.

    Args:
        averaged_results: List of AveragedLTEKPI / AveragedNR5GKPI objects
                          collected from one 5-minute window in alarms.py
    """
    # Create the kpi_data directory on the microSD if it doesn't exist yet
    # exist_ok=True means it won't throw an error if it already exists
    os.makedirs(KPI_DIR, exist_ok=True)

    # Build today's filename using ISO date format (e.g. 2026-04-05_kpi.json)
    # ISO format is important — it ensures files sort correctly by date
    # when we need to find and delete the oldest one
    today    = date.today().strftime("%Y%m%d")
    filepath = os.path.join(KPI_DIR, f"{today}_kpi.json")

    # Check if today's file already exists
    # This single check covers both the "new day" and "fresh boot" scenarios —
    # in both cases the file simply doesn't exist yet and needs to be created
    if os.path.exists(filepath):
        # Today's file exists — load it so we can append to it
        with open(filepath, "r") as f:
            daily_data = json.load(f)
    else:
        # Today's file doesn't exist — create a fresh structure for today
        daily_data = {
            "date":    today,
            "entries": []   # each entry will be one 5-minute averaged window
        }
        print(f"[FILE] New daily KPI file created → {filepath}")

    # Build the entry for this 5-minute window
    # start_time is from the first session, end_time from the last session
    # both come from the AveragedKPI base class in models.py
    entry = {
        "start_time": averaged_results[0].start_time.strftime("%H:%M:%S"),
        "end_time":   averaged_results[0].end_time.strftime("%H:%M:%S"),
        # Reuse the same helper used for the GUI JSON so the band structure
        # is always consistent between the two files
        "bands":      [_averaged_to_dict(avg) for avg in averaged_results]
    }

    # Append the new entry to today's list
    daily_data["entries"].append(entry)

    # Write the updated daily file back to the microSD
    with open(filepath, "w") as f:
        json.dump(daily_data, f, indent=4)

    print(f"[FILE] Entry appended to {filepath} "
          f"({len(daily_data['entries'])} entries today)")

    # ── 7-day retention cleanup ───────────────────────────────────────────────
    # Find all daily KPI files and sort them chronologically
    # ISO date filenames (kpi_YYYY-MM-DD.json) sort correctly as plain strings
    files = sorted(glob.glob(os.path.join(KPI_DIR, "*_kpi.json")))

    # If we have more than MAX_DAYS files, delete the oldest one
    # files[0] is always the oldest because of the chronological sort above
    if len(files) > MAX_DAYS:
        os.remove(files[0])
        print(f"[CLEANUP] Removed oldest KPI file: {files[0]}")

if __name__ == "__main__":
    print("script started")
    class Dummy:
        rat = "LTE"
        band = 12
        pci = 100
        earfcn = 5035
        avg_rssi = -70
        avg_rsrp = -95
        avg_rsrq = -10
        avg_sinr = 5
        avg_ss_rsrp = 7
        start_time = end_time = __import__("datetime").datetime.now()

    avg = Dummy()

    update_gui_json([avg])
    append_to_daily_file([avg])