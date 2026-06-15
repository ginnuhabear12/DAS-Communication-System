"""
Module: constants.py
Purpose: Define system-wide limits, thresholds, identities, and protocol behavior.
         These values must stay consistent across deployments unless explicitly reconfigured.
"""

from pathlib import Path

# Path to the GUI configuration file used to load user-defined settings at runtime
CONFIG_PATH = Path("/home/das/DAS-Communication-System/device/GUI/config.json")

#Sampling Behavior Constants
SAMPLES_PER_SESSION = 5          # Number of KPI samples to collect per band per session (temporary — adjust as needed)
SAMPLE_INTERVAL_SECONDS = 90     # Time in seconds between individual samples within a session
AVERAGING_WINDOW_SECONDS = 300   # Rolling window (5 minutes) over which samples are averaged for reporting
BANDS = []                       # List of active bands to scan; populated at runtime from config


#RF Valid Ranges
# Defines the physically valid measurement range for each KPI metric.
# Readings outside these bounds are considered malformed or hardware errors and are discarded.
MIN_VALID_RSRP = -150
MAX_VALID_RSRP = -30

MIN_VALID_RSRQ = -30
MAX_VALID_RSRQ = 0

MIN_VALID_SINR = -20
MAX_VALID_SINR = 40

MIN_VALID_RSSI = -150
MAX_VALID_RSSI = -30


#Default Thresholds
# Fallback thresholds applied when the user has not configured custom alarm limits.
DEFAULT_MIN_RSRP = -105.0
DEFAULT_MIN_RSRQ = -15.0
DEFAULT_MIN_SINR = 5.0
DEFAULT_MIN_RSSI = -100.0


#SNMP Constants
# Settings for SNMP-based alarm and KPI reporting to the network management system.
SNMP_MANAGER_PORT = 161 #? or 162 (162 for agent) idk
SNMP_AGENT_PORT = 162
SNMP_MANAGER_IP = 2 #change
SNMP_COMMUNITY = "public" #? #NOT “public”, make it a random string
SNMP_VERSION = 2 #might need 3 - most likely 2 because we have an openVPN


#Alarm Severity Labels
SEVERITY_NORMAL = "NORMAL"
SEVERITY_WARNING = "WARNING"
SEVERITY_CRITICAL = "CRITICAL"


#Storage Rules
LOG_RETENTION_DAYS = 7
MAX_STORAGE_GB = 1 


#Hardware Expectations
MAX_ALLOWED_POWER_WATTS = 30
MAX_ALLOWED_TEMP_C = 80


#Identity Verification Rules
PCI_MATCH_REQUIRED = True
BAND_MATCH_REQUIRED = True
EARFCN_MATCH_REQUIRED = True


#AT Command Strings
# Pre-defined AT command strings issued to the Quectel modem via serial interface.
# These follow the Quectel AT command specification for LTE/NR5G modems.
AT_CMD_SERVING_CELL      = 'AT+QENG="servingcell"'         # Query current serving cell KPIs (RSRP, RSRQ, SINR, RSSI, PCI, EARFCN, band)
AT_CMD_NEIGHBOR_CELL     = 'AT+QENG="neighbourcell"'       # Query neighboring cell info
AT_CMD_FULL_FUNCTIONALITY = 'AT+CFUN=1'                    # Set modem to full functionality mode (enables RF and all interfaces)
AT_CMD_ALL_CELL_INFO     = 'AT+QSCAN=3'                    # Full cell scan — not used in production; retained for diagnostic purposes # [UNUSED?]
AT_CMD_5G_BAND_CONFIG    = 'AT+QNWPREFCFG="nr5g_band",'   # Set NR5G band preference — append target band number before sending (e.g. + "78")
AT_CMD_LTE_BAND_CONFIG   = 'AT+QNWPREFCFG="lte_band",'    # Set LTE band preference — append target band number before sending (e.g. + "4")
AT_CMD_COPS_AUTO         = 'AT+COPS=0'                     # Auto network registration — modem selects best available cell (required for NR5G)
AT_CMD_COPS_DEREGISTER   = 'AT+COPS=2'                     # Deregister from network — must be sent before switching to LTE-only band configuration


#System Timing Constants
VPN_RETRY_INTERVAL = 30
MODEM_BOOT_TIMEOUT = 60
SNMP_RETRY_LIMIT = 3
