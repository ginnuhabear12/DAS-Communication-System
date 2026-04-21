"""
file_manager.py — KPI File Management for DAS Communication System
------------------------------------------------------------------
Handles two writes per 5-minute window:
    1. Overwrite device_data.json  → updates the live GUI
    2. Append to kpi_YYYY-MM-DD.json → builds the daily KPI log on microSD
    3. Cleanup → enforces 7-day rolling retention

Error Handling Philosophy:
    - Every filesystem operation is individually protected.
    - Cache (_gui_cache, _daily_cache) is updated from memory every cycle
      before the write attempt, unconditionally — so the cache always holds
      the most current intended state regardless of write outcome.
    - On read failure: retry once for OSError (transient hardware glitch),
      no retry for JSONDecodeError or PermissionError (retrying won't help).
      Fall back to cache if available, defaults/empty structure if first boot.
    - On write failure: retry once, then send SNMP runtime trap every cycle
      the failure persists so the operator is alerted.
    - All SNMP trap sends are wrapped so a trap failure never interrupts
      the main error handling path.
"""

import json
import os
import glob
import time
from datetime import date
from models import AveragedLTEKPI, AveragedNR5GKPI

# FIX: Protected snmpSend import — if pysnmp or snmpSend is unavailable,
# fall back to a no-op stub so file_manager continues operating.
try:
    from snmpSend import send_runtime_alarm
except Exception as _snmp_err:
    print(f"[FILE] WARNING: snmpSend failed to import — {_snmp_err}. "
          f"SNMP alerts will be disabled for file_manager this session.")
    def send_runtime_alarm(component: str, detail: str) -> None:
        print(f"[FILE] SNMP unavailable — would have sent RUNTIME alarm: "
              f"{component} | {detail}")


# ── File Paths ────────────────────────────────────────────────────────────────
KPI_DIR      = "/home/das/DAS-Communication-System/device/core/kpi_data"
GUI_JSON_PATH = "/home/das/DAS-Communication-System/device/core/device_data_test.json"
MAX_DAYS     = 7

# ── Write retry configuration ─────────────────────────────────────────────────
# Applied to both read retries (OSError only) and write retries.
_RETRY_SLEEP_SECONDS = 2
_WRITE_RETRY_COUNT   = 1   # one retry after initial failure

# ── In-memory caches ──────────────────────────────────────────────────────────
# Both caches start as None — only populated after the first successful
# in-memory data build. On first boot, hardcoded defaults or empty structures
# are used instead.
#
# Cache update happens every cycle from the in-memory data object before the
# write attempt — unconditionally — so the cache always reflects the most
# current intended state regardless of whether the write to disk succeeded.
# This means if writes fail for multiple cycles, all entries accumulate in
# the cache and are written in full when the filesystem recovers.
_gui_cache   = None   # Holds full data dict last built for GUI JSON
_daily_cache = None   # Holds full daily_data dict including all entries


# ── Internal: SNMP trap helper ────────────────────────────────────────────────
def _send_trap(component: str, detail: str) -> None:
    """
    Wraps send_runtime_alarm so a trap send failure never interrupts
    the calling error handler. Always safe to call from any except block.
    """
    try:
        send_runtime_alarm(component=component, detail=detail)
    except Exception as e:
        print(f"[FILE] SNMP trap send failed for '{component}': {e}")


# ── Internal: Conversion helper ───────────────────────────────────────────────
def _averaged_to_dict(avg) -> dict:
    """
    Converts an AveragedLTEKPI or AveragedNR5GKPI object into a dictionary
    for JSON serialization. None KPI values are preserved as JSON null.

    SUBJECT TO CHANGE — field names and structure to be confirmed
    with partner once GUI multi-band support is implemented.
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
# update_gui_json
# ══════════════════════════════════════════════════════════════════════════════

def update_gui_json(averaged_results: list) -> None:
    """
    Overwrites the bands section of device_data_test.json with the latest
    time-averaged values. All other fields are preserved from the existing
    file or from cache if the file cannot be read.

    Flow:
        1. Attempt read — with retry/fallback logic per error type
        2. Update in-memory data with new KPI values and timestamp
        3. Update _gui_cache from memory — always, every cycle
        4. Attempt write — retry once on failure, trap every cycle it persists

    Args:
        averaged_results: List of AveragedLTEKPI / AveragedNR5GKPI objects.

    SUBJECT TO CHANGE — JSON file path and structure to be confirmed
    with partner once GUI multi-band support is implemented.
    """
    global _gui_cache

    if not averaged_results:
        print("[FILE] update_gui_json: no results to write — skipping.")
        return

    # ── Hardcoded defaults — used only on first boot when no cache exists ─────
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
        print(f"[FILE] GUI JSON read OSError: {e} — retrying in {_RETRY_SLEEP_SECONDS}s...")
        time.sleep(_RETRY_SLEEP_SECONDS)
        try:
            with open(GUI_JSON_PATH, "r") as f:
                data = json.load(f)
            print("[FILE] GUI JSON read retry succeeded.")
        except Exception as retry_e:
            # Retry also failed — use cache or defaults, send trap.
            print(f"[FILE] GUI JSON read retry failed: {retry_e} — using cache or defaults.")
            _send_trap(
                component = "GUI JSON read",
                detail    = "OSError (EIO): hardware I/O error on GUI JSON read, retry failed. Operator action required if issue persists."
            )
            data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except json.JSONDecodeError as e:
        # File is corrupted — retrying reads the same corrupt content, pointless.
        # Use cache (holds last valid state) or defaults on first boot.
        # No trap sent — this is self-healing, rebuilt every 5 minutes with no data loss.
        print(f"[FILE] GUI JSON corrupted (JSONDecodeError): {e} — using cache or defaults.")
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    except PermissionError as e:
        # Permission error — retrying won't fix it, operator must intervene.
        print(f"[FILE] GUI JSON read PermissionError: {e} — using cache or defaults.")
        _send_trap(
            component = "GUI JSON read",
            detail    = "PermissionError: process cannot read GUI JSON file. Operator action required."
        )
        data = _gui_cache if _gui_cache is not None else dict(_DEFAULT_GUI_STRUCTURE)

    # ── Step 2: Update in-memory data ─────────────────────────────────────────
    # bands and last_update are always replaced from averaged_results.
    # All other fields (device_status, alert_message, etc.) are preserved
    # from the read, from cache, or from defaults — whichever succeeded above.
    data["last_update"] = averaged_results[0].end_time.strftime("%Y-%m-%d %H:%M:%S")
    data["bands"]       = [_averaged_to_dict(avg) for avg in averaged_results]

    # ── Step 3: Update cache — always, every cycle, before write ──────────────
    # Cache is updated unconditionally from the in-memory data object.
    # Even if the write below fails, the cache holds the current intended
    # state so it accumulates correctly across failing cycles.
    _gui_cache = data

    # ── Step 4: Write to file — with one retry ────────────────────────────────
    for write_attempt in range(_WRITE_RETRY_COUNT + 1):
        try:
            with open(GUI_JSON_PATH, "w") as f:
                json.dump(data, f, indent=4)
            print(f"[FILE] GUI JSON updated at {data['last_update']}")
            return  # Success — exit function

        except Exception as write_e:
            if write_attempt < _WRITE_RETRY_COUNT:
                print(f"[FILE] GUI JSON write failed: {write_e} — "
                      f"retrying in {_RETRY_SLEEP_SECONDS}s...")
                time.sleep(_RETRY_SLEEP_SECONDS)
            else:
                # All write attempts failed — trap every cycle it persists.
                print(f"[FILE] GUI JSON write failed after retry: {write_e}")
                _send_trap(
                    component = "GUI JSON write",
                    detail    = "OSError: GUI JSON write failed after retry. Operator action required if issue persists."
                )


# ══════════════════════════════════════════════════════════════════════════════
# append_to_daily_file
# ══════════════════════════════════════════════════════════════════════════════

def append_to_daily_file(averaged_results: list) -> None:
    """
    Appends the current window's time-averaged KPI values to today's daily file.

    Flow:
        1. Attempt os.makedirs — track failure, still proceed to cache update
        2. If makedirs succeeded: attempt read with retry/fallback logic
           If makedirs failed: use cache or empty structure (no file to read)
        3. Append current entry to daily_data in memory
        4. Update _daily_cache — always, every cycle, unconditionally
        5. If makedirs failed: send trap and return (can't write without directory)
        6. Attempt write — retry once on failure, trap every cycle it persists
        7. 7-day cleanup — trap if fails, always continue

    Args:
        averaged_results: List of AveragedLTEKPI / AveragedNR5GKPI objects.
    """
    global _daily_cache

    if not averaged_results:
        print("[FILE] append_to_daily_file: no results to write — skipping.")
        return

    today    = date.today().strftime("%Y%m%d")
    filepath = os.path.join(KPI_DIR, f"{today}_kpi.json")

    # ── Step 1: Attempt os.makedirs ───────────────────────────────────────────
    # Tracked separately so cache update (step 4) still happens even if
    # the directory cannot be created. Cache must always reflect current data.
    makedirs_failed = False
    try:
        os.makedirs(KPI_DIR, exist_ok=True)
    except Exception as e:
        print(f"[FILE] os.makedirs failed: {e} — will update cache but cannot write.")
        makedirs_failed = True

    # ── Step 2: Read existing file or use cache/empty ─────────────────────────
    daily_data = None

    if makedirs_failed:
        # Directory doesn't exist — no file to read, use cache or empty structure.
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
                print(f"[FILE] Daily file read OSError: {e} — "
                      f"retrying in {_RETRY_SLEEP_SECONDS}s...")
                time.sleep(_RETRY_SLEEP_SECONDS)
                try:
                    with open(filepath, "r") as f:
                        daily_data = json.load(f)
                    print("[FILE] Daily file read retry succeeded.")
                except Exception as retry_e:
                    # Retry failed — use cache to preserve previous entries,
                    # empty structure only if no cache exists yet.
                    print(f"[FILE] Daily file read retry failed: {retry_e} — "
                          f"using cache or empty structure.")
                    _send_trap(
                        component = "daily file read",
                        detail    = "OSError (EIO): hardware I/O error on daily KPI file read, retry failed. Operator action required if issue persists."
                    )
                    daily_data = _daily_cache if _daily_cache is not None else {
                        "date": today, "entries": []
                    }

            except json.JSONDecodeError as e:
                # File is corrupted — use cache to recover all previous entries.
                # Cache holds everything up to the last successful write so
                # data loss is minimal (at most one entry from the failed write
                # that caused the corruption).
                print(f"[FILE] Daily file corrupted (JSONDecodeError): {e} — "
                      f"recovering from cache.")
                _send_trap(
                    component = "daily file read",
                    detail    = "JSONDecodeError: daily KPI file contains invalid JSON. Data since last successful write may be lost. Operator action required if issue persists."
                )
                daily_data = _daily_cache if _daily_cache is not None else {
                    "date": today, "entries": []
                }

            except PermissionError as e:
                # Permission error on read — still attempt write separately
                # since read/write permissions are independent in Linux.
                print(f"[FILE] Daily file read PermissionError: {e} — "
                      f"using cache, will still attempt write.")
                _send_trap(
                    component = "daily file read",
                    detail    = "PermissionError: process cannot read daily KPI file. Operator action required."
                )
                daily_data = _daily_cache if _daily_cache is not None else {
                    "date": today, "entries": []
                }

        else:
            # File doesn't exist yet — first entry of the day or first boot.
            daily_data = {"date": today, "entries": []}
            print(f"[FILE] New daily KPI file will be created → {filepath}")

    # ── Step 3: Append current entry to in-memory data ────────────────────────
    entry = {
        "start_time": averaged_results[0].start_time.strftime("%H:%M:%S"),
        "end_time":   averaged_results[0].end_time.strftime("%H:%M:%S"),
        "bands":      [_averaged_to_dict(avg) for avg in averaged_results]
    }
    daily_data["entries"].append(entry)

    # ── Step 4: Update cache — always, every cycle, unconditionally ───────────
    # Cache updates from the in-memory daily_data object before any write
    # attempt. This ensures the cache accumulates all entries even across
    # cycles where the write fails, so when the filesystem recovers,
    # the full history is written at once with nothing missing.
    _daily_cache = daily_data

    # ── Step 5: Return if makedirs failed — can't write without directory ─────
    if makedirs_failed:
        _send_trap(
            component = "daily file makedirs",
            detail    = "OSError: KPI data directory inaccessible. Data held in memory only — will be lost on power off. Operator action required."
        )
        return

    # ── Step 6: Write to file — with one retry ────────────────────────────────
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
                    detail    = "OSError: daily KPI file write failed after retry. Operator action required if issue persists."
                )

    if not write_succeeded:
        # Write failed — skip cleanup since the file state on disk is uncertain.
        return

    # ── Step 7: 7-day retention cleanup ──────────────────────────────────────
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