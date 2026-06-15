# DAS Communication System

A Python-based edge device system for monitoring Distributed Antenna System (DAS) signal quality over LTE and 5G NR networks. The system collects RF Key Performance Indicators (KPIs) from a Quectel modem via AT commands, averages them over a rolling 5-minute window, sends SNMP traps to a Network Management Server (NMS) when thresholds are violated, and exposes a local web dashboard for configuration and live monitoring.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Hardware](#hardware)
  - [Package Contents](#package-contents)
  - [Hardware Description](#hardware-description)
- [Installation](#installation)
  - [Hardware Assembly](#hardware-assembly)
  - [Software Installation](#software-installation)
- [Configuration](#configuration)
- [Running the System](#running-the-system)
- [Systemd Services](#systemd-services)
- [KPI Collection](#kpi-collection)
- [Alarm System](#alarm-system)
- [Web Dashboard (GUI)](#web-dashboard-gui)
- [Data Storage](#data-storage)
- [VPN](#vpn)
- [SNMP OID Reference](#snmp-oid-reference)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Overview

The DAS Communication System runs on a Raspberry Pi (or similar Linux SBC) connected to a Quectel cellular modem via USB serial. It is designed to operate as an unattended field device that:

1. Collects LTE and/or NR5G signal metrics (RSRP, RSRQ, RSSI, SINR) from the modem every 90 seconds.
2. Averages five consecutive samples (a 5-session window covering ~7.5 minutes) per configured band.
3. Evaluates the averaged values against operator-configured thresholds.
4. Fires SNMPv2c traps to an NMS when a KPI is invalid or below threshold.
5. Persists daily KPI logs to local storage with a 7-day rolling retention policy.
6. Provides a secured web UI for live status, configuration changes, VPN file upload, and log downloads.

---

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                    Raspberry Pi                        │
│                                                        │
│  ┌──────────────┐   AT commands   ┌─────────────────┐  │
│  │  full_script │ ──────────────► │  Quectel Modem  │  │
│  │  (monitor)   │ ◄────────────── │  (/dev/ttyUSB2) │  │
│  └──────┬───────┘   KPI data      └─────────────────┘  │
│         │                                               │
│  ┌──────▼───────┐   5-session avg  ┌──────────────────┐ │
│  │  alarms.py   │ ────────────────► │  file_manager   │ │
│  │  (threshold) │                  │  device_data.json│ │
│  └──────┬───────┘                  │  kpi_YYYYMMDD   │ │
│         │                          └──────────────────┘ │
│  ┌──────▼───────┐                                       │
│  │  snmpSend.py │ ──────► SNMP Traps ──► NMS (10.8.0.1) │
│  └──────────────┘                                       │
│                                                         │
│  ┌──────────────┐                                       │
│  │  GUI (FastAPI│ ◄── Browser (port 8000)               │
│  │  + Jinja2)   │                                       │
│  └──────────────┘                                       │
│                                                         │
│  ┌──────────────┐                                       │
│  │  OpenVPN     │ ──────► VPN Tunnel (tun0)             │
│  └──────────────┘                                       │
└────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
DAS-Communication-System/
├── data/
│   ├── device_data.json          # Live status file read by the GUI
│   └── kpi_data/                 # Daily KPI JSON logs (rolling 7-day)
│       └── YYYYMMDD_kpi.json
│
├── device/
│   ├── core/
│   │   ├── full_script.py        # Main collection loop (entry point)
│   │   ├── init_system.py        # Startup health checks
│   │   ├── modem.py              # AT command serial interface
│   │   ├── alarms.py             # 5-session window processing & alarm logic
│   │   ├── snmpSend.py           # SNMPv2c trap sender
│   │   ├── file_manager.py       # JSON persistence & retention cleanup
│   │   ├── models.py             # Dataclass definitions (KPIs, sessions, etc.)
│   │   ├── constants.py          # System-wide constants and AT command strings
│   │   ├── standin_kpi_collection.py  # KPI collection per band (AT+QENG)
│   │   ├── snmpTestCode.py       # SNMP test utilities
│   │   ├── requirements.txt      # Python dependencies
│   │   └── SNMP/
│   │       └── snmpReceiver.py   # Test SNMP trap receiver
│   │
│   ├── GUI/
│   │   ├── main.py               # FastAPI web application
│   │   ├── config.json           # Runtime configuration (site, bands, thresholds)
│   │   ├── templates/
│   │   │   ├── dashboard.html    # Main dashboard page
│   │   │   └── login.html        # Login page
│   │   ├── static/               # AdminLTE CSS/JS assets
│   │   └── vpn/
│   │       └── client.ovpn       # OpenVPN client config (uploaded via GUI)
│   │
│   ├── data/                     # Device-local copy of KPI data
│   └── etc/systemd/system/
│       ├── das-init.service      # Initialization service
│       ├── das-monitor.service   # KPI monitoring service
│       └── das-gui.service       # Web GUI service
│
├── docs/
│   └── README.md
│
├── logs/
│   └── today.log
│
└── testing/
    ├── SNMPTestReceiver.py
    ├── SNMPTestSender.py
    ├── modemTesting.py
    ├── modemFailuretests.py
    └── ...
```

---

## Requirements

### Python Version

Python 3.12 or later (uses `float | None` union syntax and `list[Type]` generics).

### Python Dependencies

Install from `device/core/requirements.txt`:

```bash
pip install -r device/core/requirements.txt
```

Key packages include:

- `fastapi` + `uvicorn` — web dashboard
- `pyserial` — AT command communication with modem
- `pysnmp-lextudio` — SNMPv2c trap sending
- `pydantic` — data validation

---

## Hardware

### Package Contents

Verify the following items are included before beginning assembly:

| Item | Quantity |
|------|----------|
| Raspberry Pi 5 | 1 |
| Waveshare PoE HAT (G) | 1 |
| Raspberry Pi Active Cooler | 1 |
| Waveshare PCIe TO 4G/5G M.2 USB3.2 HAT+ | 1 |
| Quectel RM520N-GL Modem | 1 |
| Siretta ASMGA010XB113S11 | 2 |
| Antenna Siretta ECHO 47 | 2 |
| SanDisk MicroSD Max Endurance | 1 |
| Weather Proof Box | 1 |
| PoE Injector | 1 |
| Ethernet Cable | 2 |
| PCIe Cable | 1 |

### Hardware Description

#### Front Panel

| Component | Function |
|-----------|----------|
| Ethernet Port | PoE Power and Network |
| Antenna Port A | Cellular Reception |
| Antenna Port B | Cellular Reception |

#### Internal Components

| Component | Purpose |
|-----------|---------|
| Raspberry Pi 5 | Main computer that runs the monitoring software, collects data, and controls the system. |
| Waveshare PoE HAT (G) | Provides power and network connectivity through a single Ethernet cable. |
| Raspberry Pi Active Cooler | Fan and heatsink used to keep the Raspberry Pi cool during operation. |
| Waveshare PCIe TO 4G/5G M.2 USB3.2 HAT+ | Interface board that connects the 5G modem to the Raspberry Pi. |
| Quectel RM520N-GL Modem | 5G/LTE modem used to measure cellular signal strength and quality. |
| Siretta ASMGA010XB113S11 | External antenna that receives cellular signals for the modem. |
| Antenna Siretta ECHO 47 | Wideband LTE/5G antenna designed to receive signals across multiple cellular frequency bands. |
| SanDisk MicroSD Max Endurance | Stores the operating system, software, configuration files, and monitoring data. |

> The modem must enumerate on `/dev/ttyUSB2` (the AT command port). The system also checks `/dev/ttyUSB0`–`/dev/ttyUSB3` for modem presence during initialization.

---

## Installation

### Hardware Assembly

**Required components:** Raspberry Pi 5 × 1, Raspberry Pi Active Cooler, Waveshare PoE HAT (G), Waveshare PCIe TO 4G/5G M.2 USB3.2 HAT+, Quectel RM520N-GL Modem, PCIe Cable, antenna cables, antennas, PoE injector.

1. Place the Raspberry Pi 5 Active Cooler onto the Raspberry Pi 5 and connect the fan wires to the FAN interface. Do not remove the thermal pads.
2. Add the GPIO extension header.
3. Using the hardware from the PoE HAT package, screw the 4 long mounts (with 4 short screws) onto the top side of the Raspberry Pi 5.
4. Place the PoE HAT onto the GPIO and PoE interfaces of the Raspberry Pi 5.
5. Screw the 4 mount-with-screw standoffs onto the previous mounts to extend the stack.
6. Connect the PCIe cable to the PCIe interface on the Raspberry Pi 5.
7. Place the 4G/5G HAT onto the standoffs and screw it securely in place.
8. Connect the other end of the PCIe cable to the PCIe interface on the 5G HAT.
9. Insert the Quectel RM520N-GL modem into the HAT's M.2 interface and screw it securely onto the HAT.
10. Connect the antenna cables to the **ANT0** and **ANT1** interfaces on the modem.
11. Secure the antennas to the other ends of the antenna cables.
12. Connect an **IEEE 802.3at rated PoE injector** to the Raspberry Pi 5's Ethernet interface to supply power and network connectivity.

### Software Installation

#### 1. Clone the repository

```bash
git clone https://github.com/ginnuhabear12/DAS-Communication-System.git /home/das/DAS-Communication-System
cd /home/das/DAS-Communication-System
```

#### 2. Create a Python virtual environment (optional but recommended)

```bash
python3 -m venv device/core/.venv
source device/core/.venv/bin/activate
pip install -r device/core/requirements.txt
```

#### 3. Create required directories

```bash
mkdir -p data/kpi_data logs /run/das
```

#### 4. Install systemd services

```bash
sudo cp device/etc/systemd/system/das-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable das-init.service das-monitor.service das-gui.service
```

---

## Configuration

All runtime settings are stored in `device/GUI/config.json`. This file is written by the web dashboard and read by the monitoring script.

### Configuration Fields

| Field | Type | Description |
|-------|------|-------------|
| `site_name` | string | Human-readable site identifier |
| `device_id` | string | Unique device identifier |
| `snmp_host` | string | IP address of the NMS (SNMP trap destination) |
| `monitored_bands` | list | Bands to collect, e.g. `["b2", "b4", "n41"]` |
| `rsrp_threshold_min` | number | Minimum acceptable averaged RSRP (dBm) |
| `rsrq_threshold_min` | number | Minimum acceptable averaged RSRQ (dB) |
| `rssi_threshold_min` | number | Minimum acceptable averaged RSSI (dBm) |
| `sinr_threshold_min` | number | Minimum acceptable averaged SINR (dB) |
| `gui_username` | string | Web UI login username |
| `gui_password_hash` | string | SHA-256 hash of the web UI password |

#### Band Naming Convention

- LTE bands are prefixed with `b` — e.g. `"b2"`, `"b4"`, `"b12"`, `"b66"`
- NR5G bands are prefixed with `n` — e.g. `"n41"`, `"n77"`, `"n260"`

#### Example `config.json`

```json
{
    "site_name": "Site A",
    "device_id": "DAS-001",
    "monitored_bands": ["b4", "b12", "n41"],
    "snmp_host": "10.8.0.1",
    "rssi_threshold_min": -90,
    "rsrp_threshold_min": -110,
    "rsrq_threshold_min": -14,
    "sinr_threshold_min": 0,
    "gui_username": "admin"
}
```

The monitoring script waits at startup until all required fields in `config.json` are populated. You can set them via the web dashboard before or after starting the services.

---

## Running the System

### Using systemd (recommended for production)

```bash
sudo systemctl start das-init.service
sudo systemctl start das-monitor.service
sudo systemctl start das-gui.service
```

View logs:

```bash
journalctl -u das-monitor.service -f
journalctl -u das-gui.service -f
```

### Running manually (development)

Start the GUI:

```bash
cd device/GUI
uvicorn main:app --host 0.0.0.0 --port 8000
```

Start the monitoring loop:

```bash
cd device/core
python3 full_script.py
```

Run initialization checks:

```bash
python3 device/core/init_system.py
```

---

## Systemd Services

Three services manage the system lifecycle:

### `das-init.service`

Runs once at boot as a `oneshot` service. Performs the following checks before marking the system ready:

- Storage directories exist and are writable
- `config.json` is present and contains required fields
- `eth0` has an IPv4 address
- A modem device is present on `/dev/ttyUSB*`
- VPN tunnel (`tun0`) is present (degraded mode if absent)

On success, writes a ready flag to `/run/das/init.ready`.

### `das-monitor.service`

The main KPI collection daemon. Requires `das-init.service` to have succeeded (checks for `/run/das/init.ready` before starting). Restarts automatically on failure with a 5-second delay.

### `das-gui.service`

Runs the FastAPI web dashboard on port 8000 via uvicorn. Starts after the network is available. Restarts automatically on failure.

---

## KPI Collection

### Collection Cycle

The monitoring loop runs sessions on a fixed 90-second interval (`SAMPLE_INTERVAL_SECONDS`). Each session collects one sample per configured band. After every 5 sessions, the window is processed and averaged results are written to disk.

```
Session 1 ──┐
Session 2   ├─ Every 90s ──► 5-session window ──► Average & alarm check
Session 3   │                                     ──► Write to disk
Session 4   │
Session 5 ──┘
```

### AT Commands Used

| Command | Purpose |
|---------|---------|
| `AT+QENG="servingcell"` | Primary cell KPI data |
| `AT+QENG="neighbourcell"` | Neighbor cell data |
| `AT+CFUN=1` | Full modem reset at startup |
| `AT+COPS=0` | Auto network registration |
| `AT+COPS=2` | Deregister (used before band switching) |
| `AT+QNWPREFCFG="mode_pref",AUTO` | Set LTE + NR5G dual mode |
| `AT+QNWPREFCFG="nr5g_band",<n>` | Configure NR5G band |
| `AT+QNWPREFCFG="lte_band",<n>` | Configure LTE band |

### KPI Fields

**LTE (`LTEKPI`)**

| Field | Unit | Description |
|-------|------|-------------|
| `rsrp` | dBm | Reference Signal Received Power |
| `rsrq` | dB | Reference Signal Received Quality |
| `rssi` | dBm | Received Signal Strength Indicator |
| `sinr` | dB | Signal-to-Interference-plus-Noise Ratio |
| `earfcn` | — | LTE frequency channel number |
| `pci` | — | Physical Cell ID |

**NR5G (`NR5GKPI`)**

| Field | Unit | Description |
|-------|------|-------------|
| `ss_rsrp` | dBm | Synchronization Signal RSRP |
| `ss_rsrq` | dB | Synchronization Signal RSRQ |
| `ss_sinr` | dB | Synchronization Signal SINR |
| `arfcn` | — | NR absolute frequency channel number |
| `pci` | — | Physical Cell ID |

Any value above `500` (the `INVALID_SENTINEL`) is treated as an invalid/unavailable reading.

### Fault Tolerance

- **Dummy sessions**: If KPI collection fails entirely for a session, a dummy session with all values set to `9999` is inserted to maintain the 5-session window structure and trigger invalid alarms downstream.
- **Consecutive failure restart**: If all bands fail via AT command error for 2 consecutive sessions, or all bands fail via `SerialException` for 2 consecutive sessions, the modem is restarted automatically and an SNMP runtime alarm is sent.
- **SIM detection**: The system checks for SIM presence at startup and before every session, enabling automatic recovery when a SIM is inserted after boot.

---

## Alarm System

Alarms are sent as SNMPv2c traps to the configured NMS. Three trap types are defined:

### Invalid KPI Alarm

Fires when the last 3 of 5 samples for a KPI are above the invalid sentinel (500). Indicates the modem could not report a valid value for that metric on that band.

### Threshold Alarm

Fires when the 5-session average of a KPI falls below the operator-configured minimum threshold. Indicates degraded RF signal quality.

### Runtime Alarm

Fires for system-level failures with no specific band or KPI context — modem command failures, VPN disconnection, file write errors, SIM absence, serial port loss, etc.

---

## Web Dashboard (GUI)

Access the dashboard at `http://<device-ip>:8000` after starting `das-gui.service`.

### Login

Default credentials: username `admin`, password `admin`. Change the password via the settings page on first use. Credentials are stored as a SHA-256 hash in `config.json`.

### Features

- **Live status panel** — device status, modem status, VPN status, SNMP status, last update time
- **Per-band KPI display** — averaged RSRP, RSRQ, RSSI/SS-RSRP/SS-RSRQ, SINR for all monitored bands
- **Configuration page** — set site name, device ID, SNMP host, monitored bands, KPI thresholds, and GUI credentials
- **VPN upload** — upload a `.ovpn` client config file via the browser
- **Log download** — download all daily KPI JSON files as a `.zip` archive
- **Auto-refresh** — device data is polled every 5 seconds

---

## Data Storage

### Live Status File

`device/data/device_data.json` — overwritten every 5-minute averaging window. Contains current device status, last update timestamp, VPN/SNMP status, and per-band averaged KPI values.

### Daily KPI Log Files

`device/data/kpi_data/YYYYMMDD_kpi.json` — one file per calendar day, appended every 5-minute window. Contains a list of timestamped entries, each with all band averages for that window.

### Retention

Daily files older than 7 days are automatically deleted after each successful write cycle.

---

## VPN

The system uses OpenVPN to maintain connectivity to the NMS. The `.ovpn` client config file is stored at `device/GUI/vpn/client.ovpn` and can be uploaded via the web dashboard.

VPN status is checked after every collection session. If `tun0` is down, the system:

1. Updates `vpn_status` to `"DOWN"` in the device data file
2. Sends a runtime SNMP alarm
3. Attempts to restart OpenVPN automatically
4. Checks again after 5 seconds and updates the status accordingly

---

## SNMP OID Reference

Enterprise root OID: `1.3.6.1.4.1.12345`

> Replace `12345` with your assigned Private Enterprise Number (PEN) for production deployment.

### Trap Type OIDs

| OID | Name | Description |
|-----|------|-------------|
| `1.3.6.1.4.1.12345.1.1` | `trapInvalidKPI` | KPI has too many invalid samples |
| `1.3.6.1.4.1.12345.1.2` | `trapThresholdKPI` | KPI average is below threshold |
| `1.3.6.1.4.1.12345.1.3` | `trapRuntime` | System or modem runtime failure |

### Varbind OIDs

| OID | Name | Type | Used In |
|-----|------|------|---------|
| `1.3.6.1.4.1.12345.2.1.0` | `band` | Integer32 | KPI traps |
| `1.3.6.1.4.1.12345.2.2.0` | `kpi` | OctetString | KPI traps |
| `1.3.6.1.4.1.12345.2.3.0` | `alarmType` | OctetString | KPI traps |
| `1.3.6.1.4.1.12345.2.4.0` | `detail` | OctetString | All traps |
| `1.3.6.1.4.1.12345.2.5.0` | `component` | OctetString | Runtime traps |

> **Production note:** The default NMS port is set to `1162` for testing. Change `NMS_PORT` in `snmpSend.py` to `162` for production.

---

## Testing

Test scripts are located in the `testing/` directory:

| Script | Purpose |
|--------|---------|
| `SNMPTestReceiver.py` | Listens for SNMP traps on port 1162 for verification |
| `SNMPTestSender.py` | Sends a test SNMP trap |
| `modemTesting.py` | Interactive modem AT command testing |
| `modemFailuretests.py` | Simulates modem failure scenarios |
| `atCommandExample.py` | Basic AT command usage example |
| `qscanParse.py` | Parses AT+QSCAN output |
| `testalarms.py` | Tests alarm processing logic |

Run SNMP receiver in one terminal and trigger a test trap from another:

```bash
python3 testing/SNMPTestReceiver.py   # Terminal 1
python3 testing/SNMPTestSender.py     # Terminal 2
```

---

## Troubleshooting

### Modem not detected

Check that the device appears at `/dev/ttyUSB*`:

```bash
ls /dev/ttyUSB*
dmesg | grep ttyUSB
```

Ensure the `das` user has permission to access the serial port:

```bash
sudo usermod -aG dialout das
```

### `das-monitor` won't start

The monitor service requires `das-init.service` to have completed successfully and left `/run/das/init.ready`. Check the init service log:

```bash
journalctl -u das-init.service
```

### No SNMP traps received at NMS

- Confirm `snmp_host` in `config.json` is correct.
- Verify VPN tunnel is active (`ip addr show tun0`).
- Check NMS port — default is `1162` (test), production should be `162`.
- Run `testing/SNMPTestReceiver.py` locally to confirm trap sending works.

### GUI not loading

Ensure port 8000 is reachable and `das-gui.service` is running:

```bash
sudo systemctl status das-gui.service
curl http://localhost:8000/
```

### Config changes not taking effect

Band and threshold changes made in the GUI take effect at the start of the next 5-session averaging window (up to ~7.5 minutes after saving). The config is reloaded automatically after each completed window.
