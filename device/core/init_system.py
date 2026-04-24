#!/usr/bin/env python3

from pathlib import Path
import json
import subprocess
import sys
import time

READY_DIR = Path("/run/das")
READY_FILE = READY_DIR / "init.ready"
CONFIG_PATH = Path("/home/das/DAS-Communication-System/device/GUI/config.json")
DATA_DIR = Path("/home/das/DAS-Communication-System/data")
LOG_DIR = Path("/home/das/DAS-Communication-System/logs")


def fail(msg: str, code: int = 1):
    print(f"[INIT][FAIL] {msg}", flush=True)
    if READY_FILE.exists():
        READY_FILE.unlink()
    sys.exit(code)


def ok(msg: str):
    print(f"[INIT][OK] {msg}", flush=True)


def check_storage():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    test_file = DATA_DIR / ".write_test"
    test_file.write_text("ok")
    test_file.unlink()

    ok("storage writable")


def check_config():
    if not CONFIG_PATH.exists():
        fail(f"missing config file: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    required = [
        "site_name",
        "device_id",
        "poll_interval",
        "snmp_host",
    ]

    for key in required:
        if key not in config:
            fail(f"missing config key: {key}")

    rat = str(config.get("rat", "LTE")).upper()

    # DO NOT hard fail on LTE/5G params — just warn
    if rat == "LTE":
        if "earfcn" not in config:
            print("[INIT][WARN] earfcn missing for LTE mode", flush=True)
    elif rat == "5G":
        if "nr_band" not in config:
            print("[INIT][WARN] nr_band missing for 5G mode", flush=True)
        if "nr_arfcn" not in config:
            print("[INIT][WARN] nr_arfcn missing for 5G mode", flush=True)
    else:
        print(f"[INIT][WARN] invalid rat value: {rat}", flush=True)

    ok("config valid")


def check_network():
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
    result = subprocess.run(
        ["ip", "addr", "show", "tun0"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    if result.returncode == 0:
        ok("vpn tunnel present")
    else:
        print("[INIT][WARN] tun0 not present, continuing in degraded mode", flush=True)


def check_modem():
    devs = [
        Path("/dev/ttyUSB0"),
        Path("/dev/ttyUSB1"),
        Path("/dev/ttyUSB2"),
        Path("/dev/ttyUSB3"),
    ]

    if not any(d.exists() for d in devs):
        fail("no modem ttyUSB device found")

    ok("modem serial device detected")


def mark_ready():
    READY_DIR.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text("ready\n")
    ok(f"created ready flag: {READY_FILE}")


def main():
    print("[INIT] starting system initialization", flush=True)

    check_storage()
    check_config()
    check_network()

    time.sleep(2)
    check_modem()
    check_vpn()

    mark_ready()
    print("[INIT] initialization complete", flush=True)


if __name__ == "__main__":
    main()




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
