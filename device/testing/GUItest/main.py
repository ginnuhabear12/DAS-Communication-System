#eventually downloard requirements.txt so every device has that file
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    data = {
        "site_name": "Hospital East Wing",
        "device_id": "DAS-EMD-001",
        "last_update": "2026-03-12 19:10:00",
        "device_status": "ONLINE",
        "modem_status": "CONNECTED",
        "vpn_status": "ACTIVE",
        "snmp_status": "RUNNING",
        "rssi": "-70 dBm",
        "rsrp": "-95 dBm",
        "rsrq": "-10 dB",
        "sinr": "18 dB",
        "pci": "145",
        "band": "n41",
        "earfcn": "5230",
        "cell_id": "0x31A4",
        "alert_message": "No active alarms"
    }

    logs = [
        {"time": "19:01", "rsrp": "-94 dBm", "sinr": "19 dB", "status": "Normal"},
        {"time": "19:02", "rsrp": "-95 dBm", "sinr": "18 dB", "status": "Normal"},
        {"time": "19:03", "rsrp": "-101 dBm", "sinr": "12 dB", "status": "Warning"},
    ]

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "data": data, "logs": logs}
    )

