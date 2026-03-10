# from fastapi import FastAPI, Request
# from fastapi.responses import HTMLResponse
# from fastapi.staticfiles import StaticFiles
# from fastapi.templating import Jinja2Templates
# from device.testing.GUItest.services.modem_service import get_modem_signal

# app = FastAPI()

# app.mount("/static", StaticFiles(directory="static"), name="static")
# templates = Jinja2Templates(directory="templates")


# @app.get("/", response_class=HTMLResponse)
# def home(request: Request):
#     return templates.TemplateResponse("index.html", {"request": request})


# @app.get("/settings", response_class=HTMLResponse)
# def settings_page(request: Request):
#     return templates.TemplateResponse("settings.html", {"request": request})


# @app.get("/api/signal")
# def get_signal():
#     return get_modem_signal()
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>DAS Test</title>
        <link rel="stylesheet" href="/static/css/style.css">
    </head>
    <body>

        <h1>DAS Dashboard</h1>
        <p id="msg">Loading...</p>

        <script src="/static/js/app.js"></script>

    </body>
    </html>
    """