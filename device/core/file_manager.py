"""
Module: file_manager.py
Purpose: KPI file management for the DAS Communication System.
         Handles two writes per 5-minute averaged window and enforces
         rolling log retention:
             1. Overwrite device_data.json  → updates the live GUI dashboard
             2. Append to YYYYMMDD_kpi.json → builds the daily KPI log on microSD
             3. Cleanup                     → enforces 7-day rolling retention

Error Handling Philosophy:
    Every filesystem operation is individually protected with try/except.
    The design separates read failures, write failures, and directory failures
    so each is handled with the appropriate retry and fallback strategy:

    Cache (_gui_cache, _daily_cache):
        Updated from the in-memory data object every cycle before the write
        attempt, unconditionally. This means the cache always holds the most
        current intended state regardless of whether the disk write succeeded.
        If writes fail for multiple cycles, all entries accumulate in the cache
        and are written in full when the filesystem recovers — nothing is lost.

    On read failure:
        OSError       → retry once (may be a transient hardware glitch).
        JSONDecodeError / PermissionError → no retry (retrying reads the
            same corrupt/inaccessible content). Fall back to cache if available,
            or to hardcoded defaults / empty structure on first boot.

    On write failure:
        Retry once, then send an SNMP runtime trap every cycle the failure
        persists so the operator is alerted.

    SNMP trap sends:
        All wrapped in _send_trap() so a trap failure never interrupts
        the main error handling path.

NOTE: This module has been tested with LTE-only (Mode C, no SIM) data.
      The NR5G branch in _averaged_to_dict() is implemented but untested.
"""

import json
import os
import glob
import time
from datetime import date, datetime
from models import AveragedLTEKPI, AveragedNR5GKPI

# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ══════════════════════════════════════════════════════════════════════════════
# SNMP Import with Fallback
# ══════════════════════════════════════════════════════════════════════════════
# snmpSend is imported inside a try/except so that file_manager continues
# operating even if pysnmp or snmpSend itself is unavailable (e.g. on a
# development machine without the SNMP stack installed). In that case,
# SNMP_READY is set to False and send_runtime_alarm is replaced with a
# no-op stub that logs what would have been sent instead of transmitting it.
try:
    from snmpSend import send_runtime_alarm, SNMP_READY  # ADDED SNMP_READY
except Exception as _snmp_err:
    print(f"{_ts()} [FILE] WARNING: snmpSend failed to import — {_snmp_err}. "
          f"SNMP alerts will be disabled for file_manager this session.")
    SNMP_READY = False  # ADDED — snmpSend itself unavailable
    def send_runtime_alarm(component: str, detail: str) -> None:
        print(f"{_ts()} [FILE] SNMP unavailable — would have sent RUNTIME alarm: "
              f"{component} | {detail}")


# ══════════════════════════════════════════════════════════════════════════════
# File Path and Retry Configuration
# ══════════════════════════════════════════════════════════════════════════════
KPI_DIR      = "/home/das/DAS-Communication-System/device/data/kpi_data" 
<<<<<<< HEAD
GUI_JSON_PATH = "/home/das/DAS-Communication-System/device/data/device_data.json"
=======
GUI_JSON_PATH = "/home/das/DAS-Communication-System/device/data/device_data_test.json"
CONFIG_PATH   = "/home/das/DAS-Communication-System/device/data/core/GUI/config.json"
>>>>>>> fd2b2e42c86a9e05b3b0369a00d67ec8e170f336
MAX_DAYS     = 7

# ── Write retry configuration ─────────────────────────────────────────────────
# Applied to both read retries (OSError only) and write retries.
_RETRY_SLEEP_SECONDS = 2
_WRITE_RETRY_COUNT   = 1   # one retry after initial failure

# ══════════════════════════════════════════════════════════════════════════════
# In-Memory Caches
# ══════════════════════════════════════════════════════════════════════════════
# Both caches start as None and are only populated after the first successful
# in-memory data build during normal operation. On first boot, hardcoded
# defaults or empty structures are used in their place.
#
# Cache update rule: updated every cycle from the in-memory data object
# BEFORE the write attempt, unconditionally. This ensures the cache always
# reflects the current intended state even when disk writes are failing.
# When the filesystem recovers, the full accumulated state is written at once.
_gui_cache   = None   # Holds full data dict last built for GUI JSON
_daily_cache = None   # Holds full daily_data dict including all entries


# ══════════════════════════════════════════════════════════════════════════════
# Internal Helpers
# ══════════════════════════════════════════════════════════════════════════════
def _send_trap(component: str, detail: str) -> None:
    """
    Safe wrapper around send_runtime_alarm().

    Ensures that a failure to send an SNMP trap never interrupts or
    propagates out of the calling except block. Always safe to call
    from within any error handler in this module.

    Args:
        component: Short label identifying what failed (e.g. "GUI JSON write").
        detail:    Human-readable description of the failure for the alarm log.
    """
    try:
        send_runtime_alarm(component=component, detail=detail)
    except Exception as e:
        print(f"{_ts()} [FILE] SNMP trap send failed for '{component}': {e}")


# ── Internal: Conversion helper ───────────────────────────────────────────────
def _averaged_to_dict(avg) -> dict:
    """
    Convert an AveragedLTEKPI or AveragedNR5GKPI object to a plain dict
    for JSON serialization.

    None KPI field values (produced when a metric was invalid for the window)
    are preserved as-is — json.dump() serializes Python None as JSON null,
    which is the intended representation for missing/invalid averaged data.

    The LTE path has been tested. The NR5G path is implemented but untested.

    NOTE — SUBJECT TO CHANGE: Field names and dict structure to be confirmed
    with partner once GUI multi-band support is fully implemented. Any change
    here must be reflected in the GUI's JSON parsing logic.

    Args:
        avg: An AveragedLTEKPI or AveragedNR5GKPI instance from alarms.py.

    Returns:
        dict: Serializable representation of the averaged KPI result.
    """
    base = {
        "rat":  avg.rat,
        "band": avg.band,
        "pci":  avg.pci,
    }

    if isinstance(avg, AveragedLTEKPI):
        base.update({
            "earfcn":   avg.earfcn,
            "avg_rssi": avg.avg_rssi,
            "avg_rsrp": avg.avg_rsrp,
            "avg_rsrq": avg.avg_rsrq,
            "avg_sinr": avg.avg_sinr,
        })
    elif isinstance(avg, AveragedNR5GKPI):
        base.update({
            "arfcn":       avg.arfcn,
            "avg_ss_rsrp": avg.avg_ss_rsrp,
            "avg_ss_rsrq": avg.avg_ss_rsrq,
            "avg_ss_sinr": avg.avg_ss_sinr,
        })

    return base


# ══════════════════════════════════════════════════════════════════════════════
# GUI JSON Update
# ══════════════════════════════════════════════════════════════════════════════
def update_gui_json(averaged_results: list) -> None:
    """
    Overwrite the bands section of device_data.json with the latest
    5-minute averaged KPI values. All other fields (device_status, logs,
    site_name, etc.) are preserved from the existing file or from cache
    if the file cannot be read.

    This is called once per 5-minute window by full_script.py, immediately
    after process_window() returns. The GUI reads this file to display
    live KPI data — it is always overwritten in full, never appended to.

    Processing flow:
        1. Read existing file — with per-error-type retry/fallback logic
        2. Update bands, last_update, snmp_status, device_id, site_name in memory
        3. Update _gui_cache unconditionally from the in-memory data object
        4. Write to file — retry once on failure, trap every cycle it persists

    Args:
        averaged_results: List of AveragedLTEKPI / AveragedNR5GKPI objects
                          returned by process_window() in alarms.py.
    """
    global _gui_cache

    if not averaged_results:
        print(f"{_ts()} [FILE] update_gui_json: no results to write — skipping.")
        return

    # ── Hardcoded defaults — used only on first boot when no cache exists ─────
    _DEFAULT_GUI_STRUCTURE = {
        "device_status": "ONLINE",
        "modem_status":  "CONNECTED",
        "vpn_status":    "ACTIVE",
        "snmp_status":   "RUNNING",
        "site_name":     "DAS",
        "device_id":     "",
        "alert_message": "No active alarms",
        "bands":         [],
        "logs":          []
    }

    # ── Step 1: Read existing file ────────────────────────────────────────────
    data = None

    try:
        with open(GUI_JSON_PATH, "r") as f:
            data = json.load(f)

    except OSError as e:
        # OSError (including EIO) can be transient on microSD — retry once.
        # If the retry also fails, fall back to cache or defaults and send trap.
        print(f"{_ts()} [FILE] GUI JSON read OSError: {e} — retrying in {_RETRY_SLEEP_SECONDS}s...")
        time.sleep(_RETRY_SLEEP_SECONDS)
        try:
            with open(GUI_JSON_PATH, "r") as f:
                data = json.load(f)
            print(f"{_ts()} [FILE] GUI JSON read retry succeeded.")
        except Exception as retry_e:
            # Retry also failed — use cache or defaults, send trap.
            print(f"{_ts()} [FILE] GUI JSON read retry failed: {retry_e} — using cache or defaults.")
            _send_trap(
                component = "GUI JSON read",
                detail    = "OSError (EIO): hardware I/O error on GUI JSON read, retry failed. Operator action required if issue persists."
            )
            data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except json.JSONDecodeError as e:
        # File is corrupted — retrying reads the same corrupt content, pointless.
        # Use cache (holds last valid state) or defaults on first boot.
        # No trap sent — this is self-healing, rebuilt every 5 minutes with no data loss.
        print(f"{_ts()} [FILE] GUI JSON corrupted (JSONDecodeError): {e} — using cache or defaults.")
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except PermissionError as e:
        # Permission error — retrying won't change the OS-level permission.
        # Operator must investigate (wrong file ownership, read-only mount, etc.)
        print(f"{_ts()} [FILE] GUI JSON read PermissionError: {e} — using cache or defaults.")
        _send_trap(
            component = "GUI JSON read",
            detail    = "PermissionError: process cannot read GUI JSON file. Operator action required."
        )
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except Exception as e:
        # Catches anything not covered above — most likely UnicodeDecodeError
        # from a partially written file with garbage bytes, but also guards
        # against any other unexpected read failure. Retrying would read the
        # same corrupt content, so fall back to cache or defaults directly.
        print(f"{_ts()} [FILE] GUI JSON read unexpected error ({type(e).__name__}): {e} "
              f"— using cache or defaults.")
        _send_trap(
            component = "GUI JSON read",
            detail    = f"{type(e).__name__}: unexpected read failure on GUI JSON — "
                        f"file may be partially written or corrupt. Using cache or defaults."
        )
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    # ── Step 2: Update In-Memory Data ─────────────────────────────────────────
    # end_time of the first averaged result is used as the window timestamp —
    # all bands share the same session window so any result's end_time is equivalent.
    data["last_update"] = averaged_results[0].end_time.strftime("%Y-%m-%d %H:%M:%S")
    data["bands"]       = [_averaged_to_dict(avg) for avg in averaged_results]
    data["snmp_status"] = "RUNNING" if SNMP_READY else "DOWN"  # ADDED
   
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        data["device_id"] = cfg.get("device_id", "")
        data["site_name"] = cfg.get("site_name", data.get("site_name", "DAS"))
    except Exception:
        pass

    # ── Step 3: Update Cache — Unconditionally, Before Write ──────────────────
    # Cache reflects the current intended state regardless of write outcome.
    # If the write below fails, the next cycle's cache will include this
    # cycle's data plus the next, ensuring nothing is lost on recovery.
    _gui_cache = data

    # ── Step 4: Write to file — with one retry ────────────────────────────────
    for write_attempt in range(_WRITE_RETRY_COUNT + 1):
        try:
            with open(GUI_JSON_PATH, "w") as f:
                json.dump(data, f, indent=4)
            print(f"{_ts()} [FILE] GUI JSON updated at {data['last_update']}")
            return  # Success — exit function

        except Exception as write_e:
            if write_attempt < _WRITE_RETRY_COUNT:
                print(f"{_ts()} [FILE] GUI JSON write failed: {write_e} — "
                      f"retrying in {_RETRY_SLEEP_SECONDS}s...")
                time.sleep(_RETRY_SLEEP_SECONDS)
            else:
                # Retry also failed — send trap. Trap fires every cycle the
                # failure persists so the operator is continuously alerted.
                print(f"{_ts()} [FILE] GUI JSON write failed after retry: {write_e}")
                _send_trap(
                    component = "GUI JSON write",
                    detail    = f"{type(write_e).__name__}: GUI JSON write failed after retry: {write_e}. "
                                f"Operator action required if issue persists."
                )


# ══════════════════════════════════════════════════════════════════════════════
# Daily KPI File Append
# ══════════════════════════════════════════════════════════════════════════════

def append_to_daily_file(averaged_results: list) -> None:
    """
    Append the current window's averaged KPI values to today's daily log file.

    Daily files are named YYYYMMDD_kpi.json and stored in KPI_DIR. Each call
    appends one entry (a start_time, end_time, and list of band results) to
    the "entries" array. After a successful write, the 7-day retention cleanup
    is run to delete the oldest file if more than MAX_DAYS files exist.

    Unlike update_gui_json (which overwrites), this function reads the existing
    file, appends the new entry in memory, then writes the full updated structure
    back. This preserves all previous entries from the current day in a single file.

    Processing flow:
        1. Attempt os.makedirs — track failure, still proceed to cache update
        2. Read existing file (if makedirs succeeded and file exists) with
           per-error-type retry/fallback; use cache or empty structure otherwise
        3. Append current entry to daily_data in memory
        4. Update _daily_cache unconditionally from in-memory data
        5. If makedirs failed: send trap and return (cannot write without directory)
        6. Write to file — retry once on failure, trap every cycle it persists
        7. 7-day cleanup — trap if it fails, always continue regardless

    Args:
        averaged_results: List of AveragedLTEKPI / AveragedNR5GKPI objects
                          returned by process_window() in alarms.py.
    """
    global _daily_cache

    if not averaged_results:
        print(f"{_ts()} [FILE] append_to_daily_file: no results to write — skipping.")
        return

    today    = date.today().strftime("%Y%m%d")
    filepath = os.path.join(KPI_DIR, f"{today}_kpi.json")

    # ── Step 1: Ensure Directory Exists ───────────────────────────────────────
    # Tracked separately so the cache update in Step 4 still happens even if
    # the directory cannot be created — the cache must always be current.
    # exist_ok=True means no error is raised if the directory already exists.
    makedirs_failed = False
    try:
        os.makedirs(KPI_DIR, exist_ok=True)
    except Exception as e:
        print(f"{_ts()} [FILE] os.makedirs failed: {e} — will update cache but cannot write.")
        makedirs_failed = True

    # ── Step 2: Read existing file or use cache/empty ─────────────────────────
    daily_data = None

    if makedirs_failed:
        # Directory doesn't exist — no file to read.
        # Use cache to preserve entries accumulated so far today, or
        # start a fresh empty structure if this is the first entry.
        daily_data = _daily_cache if _daily_cache is not None else {
            "date": today, "entries": []
        }

    else:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    daily_data = json.load(f)

            except OSError as e:
                # Transient hardware glitch — retry once.
                print(f"{_ts()} [FILE] Daily file read OSError: {e} — "
                      f"retrying in {_RETRY_SLEEP_SECONDS}s...")
                time.sleep(_RETRY_SLEEP_SECONDS)
                try:
                    with open(filepath, "r") as f:
                        daily_data = json.load(f)
                    print(f"{_ts()} [FILE] Daily file read retry succeeded.")
                except Exception as retry_e:
                    # Retry failed — use cache to preserve previous entries,
                    # empty structure only if no cache exists yet.
                    print(f"{_ts()} [FILE] Daily file read retry failed: {retry_e} — "
                          f"using cache or empty structure.")
                    _send_trap(
                        component = "daily file read",
                        detail    = "OSError (EIO): hardware I/O error on daily KPI file read, retry failed. Operator action required if issue persists."
                    )
                    daily_data = _daily_cache if _daily_cache is not None else {
                        "date": today, "entries": []
                    }

            except json.JSONDecodeError as e:
                # File is corrupted — no retry. Use cache to recover as many
                # previous entries as possible. Data loss is at most one entry
                # (the write that corrupted the file).
                print(f"{_ts()} [FILE] Daily file corrupted (JSONDecodeError): {e} — "
                      f"recovering from cache.")
                _send_trap(
                    component = "daily file read",
                    detail    = "JSONDecodeError: daily KPI file contains invalid JSON. Data since last successful write may be lost. Operator action required if issue persists."
                )
                daily_data = _daily_cache if _daily_cache is not None else {
                    "date": today, "entries": []
                }

            except PermissionError as e:
                # Read permission error — still attempt the write separately since
                # read and write permissions are independent in Linux. The write
                # may succeed even if the read failed.
                print(f"{_ts()} [FILE] Daily file read PermissionError: {e} — "
                      f"using cache, will still attempt write.")
                _send_trap(
                    component = "daily file read",
                    detail    = "PermissionError: process cannot read daily KPI file. Operator action required."
                )
                daily_data = _daily_cache if _daily_cache is not None else {
                    "date": today, "entries": []
                }

            except Exception as e:
                # Catches anything not covered above — most likely UnicodeDecodeError
                # from a partially written file with garbage bytes, but also guards
                # against any other unexpected read failure. Retrying would read the
                # same corrupt content, so fall back to cache or empty structure directly.
                print(f"{_ts()} [FILE] Daily file read unexpected error ({type(e).__name__}): {e} "
                      f"— using cache or empty structure.")
                _send_trap(
                    component = "daily file read",
                    detail    = f"{type(e).__name__}: unexpected read failure on daily KPI file — "
                                f"file may be partially written or corrupt. Using cache or empty structure."
                )
                daily_data = _daily_cache if _daily_cache is not None else {
                    "date": today, "entries": []
                }

        else:
            # File doesn't exist yet — first entry of the day or first boot.
            # A new file will be created during the write step below.
            daily_data = {"date": today, "entries": []}
            print(f"[FILE] New daily KPI file will be created → {filepath}")

    # ── Step 3: Append Current Entry to In-Memory Data ────────────────────────
    # Build one entry dict for this window and add it to the entries list.
    # start_time and end_time bound the 5-minute averaging window.
    # bands holds the per-band averaged KPI dicts from _averaged_to_dict().
    entry = {
        "start_time": averaged_results[0].start_time.strftime("%H:%M:%S"),
        "end_time":   averaged_results[0].end_time.strftime("%H:%M:%S"),
        "bands":      [_averaged_to_dict(avg) for avg in averaged_results]
    }
    daily_data["entries"].append(entry)

    # ── Step 4: Update Cache — Unconditionally, Before Write ──────────────────
    # Cache reflects the full intended state of today's file including this
    # entry. If the write fails, the next cycle's cache will include both this
    # entry and the next, so the full history is written when the disk recovers.
    _daily_cache = daily_data

    # ── Step 5: Return Early if Directory Creation Failed ─────────────────────
    # Cannot write to a file in a directory that doesn't exist.
    # Data is held in _daily_cache only — it will be lost on power-off if the
    # filesystem issue is not resolved. Trap fires to alert the operator.
    if makedirs_failed:
        _send_trap(
            component = "daily file makedirs",
            detail    = "OSError: KPI data directory inaccessible. Data held in memory only — will be lost on power off. Operator action required."
        )
        return

    # ── Step 6: Write to File — With One Retry ────────────────────────────────
    # Writes the full daily_data structure (all entries today) back to disk.
    # write_succeeded controls whether the cleanup step runs — cleanup is
    # skipped if the write failed since the on-disk file state is uncertain.
    write_succeeded = False
    for write_attempt in range(_WRITE_RETRY_COUNT + 1):
        try:
            with open(filepath, "w") as f:
                json.dump(daily_data, f, indent=4)
            print(f"[FILE] Entry appended to {filepath} "
                  f"({len(daily_data['entries'])} entries today)")
            write_succeeded = True
            break

        except Exception as write_e:
            if write_attempt < _WRITE_RETRY_COUNT:
                print(f"[FILE] Daily file write failed: {write_e} — "
                      f"retrying in {_RETRY_SLEEP_SECONDS}s...")
                time.sleep(_RETRY_SLEEP_SECONDS)
            else:
                print(f"[FILE] Daily file write failed after retry: {write_e}")
                _send_trap(
                    component = "daily file write",
                    detail    = f"{type(write_e).__name__}: daily KPI file write failed after retry: {write_e}. "
                                f"Operator action required if issue persists."
                )

    if not write_succeeded:
        # Write failed — skip cleanup since the file state on disk is uncertain.
        return

    # ── Step 7: 7-Day Retention Cleanup ───────────────────────────────────────
    # Runs after every successful write. Finds all daily KPI files sorted
    # alphabetically (YYYYMMDD prefix makes this equivalent to chronological order)
    # and deletes the oldest if the count exceeds MAX_DAYS.
    # Failure here does not affect this cycle's written data — old files will
    # simply accumulate until the operator resolves the underlying filesystem issue.
    # No trap sent — the script retries cleanup automatically every 5 minutes,
    # so transient failures self-resolve without operator intervention.
    try:
        files = sorted(glob.glob(os.path.join(KPI_DIR, "*_kpi.json")))
        if len(files) > MAX_DAYS:
            os.remove(files[0])
            print(f"[CLEANUP] Removed oldest KPI file: {files[0]}")
    except Exception as cleanup_e:
        # Cleanup failure does not affect this cycle's data — it was already
        # written successfully. Old files will accumulate until operator
        # intervenes to fix the underlying permissions or filesystem issue.
        # No trap sent — script retries cleanup automatically every 5 minutes.
        print(f"[FILE] Cleanup failed: {cleanup_e} — old files may accumulate.")


# ══════════════════════════════════════════════════════════════════════════════
# update_vpn_status
# ══════════════════════════════════════════════════════════════════════════════

def update_vpn_status(vpn_status: str) -> None:
    """
    Update only the vpn_status field in device_data.json without modifying
    any other fields. Called by full_script.py whenever the VPN tunnel
    state changes so the GUI dashboard reflects current connectivity.

    Uses the same read → update → cache → write pattern as update_gui_json()
    with identical retry and fallback logic. See update_gui_json() for the
    full rationale behind each step.

    Processing flow:
        1. Read existing file — with per-error-type retry/fallback logic
        2. Update vpn_status field in memory
        3. Update _gui_cache unconditionally from the in-memory data object
        4. Write to file — retry once on failure, trap every cycle it persists

    Args:
        vpn_status: Status string to write — "ACTIVE" or "DOWN".
    """
    global _gui_cache

    _DEFAULT_GUI_STRUCTURE = {
        "device_status": "ONLINE",
        "modem_status":  "CONNECTED",
        "vpn_status":    "ACTIVE",
        "snmp_status":   "RUNNING",
        "site_name":     "DAS",
        "alert_message": "No active alarms",
        "bands":         [],
        "logs":          []
    }

    # ── Step 1: Read existing file ────────────────────────────────────────────
    data = None

    try:
        with open(GUI_JSON_PATH, "r") as f:
            data = json.load(f)

    except OSError as e:
        # OSError can be transient (EIO hardware glitch) — retry once.
        print(f"{_ts()} [FILE] VPN status read OSError: {e} — retrying in {_RETRY_SLEEP_SECONDS}s...")
        time.sleep(_RETRY_SLEEP_SECONDS)
        try:
            with open(GUI_JSON_PATH, "r") as f:
                data = json.load(f)
            print(f"{_ts()} [FILE] VPN status read retry succeeded.")
        except Exception as retry_e:
            print(f"{_ts()} [FILE] VPN status read retry failed: {retry_e} — using cache or defaults.")
            _send_trap(
                component = "VPN status read",
                detail    = "OSError: hardware I/O error on VPN status read, retry failed."
            )
            data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except json.JSONDecodeError as e:
        print(f"{_ts()} [FILE] VPN status file corrupted (JSONDecodeError): {e} — using cache or defaults.")
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except PermissionError as e:
        print(f"{_ts()} [FILE] VPN status read PermissionError: {e} — using cache or defaults.")
        _send_trap(
            component = "VPN status read",
            detail    = "PermissionError: process cannot read VPN status file."
        )
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except Exception as e:
        print(f"{_ts()} [FILE] VPN status read unexpected error ({type(e).__name__}): {e} — using cache or defaults.")
        _send_trap(
            component = "VPN status read",
            detail    = f"{type(e).__name__}: unexpected read failure on VPN status."
        )
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    # ── Step 2: Update in-memory data ─────────────────────────────────────────
    data["vpn_status"] = vpn_status

    # ── Step 3: Update cache — always, every cycle, before write ──────────────
    _gui_cache = data

    # ── Step 4: Write to file — with one retry ────────────────────────────────
    for write_attempt in range(_WRITE_RETRY_COUNT + 1):
        try:
            with open(GUI_JSON_PATH, "w") as f:
                json.dump(data, f, indent=4)
            print(f"{_ts()} [FILE] VPN status updated to: {vpn_status}")
            return  # Success — exit function

        except Exception as write_e:
            if write_attempt < _WRITE_RETRY_COUNT:
                print(f"{_ts()} [FILE] VPN status write failed: {write_e} — "
                      f"retrying in {_RETRY_SLEEP_SECONDS}s...")
                time.sleep(_RETRY_SLEEP_SECONDS)
            else:
                print(f"{_ts()} [FILE] VPN status write failed after retry: {write_e}")
                _send_trap(
                    component = "VPN status write",
                    detail    = f"{type(write_e).__name__}: VPN status write failed after retry: {write_e}."
                )

