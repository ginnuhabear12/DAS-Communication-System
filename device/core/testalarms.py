"""
test_alarms.py — Manual Test for alarms.py Logic
-------------------------------------------------
Creates 5 hardcoded SamplingSession instances and calls process_window()
to verify all alarm scenarios work correctly before SNMP is integrated.

3 bands per session:
    Band 4  (LTE)  — All valid, all in range       → NO alarms expected
    Band 12 (LTE)  — RSRP last 3 invalid           → INVALID alarm for RSRP
                   — SINR valid but below threshold → THRESHOLD alarm for SINR
    Band 41 (NR5G) — SS-RSRP valid but below threshold → THRESHOLD alarm for SS-RSRP

Expected output:
    [INVALID]   Band 12 | RSRP: last 3 samples invalid
    [THRESHOLD] Band 12 | SINR: avg = -2.2, below threshold (0.0)
    [THRESHOLD] Band 41 | SS_RSRP: avg = -114.0, below threshold (-110.0)
"""

from datetime import datetime
from models import LTEKPI, NR5GKPI, SamplingSession
from alarms import process_window

# ── Timestamps ────────────────────────────────────────────────────────────────
# One timestamp per session, spaced 1 minute apart
t1 = datetime(2026, 3, 12, 10, 0, 0)
t2 = datetime(2026, 3, 12, 10, 1, 0)
t3 = datetime(2026, 3, 12, 10, 2, 0)
t4 = datetime(2026, 3, 12, 10, 3, 0)
t5 = datetime(2026, 3, 12, 10, 4, 0)

# ══════════════════════════════════════════════════════════════════════════════
# Session 1
# ══════════════════════════════════════════════════════════════════════════════
session1 = SamplingSession(
    session_start = t1,
    readings = [
        # Band 4 — LTE — all valid, in range
        LTEKPI(
            timestamp = t1, rat = "LTE", band = 4, pci = 101,
            earfcn = 1600, rsrp = -100.0, rsrq = -8.0, rssi = -80.0, sinr = 10.0,
        ),
        # Band 12 — LTE — RSRP valid in first 2 sessions, SINR below threshold
        LTEKPI(
            timestamp = t1, rat = "LTE", band = 12, pci = 202,
            earfcn = 5110, rsrp = -105.0, rsrq = -10.0, rssi = -85.0, sinr = -2.0,
        ),
        # Band 41 — NR5G — SS-RSRP below threshold
        NR5GKPI(
            timestamp = t1, rat = "NR5G", band = 41, pci = 303,
            arfcn = 520000, ss_rsrp = -112.0, ss_rsrq = -10.0, ss_sinr = 5.0,
        ),
    ]
)

# ══════════════════════════════════════════════════════════════════════════════
# Session 2
# ══════════════════════════════════════════════════════════════════════════════
session2 = SamplingSession(
    session_start = t2,
    readings = [
        # Band 4 — all valid, in range
        LTEKPI(
            timestamp = t2, rat = "LTE", band = 4, pci = 101,
            earfcn = 1600, rsrp = -98.0, rsrq = -7.0, rssi = -82.0, sinr = 12.0,
        ),
        # Band 12 — RSRP still valid, SINR below threshold
        LTEKPI(
            timestamp = t2, rat = "LTE", band = 12, pci = 202,
            earfcn = 5110, rsrp = -108.0, rsrq = -11.0, rssi = -87.0, sinr = -3.0,
        ),
        # Band 41 — SS-RSRP below threshold
        NR5GKPI(
            timestamp = t2, rat = "NR5G", band = 41, pci = 303,
            arfcn = 520000, ss_rsrp = -115.0, ss_rsrq = -11.0, ss_sinr = 6.0,
        ),
    ]
)

# ══════════════════════════════════════════════════════════════════════════════
# Session 3
# ══════════════════════════════════════════════════════════════════════════════
session3 = SamplingSession(
    session_start = t3,
    readings = [
        # Band 4 — all valid, in range
        LTEKPI(
            timestamp = t3, rat = "LTE", band = 4, pci = 101,
            earfcn = 1600, rsrp = -102.0, rsrq = -9.0, rssi = -79.0, sinr = 11.0,
        ),
        # Band 12 — RSRP now invalid (last 3 sessions) → should trigger INVALID alarm
        LTEKPI(
            timestamp = t3, rat = "LTE", band = 12, pci = 202,
            earfcn = 5110, rsrp = 9999.0, rsrq = -10.0, rssi = -86.0, sinr = -1.0,
        ),
        # Band 41 — SS-RSRP below threshold
        NR5GKPI(
            timestamp = t3, rat = "NR5G", band = 41, pci = 303,
            arfcn = 520000, ss_rsrp = -113.0, ss_rsrq = -10.0, ss_sinr = 5.0,
        ),
    ]
)

# ══════════════════════════════════════════════════════════════════════════════
# Session 4
# ══════════════════════════════════════════════════════════════════════════════
session4 = SamplingSession(
    session_start = t4,
    readings = [
        # Band 4 — all valid, in range
        LTEKPI(
            timestamp = t4, rat = "LTE", band = 4, pci = 101,
            earfcn = 1600, rsrp = -99.0, rsrq = -8.0, rssi = -81.0, sinr = 10.0,
        ),
        # Band 12 — RSRP invalid (last 3 sessions)
        LTEKPI(
            timestamp = t4, rat = "LTE", band = 12, pci = 202,
            earfcn = 5110, rsrp = 9999.0, rsrq = -12.0, rssi = -88.0, sinr = -2.0,
        ),
        # Band 41 — SS-RSRP below threshold
        NR5GKPI(
            timestamp = t4, rat = "NR5G", band = 41, pci = 303,
            arfcn = 520000, ss_rsrp = -114.0, ss_rsrq = -12.0, ss_sinr = 7.0,
        ),
    ]
)

# ══════════════════════════════════════════════════════════════════════════════
# Session 5
# ══════════════════════════════════════════════════════════════════════════════
session5 = SamplingSession(
    session_start = t5,
    readings = [
        # Band 4 — all valid, in range
        LTEKPI(
            timestamp = t5, rat = "LTE", band = 4, pci = 101,
            earfcn = 1600, rsrp = -101.0, rsrq = -7.0, rssi = -80.0, sinr = 12.0,
        ),
        # Band 12 — RSRP invalid (last 3 sessions)
        LTEKPI(
            timestamp = t5, rat = "LTE", band = 12, pci = 202,
            earfcn = 5110, rsrp = 9999.0, rsrq = -11.0, rssi = -85.0, sinr = -3.0,
        ),
        # Band 41 — SS-RSRP below threshold
        NR5GKPI(
            timestamp = t5, rat = "NR5G", band = 41, pci = 303,
            arfcn = 520000, ss_rsrp = -116.0, ss_rsrq = -11.0, ss_sinr = 6.0,
        ),
    ]
)

# ══════════════════════════════════════════════════════════════════════════════
# Run the test
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sessions = [session1, session2, session3, session4, session5]

    print("=" * 60)
    print("Running process_window() test")
    print("=" * 60)
    print()

    process_window(sessions)

    print()
    print("=" * 60)
    print("Test complete — verify output matches expected alarms above")
    print("=" * 60)