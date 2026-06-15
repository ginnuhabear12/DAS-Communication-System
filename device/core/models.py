"""
Module: models.py
Purpose: Defines all data structures (dataclasses and plain classes) used at runtime.
         These objects represent KPI readings, averaged results, alarms, device config,
         and system health — they are instantiated and passed between modules but contain
         no business logic themselves.
"""


from datetime import datetime   
from dataclasses import dataclass
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# Raw (Instantaneous) KPI Classes
# These are populated directly from QENG AT command responses, one per sample.
# ══════════════════════════════════════════════════════════════════════════════ 
@dataclass
class KPIReading:
    """
    One raw modem sample from QENG.
 
    Attributes:
        timestamp: Time the sample was taken.
        rat:       Radio access technology — "LTE" or "NR5G".
        band:      Raw band number as returned by QENG (e.g. 4, 41).
        pci:       Physical Cell ID.
    """
    timestamp: datetime
    rat:       str
    band:      int
    pci:       int

@dataclass
class LTEKPI(KPIReading):
    """
    Instantaneous LTE sample from QENG.
    Inherits timestamp, rat, band, and pci from KPIReading.
 
    Attributes:
        earfcn: LTE frequency channel number.
        rsrp:   Reference Signal Received Power (dBm).
        rsrq:   Reference Signal Received Quality (dB).
        rssi:   Received Signal Strength Indicator (dBm).
        sinr:   Signal to Interference & Noise Ratio (dB).
 
    Note:
        Any field value > 1000 indicates an invalid/unavailable reading.
    """
    earfcn: int
    rsrp:   float
    rsrq:   float
    rssi:   float
    sinr:   float
 
 
@dataclass
class NR5GKPI(KPIReading):
    """
    Instantaneous NR5G sample from QENG.
    Inherits timestamp, rat, band, and pci from KPIReading.
    RSSI is omitted — not reported in QENG NR5G results.
 
    Attributes:
        arfcn:   NR5G absolute radio frequency channel number.
        ss_rsrp: SS-RSRP — Synchronization Signal RSRP (dBm).
        ss_rsrq: SS-RSRQ — Synchronization Signal RSRQ (dB).
        ss_sinr: SS-SINR — Synchronization Signal SINR (dB).
 
    Note:
        Any field value > 1000 indicates an invalid/unavailable reading.
    """
    arfcn:   int
    ss_rsrp: float
    ss_rsrq: float
    ss_sinr: float

# ══════════════════════════════════════════════════════════════════════════════
# Averaged KPI Classes
# These are computed after a full sampling session completes (typically 5 samples).
# They are what gets stored, reported via SNMP, and evaluated against thresholds.
# ══════════════════════════════════════════════════════════════════════════════
 
@dataclass
class AveragedKPI:
    """
    Base class for a time-window-averaged KPI result.
    Produced by averaging the raw KPIReadings from a SamplingSession.
 
    Attributes:
        start_time:   Timestamp of the first sample in the window.
        end_time:     Timestamp of the last sample in the window.
        rat:          Radio access technology — "LTE" or "NR5G".
        band:         Raw band number (e.g. 4, 41) — same as instantaneous.
        pci:          Physical Cell ID shared across the window's samples.
        sample_count: Number of samples in the window (typically 5).
    """
    start_time:   datetime
    end_time:     datetime
    rat:          str
    band:         int
    pci:          int
    sample_count: int = 5
 
 
@dataclass
class AveragedLTEKPI(AveragedKPI):
    """
    Averaged KPI result for one LTE band over a sampling window.
    Inherits start_time, end_time, rat, band, pci, and sample_count from AveragedKPI.
 
    KPI fields are None when the majority of that window's
    samples were invalid — meaning an INVALID_KPI alarm was
    already sent and no meaningful average could be computed.
 
    Attributes:
        earfcn:   LTE frequency channel number.
        avg_rsrp: Mean RSRP (dBm), or None if majority invalid.
        avg_rsrq: Mean RSRQ (dB),  or None if majority invalid.
        avg_rssi: Mean RSSI (dBm), or None if majority invalid.
        avg_sinr: Mean SINR (dB),  or None if majority invalid.
    """
    earfcn:   int          = 0
    avg_rsrp: float | None = None
    avg_rsrq: float | None = None
    avg_rssi: float | None = None
    avg_sinr: float | None = None
 
 
@dataclass
class AveragedNR5GKPI(AveragedKPI):
    """
    Averaged KPI result for one NR5G band over a sampling window.
    Inherits start_time, end_time, rat, band, pci, and sample_count from AveragedKPI.
    No avg_rssi field — RSSI is not reported in QENG NR5G results.
 
    Attributes:
        arfcn:       NR5G absolute radio frequency channel number.
        avg_ss_rsrp: Mean SS-RSRP (dBm), or None if majority invalid.
        avg_ss_rsrq: Mean SS-RSRQ (dB),  or None if majority invalid.
        avg_ss_sinr: Mean SS-SINR (dB),  or None if majority invalid.
    """
    arfcn:       int          = 0
    avg_ss_rsrp: float | None = None
    avg_ss_rsrq: float | None = None
    avg_ss_sinr: float | None = None

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

# ══════════════════════════════════════════════════════════════════════════════
# Sampling Session
# Intermediate container used during active KPI collection before averaging.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SamplingSession:
    """
    Holds the raw KPIReadings collected during one active sampling window
    before they are averaged into an AveragedKPI result.

    Keeping this as a separate object (rather than averaging inline) improves
    debuggability, makes unit testing easier, and gives a clear handoff point
    between the collection phase and the averaging/reporting phase.

    Attributes:
        session_start: Timestamp when this sampling window began.
        readings:      List of raw KPIReading objects collected so far in this session.
                       Passed to the averaging logic once SAMPLES_PER_SESSION is reached.
    """
    session_start: datetime
    readings:      list[KPIReading]


