from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import json

# Paths
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

# Shared data (updated by poller)
latest_data = {}

# Background task to update data
async def poll_device():
    while True:
        try:
            with open("/home/das/DAS-Communication-System/device/core/device_data_test.json", "r") as f:
                fresh = json.load(f)
            latest_data.update(fresh)
        except FileNotFoundError:
            latest_data["alert_message"] = "device_data.json not found"
        except json.JSONDecodeError:
            latest_data["alert_message"] = "device_data.json is invalid JSON"
        except Exception as e:
            latest_data["alert_message"] = f"Error: {e}"

        await asyncio.sleep(5)

# FastAPI lifespan (startup background task)
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_device())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

# Static + Templates
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# API endpoint (data)
@app.get("/api/status")
def get_status():
    return JSONResponse(latest_data)

# MAIN DASHBOARD ROUTE (THIS WAS YOUR BUG)
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request,                 # <-- REQUIRED (fixes your error)
        "dashboard.html",        # <-- template file
        {
            "data": latest_data,
            "bands": latest_data.get("bands", []),
            "logs": latest_data.get("logs", [])
        }
    )

# Get config
@app.get("/api/config")
def get_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return JSONResponse(json.load(f))
    except FileNotFoundError:
        return JSONResponse({"error": "config.json not found"}, status_code=404)

# Save config
@app.post("/api/config")
async def save_config(request: Request):
    try:
        new_config = await request.json()
        print("Received config:", new_config)

        with open(CONFIG_PATH, "w") as f:
            json.dump(new_config, f, indent=4)

        return JSONResponse({"status": "saved"})
    except Exception as e:
        print("Error saving config:", e)
        return JSONResponse({"error": str(e)}, status_code=500)
