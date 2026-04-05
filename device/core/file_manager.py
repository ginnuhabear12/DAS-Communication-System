"""
file_manager.py — KPI File Management for DAS Communication System
------------------------------------------------------------------
Handles two writes per 5-minute window:
    1. Overwrite device_data.json  → updates the live GUI
    2. Append to kpi_YYYY-MM-DD.json → builds the daily KPI log on microSD
    3. Cleanup → enforces 7-day rolling retention
"""

import json
import os
import glob
from datetime import date
from models import AveragedLTEKPI, AveragedNR5GKPI

# ── File Paths ────────────────────────────────────────────────────────────────
# Directory where daily KPI files are stored on the microSD
KPI_DIR = "/home/pi/kpi_data"

# Path to the JSON file that feeds the live GUI
GUI_JSON_PATH = "/home/pi/GUItest/device_data.json"

# Maximum number of daily KPI files to keep before deleting the oldest
MAX_DAYS = 7