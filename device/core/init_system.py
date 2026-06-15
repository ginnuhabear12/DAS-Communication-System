#!/usr/bin/env python3


"""
Module: init_system.py
Purpose: Runs a sequential set of system readiness checks before the main
         monitoring script is allowed to start. Verifies that storage is
         writable, the config file is valid, the network interface is up,
         the modem is physically present, and the VPN tunnel is available.

         On success, writes a ready flag file that the systemd service
         uses as the signal to launch the main monitoring process.
         On any hard failure, the flag is removed and the process exits
         with a non-zero code to prevent the main script from starting
         in a broken state.

         Checks that are non-fatal (VPN, some config fields) issue warnings
         and allow the system to continue in a degraded mode rather than
         aborting entirely.
"""
from pathlib import Path
import json
import subprocess
import sys
import time


# ══════════════════════════════════════════════════════════════════════════════
# Path Constants
# All filesystem paths used during initialization are defined here so they
# are easy to update if the deployment directory structure changes.
# ══════════════════════════════════════════════════════════════════════════════
READY_DIR = Path("/run/das")
READY_FILE = READY_DIR / "init.ready"
CONFIG_PATH = Path("/home/das/DAS-Communication-System/device/GUI/config.json")
DATA_DIR = Path("/home/das/DAS-Communication-System/data")
LOG_DIR = Path("/home/das/DAS-Communication-System/logs")

# ══════════════════════════════════════════════════════════════════════════════
# Logging Helpers
# Lightweight wrappers that prefix all init output with [INIT][OK] or
# [INIT][FAIL] so log output is easy to grep and visually scan.
# ══════════════════════════════════════════════════════════════════════════════
def fail(msg: str, code: int = 1):
    """
    Print a hard failure message, remove the ready flag if it exists,
    and exit the process with the given code (default 1).
    Called for any condition that must prevent the main script from starting.
    """
    print(f"[INIT][FAIL] {msg}", flush=True)
    # Remove the ready flag so the systemd service does not mistakenly
    # treat a previous successful run's flag as clearance to proceed.
    if READY_FILE.exists():
        READY_FILE.unlink()
    sys.exit(code)


def ok(msg: str):
    print(f"[INIT][OK] {msg}", flush=True)

# ══════════════════════════════════════════════════════════════════════════════
# Initialization Checks
# Each function below validates one aspect of system readiness.
# They are called in order by main() and either pass silently (ok()),
# warn ([INIT][WARN]), or hard-fail (fail()) depending on severity.
# ══════════════════════════════════════════════════════════════════════════════
def check_storage():
    """
    Verify that the log and data directories exist and are writable.

    Creates both directories if they don't exist yet (parents=True means
    intermediate directories are created automatically; exist_ok=True means
    no error if they already exist).

    Then performs a write/delete test in the data directory to confirm the
    filesystem is actually writable at runtime, not just accessible by path.
    Hard-fails if the write raises an exception (caught at the OS level).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create and immediately delete a small test file to confirm write access.
    # A hidden filename (leading dot) avoids polluting the data directory listing.
    test_file = DATA_DIR / ".write_test"
    test_file.write_text("ok")
    test_file.unlink()

    ok("storage writable")


def check_config():
    """
    Validate that the GUI config file exists, is valid JSON, and contains
    all required keys for the current Radio Access Technology (RAT) mode.

    Required keys (hard fail if missing):
        site_name, device_id, snmp_host

    RAT-specific keys (soft warn only — system continues in degraded mode):
        LTE mode  → earfcn
        5G mode   → nr_band, nr_arfcn

    Note: poll_interval, snmp_community, and rat were previously required
    and would hard-fail if absent. They have since been made optional or
    moved to constants.py — see the archived version at the bottom of this
    file for the stricter original implementation.
    """
    if not CONFIG_PATH.exists():
        fail(f"missing config file: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    # These keys must be present for the system to function at all.
    # Any missing key is a hard failure — the main script cannot run safely without them.
    required = [
        "site_name",
        "device_id",
        # "poll_interval",
        "snmp_host",
    ]

    for key in required:
        if key not in config:
            fail(f"missing config key: {key}")

    # Default to "LTE" if rat is not specified — .get() avoids a KeyError.
    # Converted to uppercase so "lte", "LTE", and "Lte" are all treated the same.
    rat = str(config.get("rat", "LTE")).upper()

    # DO NOT hard fail on LTE/5G params — just warn
    # RAT-specific parameter warnings — these do not abort initialization,
    # but the missing values will need to be set before KPI collection works correctly.
    if rat == "LTE":
        if "earfcn" not in config:
            print("[INIT][WARN] earfcn missing for LTE mode", flush=True)
    elif rat == "5G":
        if "nr_band" not in config:
            print("[INIT][WARN] nr_band missing for 5G mode", flush=True)
        if "nr_arfcn" not in config:
            print("[INIT][WARN] nr_arfcn missing for 5G mode", flush=True)
    else:
        # rat is present but not a recognized value — warn without failing
        # so the operator can correct it without a full restart being required.
        print(f"[INIT][WARN] invalid rat value: {rat}", flush=True)

    ok("config valid")


def check_network():
    """
    Confirm that the primary Ethernet interface (eth0) has an IPv4 address.

    Uses the system 'ip' command rather than a Python socket check so that
    the result reflects the actual network interface state as the OS sees it.
    Hard-fails if eth0 has no inet address — SNMP reporting requires network access.
    """
    result = subprocess.run(
        ["ip", "addr", "show", "eth0"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    if "inet " not in result.stdout:
        fail("eth0 has no IPv4 address")

    ok("network up")


def check_vpn():
    """
    Check whether the OpenVPN tunnel interface (tun0) is present.

    This is a soft check — VPN absence is logged as a warning but does not
    abort initialization. The system continues in degraded mode, which means
    SNMP traps may not reach the manager if it is only accessible over VPN.
    """
    result = subprocess.run(
        ["ip", "addr", "show", "tun0"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    if result.returncode == 0:
        ok("vpn tunnel present")
    else:
        # tun0 not found — VPN is down or not yet established.
        # Continuing anyway since local monitoring can still run;
        # SNMP delivery will fail silently until the tunnel comes up.
        print("[INIT][WARN] tun0 not present, continuing in degraded mode", flush=True)


def check_modem():
    """
    Verify that at least one Quectel modem serial device is present under /dev/.

    Quectel modems enumerate as multiple ttyUSB interfaces (typically 0–3).
    AT commands are sent on ttyUSB2 (see modem.py), but here we just confirm
    that any of the expected device files exist — if none do, the modem is
    either unplugged or has not been recognized by the OS yet.
    Hard-fails if no ttyUSB device is found, since KPI collection is impossible without the modem.
    """
    devs = [
        Path("/dev/ttyUSB0"),
        Path("/dev/ttyUSB1"),
        Path("/dev/ttyUSB2"),
        Path("/dev/ttyUSB3"),
    ]

    if not any(d.exists() for d in devs):
        fail("no modem ttyUSB device found")

    ok("modem serial device detected")

# ══════════════════════════════════════════════════════════════════════════════
# Ready Flag
# ══════════════════════════════════════════════════════════════════════════════
def mark_ready():
    """
    Write the ready flag file that signals successful initialization.

    The systemd service unit for the main monitoring script uses this file
    as its start condition (ConditionPathExists). Writing it here — only
    after all checks pass — ensures the main script never starts in a
    broken environment.
    """
    READY_DIR.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text("ready\n")
    ok(f"created ready flag: {READY_FILE}")


def main():
    """
    Run all initialization checks in order, then write the ready flag.
    The 2-second sleep before check_modem() gives USB devices time to
    fully enumerate after boot before we check for their presence.
    """
    print("[INIT] starting system initialization", flush=True)

    check_storage()
    check_config()
    check_network()

    # Brief pause to allow USB subsystem and modem to finish enumerating
    # before checking for ttyUSB device files. Without this, check_modem()
    # can fail on fast boots even when the modem is physically connected.
    time.sleep(2)
    check_modem()
    check_vpn()

    mark_ready()
    print("[INIT] initialization complete", flush=True)


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════════════════
# ARCHIVED: Previous version of init_system.py
# Kept for reference only — this code is not executed.
# Key differences from the active version above:
#   - check_config() used a stricter required key list (poll_interval,
#     snmp_community, rat were all hard-fail required)
#   - RAT-specific missing keys (earfcn, nr_band, nr_arfcn) caused hard
#     failures instead of warnings
#   - check_storage() did not delete the write test file after creating it
#   - A second incomplete check_config() definition was present (now removed)
# ══════════════════════════════════════════════════════════════════════════════


# #!/usr/bin/env python3

# from pathlib import Path
# import json
# import subprocess
# import sys
# import time

# READY_DIR = Path("/run/das") 
# READY_FILE = READY_DIR / "init.ready" #temp file to show that the system is ready to run. This is used by the systemd service to determine when to start the main script. It is created at the end of the initialization process and deleted if any checks fail
# CONFIG_PATH = Path("/home/das/DAS-Communication-System/device/GUI/config.json")
# DATA_DIR = Path("/home/das/DAS-Communication-System/data")
# LOG_DIR = Path("/home/das/DAS-Communication-System/logs")


# def fail(msg: str, code: int = 1):
    # print(f"[INIT][FAIL] {msg}", flush=True)
    # if READY_FILE.exists():
        # READY_FILE.unlink()
    # sys.exit(code)


# def ok(msg: str):
    # print(f"[INIT][OK] {msg}", flush=True)


# def check_storage():
    # LOG_DIR.mkdir(parents=True, exist_ok=True)
    # DATA_DIR.mkdir(parents=True, exist_ok=True)

    # test_file = DATA_DIR / ".write_test"
    # test_file.write_text("ok")
    # ok("storage writable")

# def check_config():
    # if not CONFIG_PATH.exists():
        # fail(f"missing config file: {CONFIG_PATH}")

    # with open(CONFIG_PATH, "r") as f:
        # config = json.load(f)

    # required = [
        # "site_name",
        # "device_id",
        # "poll_interval",
        # "snmp_host",
        # "snmp_community",
        # "rat",
    # ]

    # for key in required:
        # if key not in config:
            # fail(f"missing config key: {key}")

    # rat = str(config["rat"]).upper()

    # if rat == "LTE":
        # if "earfcn" not in config:
            # fail("missing config key: earfcn for LTE mode")
    # elif rat == "5G":
        # if "nr_band" not in config:
            # fail("missing config key: nr_band for 5G mode")
        # if "nr_arfcn" not in config:
            # fail("missing config key: nr_arfcn for 5G mode")
    # else:
        # fail(f"invalid rat value: {config['rat']}")

    # ok("config valid")

# def check_config():
    # if not CONFIG_PATH.exists():
        # fail(f"missing config file: {CONFIG_PATH}")

    # with open(CONFIG_PATH, "r") as f:
        # config = json.load(f)

    # for key in required:
        # if key not in config:
            # fail(f"missing config key: {key}")

    # ok("config valid")


# def check_network():
    # result = subprocess.run(
        # ["ip", "addr", "show", "eth0"],
        # capture_output=True,
        # text=True,
        # timeout=5
    # )
    # if "inet " not in result.stdout:
        # fail("eth0 has no IPv4 address")
    # ok("network up")


# def check_vpn():
    # result = subprocess.run(
        # ["ip", "addr", "show", "tun0"],
        # capture_output=True,
        # text=True,
        # timeout=5
    # )
    # if result.returncode == 0:
        # ok("vpn tunnel present")
    # else:
        # print("[INIT][WARN] tun0 not present, continuing in degraded mode", flush=True)


# def check_modem():
    # devs = [Path("/dev/ttyUSB0"), Path("/dev/ttyUSB1"), Path("/dev/ttyUSB2"), Path("/dev/ttyUSB3")]
    # if not any(d.exists() for d in devs):
        # fail("no modem ttyUSB device found")
    # ok("modem serial device detected")


# def mark_ready():
    # READY_DIR.mkdir(parents=True, exist_ok=True)
    # READY_FILE.write_text("ready\n")
    # ok(f"created ready flag: {READY_FILE}")


# def main():
    # print("[INIT] starting system initialization", flush=True)

    # check_storage()
    # check_config()
    # check_network()

    # # give USB/modem a second to settle after boot
    # time.sleep(2)
    # check_modem()
    # check_vpn()

    # mark_ready()
    # print("[INIT] initialization complete", flush=True)


# if __name__ == "__main__":
    # main()
