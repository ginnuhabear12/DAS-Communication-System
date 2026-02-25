"""
Module: models/rules.py
Purpose: Define system limits, thresholds, identities, or protocol behavior - must stay consistent across deployments unless explicitly configured
Dependencies: ALL
Author: Ginna

"""


#Sampling Behavior Constants
SAMPLES_PER_SESSION = 5 #change
SAMPLE_INTERVAL_SECONDS = 60
AVERAGING_WINDOW_SECONDS = 300  # 5 minutes


#RF Valid Ranges
MIN_VALID_RSRP = -150
MAX_VALID_RSRP = -30

MIN_VALID_RSRQ = -30
MAX_VALID_RSRQ = 0

MIN_VALID_SINR = -20
MAX_VALID_SINR = 40

MIN_VALID_RSSI = -150
MAX_VALID_RSSI = -30


#Default Thresholds
# when nothing is set by the user
DEFAULT_MIN_RSRP = -105.0
DEFAULT_MIN_RSRQ = -15.0
DEFAULT_MIN_SINR = 5.0
DEFAULT_MIN_RSSI = -100.0


#SNMP Constants
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
AT_CMD_SERVING_CELL = 'AT+QENG="servingcell"'
AT_CMD_NEIGHBOR_CELL = 'AT+QENG="neighbourcell"'
AT_CMD_FULL_FUNCTIONALITY = 'AT+CFUN=1'
AT_CMD_AVAILABLE_SERVING_CELL = 'AT+QSCAN=3' #Will likely not use this in full code
AT_CMD_5G_BAND_CONFIG = 'AT+QNWPREFCFG="nr5g_band",' #When using this for the KPI collection, we will have to add the band # at the end of this to set bands
AT_CMD_LTE_BAND_CONFIG = 'AT+QNWPREFCFG="lte_band",' #When using this for the KPI collection, we will have to add the band # at the end of this to set bands

#System Timing Constants
VPN_RETRY_INTERVAL = 30
MODEM_BOOT_TIMEOUT = 60
SNMP_RETRY_LIMIT = 3
