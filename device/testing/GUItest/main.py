from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio

BASE_DIR = Path(__file__).resolve().parent

# Shared in-memory cache — update this dict from your poller
latest_data = {
    "device_status": "UNKNOWN",
    "rsrp": "N/A",
    # ... all your fields
}

async def poll_device():
    """Background loop — replace the body with your real data source."""
    while True:
        try:
            # Example: SSH into modem, call SNMP, hit a local API, etc.
            # result = await fetch_modem_stats()
            latest_data.update({
                "device_status": "ONLINE",
                "rsrp": "-95 dBm",
                "last_update": "...",
            })
        except Exception as e:
            latest_data["device_status"] = f"ERROR: {e}"
        await asyncio.sleep(30)  # poll every 30 seconds

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_device())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

@app.get("/api/status")
def get_status():
    return JSONResponse(latest_data)

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "data": latest_data}
    )