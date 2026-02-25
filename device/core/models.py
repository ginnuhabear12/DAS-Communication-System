"""
Module: models/core.py
Purpose: Defines all objects in the system and set parameters - runtime data
Dependencies: ALL
Author: Ginna

"""

from datetime import datetime   
from dataclasses import dataclass
 
@dataclass #eliminates boilerplate and makes the code cleaner
class KPIReading: #this represents one raw modem sample - the modem will take multiple of these before averaging
    timestamp: datetime   #need to account for timezones & daylight saving - use class datetime.tzinfo #need to be aware
    rssi: float
    rsrp: float
    rsrq: float
    sinr: float
    pci: int
    earfcn: int
    band: str #string because contains letters and numbers

class AveragedKPI:
    start_time: datetime #start and end time is naive - not realying on time zones
    end_time: datetime  #also this is a timer for how long to wait before storing - window
    avg_rssi: float
    avg_rsrp: float
    avg_rsrq: float
    avg_sinr: float
    pci: int
    earfcn: int
    band: str

class SignalIdentity: #what is expected for the values and compares with incoming modem data
    pci: int
    earfcn: int
    band: str
    bandwidth_mhz: float

class MonitoringThresholds:  #this uses the alarm limits/ranges that are defined in the GUI
    min_rsrp: float
    min_rsrq: float
    min_sinr: float
    min_rssi: float

class AlarmEvent: #uses this when an alarm event happens
    timestamp: datetime
    severity: str  #NORMAL, WARNING, CRITICAL
    reason: str #example: RSRP below threshold, modem disconnected, etc.
    kpi_snapshot: AveragedKPI #when alarm is fired, you freeze that data that causes it (??)

class SNMPTrapPayload: #this class doesn't send anything, it justs represents the structured data that will be sent #primarly for organization, helps to keep transport logic separate from monitoring logic
    device_id: str
    event_type: str
    message: str
    timestamp: datetime

class DeviceConfig:
    hostname: str
    static_ip: str | None # may not exist so it can be left empty and there will be no problems
    dhcp_enabled: bool
    vpn_enabled: bool
    snmp_manager_ip: str
    admin_username: str
    password_hash: str
    #if this is the device config, then the number of bands as well as a list of the set bands should be placed here to be used in KPI collection

class StorageRecord: #Represents a single row in SQLite. or in our json file
    timestamp: datetime
    kpi: AveragedKPI
    alarm: AlarmEvent | None

class SystemHealthStatus:
    modem_online: bool
    storage_ok: bool
    vpn_connected: bool
    temperature_c: float
    power_ok: bool

@dataclass
class SamplingSession: # Cleaner architecture. Helps debugging. Helps test reproducibility. #this just shows the data in a specific window before being averaged #called by AveragedKPI to be used
    session_start: datetime
    readings: list[KPIReading]


