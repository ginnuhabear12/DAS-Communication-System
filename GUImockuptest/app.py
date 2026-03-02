from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import random
import time

app = FastAPI(title="Edge Monitoring Device GUI Mockup")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/api/kpi", response_class=JSONResponse)
def api_kpi():
    now = int(time.time())
    return {
        "ts": now,
        "rsrp": random.randint(-120, -75),
        "rsrq": random.randint(-20, -3),
        "sinr": random.randint(-5, 30),
        "rssi": random.randint(-105, -55),
    }
