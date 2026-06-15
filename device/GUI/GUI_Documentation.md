# GUI — Script & File Documentation

This document describes every file in the `device/GUI/` directory, what it does, and how the pieces fit together.

---

## Directory Overview

```
device/GUI/
├── __init__.py
├── config.json
├── main.py
├── templates/
│   ├── dashboard.html
│   └── login.html
├── static/
│   ├── css/   (AdminLTE stylesheet bundle)
│   └── js/    (AdminLTE JavaScript bundle)
└── vpn/
    └── client.ovpn
```

---

## `main.py`

The core of the web application. Built with **FastAPI** and served by **uvicorn**.

**Responsibilities:**

- **Application startup** — On launch, a background async task (`poll_device`) begins running in a loop every 5 seconds. It reads `device_data.json` from disk and stores the latest values in a shared in-memory dictionary (`latest_data`) that all routes can access.

- **Session-based authentication** — Login state is managed with a server-side in-memory session store (`_sessions`). When a user logs in successfully, a 32-byte random token is issued and stored in an HTTP-only cookie. All dashboard and API routes check this cookie before responding. Sessions are cleared if the server restarts, forcing re-login.

- **Password handling** — Passwords are never stored in plaintext. On login, the submitted password is hashed with SHA-256 and compared against the stored hash in `config.json`. When a new password is saved via the config API, it is hashed before being written to disk.

**Routes exposed:**

| Method | Path | Auth Required | Description |
|--------|------|---------------|-------------|
| `GET` | `/login` | No | Renders the login page |
| `POST` | `/login` | No | Validates credentials and issues a session cookie |
| `GET` | `/logout` | No | Clears the session and redirects to `/login` |
| `GET` | `/` | Yes | Renders the main dashboard page |
| `GET` | `/api/status` | Yes | Returns `device_data.json` contents as JSON |
| `GET` | `/api/config` | Yes | Returns `config.json` contents as JSON |
| `POST` | `/api/config` | Yes | Receives updated config from the browser and writes it to `config.json` |
| `POST` | `/api/upload-ovpn` | Yes | Accepts a `.ovpn` file upload and saves it to `device/GUI/vpn/client.ovpn` |
| `GET` | `/api/download-logs` | Yes | Zips all daily KPI JSON files in `data/kpi_data/` and streams the archive to the browser |

---

## `config.json`

The runtime configuration file for the entire system. It is **read by both the monitoring script and the GUI**, and **written exclusively by the GUI** via the `/api/config` route.

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `site_name` | string | Human-readable name for the deployment site |
| `device_id` | string | Unique identifier for this device |
| `monitored_bands` | list | LTE/NR bands to collect KPIs for (e.g. `["b4", "b12", "n41"]`) |
| `snmp_host` | string | IP address of the NMS that receives SNMP traps |
| `rssi_threshold_min` | number | Minimum acceptable averaged RSSI (dBm) |
| `rsrp_threshold_min` | number | Minimum acceptable averaged RSRP (dBm) |
| `rsrq_threshold_min` | number | Minimum acceptable averaged RSRQ (dB) |
| `sinr_threshold_min` | number | Minimum acceptable averaged SINR (dB) |
| `gui_username` | string | Login username for the web dashboard |
| `gui_password_hash` | string | SHA-256 hash of the dashboard login password |

Changes saved through the dashboard take effect for the monitoring script at the start of the next 5-session averaging window (up to ~7.5 minutes).

---

## `templates/dashboard.html`

The single-page web dashboard rendered by the `GET /` route. It is a **Jinja2 template** that receives initial data from the server on first load, then keeps itself up to date by polling `/api/status` every 5 seconds via JavaScript.

**Layout — two sections toggled by JavaScript:**

**Dashboard section (default view)**
- Status info boxes — Device Status, Modem Status, VPN Status, SNMP Status. Each box changes colour (green/yellow/red) based on the value received from the server.
- Band KPIs table — one row per monitored band showing RAT type, band number, PCI, EARFCN, and the 5-session averaged RSSI, RSRP, RSRQ, and SINR values.
- Cell Info panel — a summary card showing the current PCI, band, EARFCN, site name, and device ID.
- Download Logs button — triggers `GET /api/download-logs` to download all KPI logs as a `.zip`.

**Configuration section (opened via "Device Configuration" button)**
- Form fields for site name, device ID, SNMP host IP, username, and new password.
- Band checkboxes — selects which bands appear in the KPIs table (b2, b4, b5, b12, b13, b17, b66).
- KPI threshold inputs — minimum acceptable RSSI, RSRP, RSRQ, and SINR values.
- `.ovpn` file upload — sends the file to `/api/upload-ovpn`.
- Diagnostics panel — runs a set of client-side health checks (config file readable, SNMP IP valid, VPN tunnel active, `.ovpn` file present) and displays pass/warn/fail badges for each.
- Save button — POSTs all config fields to `/api/config`.

**Key JavaScript functions:**

| Function | What it does |
|----------|-------------|
| `refreshStatus()` | Polls `/api/status` every 5 s and updates all status boxes, the KPIs table, and the alert banner |
| `loadConfig()` | Fetches `/api/config` and populates all form fields when the config section is opened |
| `saveConfig()` | Validates inputs then POSTs the form data to `/api/config` |
| `uploadOvpnFile()` | Sends the selected `.ovpn` file to `/api/upload-ovpn` via a `FormData` POST |
| `runDiagnostics()` | Fetches config and status, runs health checks, and renders the diagnostics panel |
| `validateVpnIp()` | Live-validates the four SNMP IP octets as the user types, flagging all-zero and loopback addresses |
| `applyBandFilter()` | Hides/shows rows in the KPIs table based on which band checkboxes are ticked |
| `showSection(name)` | Switches the visible section between `dashboard` and `config` |
| `updateBandsTable(bands)` | Rebuilds the KPIs table body from the latest status poll data |

---

## `templates/login.html`

A standalone HTML login page (no Jinja2 logic beyond optional error message rendering). It is served at `GET /login` and submits credentials via an HTML `POST` form to `/login`.

**Features:**
- Username and password fields with a show/hide password toggle button.
- Displays an inline error message block (red banner) when the server returns an `error` variable — this happens when credentials are incorrect.
- Minimal, self-contained CSS — no dependency on AdminLTE or external assets beyond Font Awesome and Google Fonts, so it loads reliably even before the full static bundle is available.

---

## `static/`

Pre-built frontend assets from the **AdminLTE 3** admin template. These files are served directly by FastAPI at the `/static` URL path and are not modified by this project.

| Path | Contents |
|------|----------|
| `static/css/adminlte.min.css` | Minified AdminLTE stylesheet (used by the dashboard) |
| `static/css/adminlte.css` | Unminified version (for reference/debugging) |
| `static/js/adminlte.min.js` | Minified AdminLTE JavaScript (sidebar, widgets, etc.) |
| `static/js/adminlte.js` | Unminified version |
| `static/img/*.html` | AdminLTE component previews (not used at runtime) |

The dashboard loads the `.min` variants in production for faster page load. Font Awesome icons and Bootstrap are loaded from CDN links in the template head.

---

## `vpn/client.ovpn`

The OpenVPN client configuration file used by the device to establish the VPN tunnel to the NMS. This file is **uploaded by the operator** via the web dashboard and saved here by the `/api/upload-ovpn` route.

- Only `.ovpn` files are accepted; the server rejects any other file extension.
- The monitoring script references this file when restarting OpenVPN after a tunnel drop.
- If this file is absent, the VPN cannot start and the diagnostics panel will flag it as an error.

---

## `__init__.py`

Empty file. Marks `device/GUI/` as a Python package so that it can be imported by other modules in the project if needed.

---

## How the Files Work Together

```
Browser
  │
  ├─ GET /           → main.py renders dashboard.html with initial data from latest_data
  ├─ GET /login      → main.py renders login.html
  ├─ POST /login     → main.py validates against config.json, sets session cookie
  │
  │  (every 5 seconds, browser JS calls:)
  ├─ GET /api/status → main.py returns latest_data (kept fresh by poll_device background task)
  │
  │  (when user opens config panel:)
  ├─ GET /api/config → main.py reads and returns config.json
  ├─ POST /api/config → main.py writes updated config.json
  ├─ POST /api/upload-ovpn → main.py saves uploaded file to vpn/client.ovpn
  └─ GET /api/download-logs → main.py zips kpi_data/*.json and streams to browser

Background (server side):
  poll_device()  →  reads device_data.json every 5 s  →  updates latest_data in memory
```
