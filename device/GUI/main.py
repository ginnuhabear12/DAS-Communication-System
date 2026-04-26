import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from fastapi import File, UploadFile
import shutil

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import zipfile
import io
from fastapi.responses import StreamingResponse

# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ----------------------------
# Paths
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
DEVICE_DATA_PATH = Path("/home/das/DAS-Communication-System/data/device_data.json")

CONFIG_DIR = Path("/home/das/DAS-Communication-System/device/GUI")
CONFIG_PATH = CONFIG_DIR / "config.json"

OVPN_DIR = CONFIG_DIR / "vpn"
OVPN_PATH = OVPN_DIR / "client.ovpn"

KPI_DATA_DIR = Path("/home/das/DAS-Communication-System/data/kpi_data")

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
OVPN_DIR.mkdir(parents=True, exist_ok=True)



# ----------------------------
# Shared data
# ----------------------------
latest_data = {}


# ----------------------------
# Background task
# ----------------------------
async def poll_device():
    while True:
        try:
            with open(DEVICE_DATA_PATH, "r") as f:
                fresh = json.load(f)

            latest_data.update(fresh)

        except FileNotFoundError:
            latest_data["alert_message"] = "device_data.json not found"

        except json.JSONDecodeError:
            latest_data["alert_message"] = "device_data.json is invalid JSON"

        except Exception as e:
            latest_data["alert_message"] = f"Error: {e}"

        await asyncio.sleep(5)


# ----------------------------
# FastAPI lifespan
# ----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_device())
    yield
    task.cancel()


# ----------------------------
# App setup
# ----------------------------
app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ----------------------------
# Dashboard route
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "data": latest_data,
            "bands": latest_data.get("bands", []),
            "logs": latest_data.get("logs", []),
        },
    )

# ----------------------------
# Download zip of Logs API
# ----------------------------
@app.get("/api/download-logs")
def download_logs():
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if KPI_DATA_DIR.exists():
            for file in KPI_DATA_DIR.glob("*.json"):
                zf.write(file, arcname=file.name)
    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=das_logs.zip"}
    )

# ----------------------------
# Status API
# ----------------------------
@app.get("/api/status")
def get_status():
    return JSONResponse(latest_data)


# ----------------------------
# Config API
# ----------------------------
@app.get("/api/config")
def get_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return JSONResponse(json.load(f))

    except FileNotFoundError:
        return JSONResponse(
            {
                "site_name": "",
                "device_id": "",
                # "poll_interval": 30,
                "snmp_host": "0.0.0.0",
                "rat": "",
                "earfcn": "",
                "nr_band": "",
                "nr_arfcn": "",
                "monitored_bands": [],
            }
        )


@app.post("/api/config")
async def save_config(request: Request):
    try:
        new_config = await request.json()

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        with open(CONFIG_PATH, "w") as f:
            json.dump(new_config, f, indent=4)

        print(f"{_ts()} Config saved to: {CONFIG_PATH}")

        return JSONResponse({"status": "saved", "saved_to": str(CONFIG_PATH)})

    except Exception as e:
        print(f"{_ts()} Error saving config: {e}")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ----------------------------
# OVPN Upload API
# ----------------------------
@app.post("/api/upload-ovpn")
async def upload_ovpn(ovpn_file: UploadFile = File(...)):
    try:
        if not ovpn_file.filename.lower().endswith(".ovpn"):
            return JSONResponse({"status": "error", "message": "Only .ovpn files allowed"}, status_code=400)

        OVPN_DIR.mkdir(parents=True, exist_ok=True)

        with OVPN_PATH.open("wb") as buffer:
            shutil.copyfileobj(ovpn_file.file, buffer)

        print(f"{_ts()} OVPN saved to: {OVPN_PATH}")

        return JSONResponse({
            "status": "uploaded",
            "saved_to": str(OVPN_PATH)
        })

    except Exception as e:
        print(f"{_ts()} OVPN upload error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)



# import os

# from fastapi import FastAPI, Request
# from fastapi.responses import HTMLResponse, JSONResponse
# from fastapi.staticfiles import StaticFiles
# from fastapi.templating import Jinja2Templates
# from fastapi import UploadFile, File
# from pathlib import Path
# import shutil
# from contextlib import asynccontextmanager
# from pathlib import Path
# import asyncio
# import json

# # Paths
# BASE_DIR = Path(__file__).resolve().parent
# CONFIG_PATH = CONFIG_PATH = Path.home() / "config.json"
# # Shared data (updated by poller)
# latest_data = {}



# # Background task to update data
# async def poll_device():
#     while True:
#         try:
#             with open("/home/das/DAS-Communication-System/data/device_data.json", "r") as f:
#                 fresh = json.load(f)
#             latest_data.update(fresh)
#         except FileNotFoundError:
#             latest_data["alert_message"] = "device_data.json not found"
#         except json.JSONDecodeError:
#             latest_data["alert_message"] = "device_data.json is invalid JSON"
#         except Exception as e:
#             latest_data["alert_message"] = f"Error: {e}"

#         await asyncio.sleep(5)

# # FastAPI lifespan (startup background task)
# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     task = asyncio.create_task(poll_device())
#     yield
#     task.cancel()

# app = FastAPI(lifespan=lifespan)

# # Static + Templates
# app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# templates = Jinja2Templates(directory=BASE_DIR / "templates")

# # API endpoint (data)
# @app.get("/api/status")
# def get_status():
#     return JSONResponse(latest_data)

# # MAIN DASHBOARD ROUTE (THIS WAS YOUR BUG)
# @app.get("/", response_class=HTMLResponse)
# def dashboard(request: Request):
#     return templates.TemplateResponse(
#         request,                 # <-- REQUIRED (fixes your error)
#         "dashboard.html",        # <-- template file
#         {
#             "data": latest_data,
#             "bands": latest_data.get("bands", []),
#             "logs": latest_data.get("logs", [])
#         }
#     )

# # Get config
# @app.get("/api/config")
# def get_config():
#     try:
#         with open(CONFIG_PATH, "r") as f:
#             return JSONResponse(json.load(f))
#     except FileNotFoundError:
#         return JSONResponse({"error": "config.json not found"}, status_code=404)

# # Save config
# @app.post("/api/config")
# async def save_config(request: Request):
#     try:
#         new_config = await request.json()
#         print("Received config:", new_config)

#         with open(CONFIG_PATH, "w") as f:
#             json.dump(new_config, f, indent=4)

#         return JSONResponse({"status": "saved"})
#     except Exception as e:
#         print("Error saving config:", e)
#         return JSONResponse({"error": str(e)}, status_code=500)
