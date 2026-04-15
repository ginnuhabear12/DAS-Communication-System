"""
modem_test_remaining.py — Unconfirmed Behavior Tests
Version: 0.4.0.0

Purpose:
    Tests only the behaviors that have NOT yet been confirmed by the
    previous test run. All confirmed behaviors are excluded.

Remaining unknowns:
    Test A — CEREG settlement polling after COPS=0
             How long does the modem take to leave stat=2 (searching)?
             Determines whether production code needs a polling loop
             or a fixed sleep after COPS=0.

    Test B — B14 (FirstNet) after COPS=2 with extended timing
             Test 3A returned SEARCH for B14 even though QSCAN showed -94 dBm.
             B12 and B13 succeeded in the same section with 3s sleep.
             Unknown: is the failure timing (too short) or FirstNet-specific
             (air interface restricts non-registered devices)?
             Two sub-tests: 10s sleep and 20s sleep, with AT+QNWINFO
             cross-check before each QENG call.

Hardware:
    Unactivated AT&T SIM inserted.
    Modem connected and responding.
"""

import time
from atCommandExample import at_command_comms


# ── Constants ─────────────────────────────────────────────────────────────────
ALL_LTE_BANDS  = "1:2:3:4:5:7:8:12:13:14:17:20:25:26:28:29:30:66:71"
ALL_NR5G_BANDS = "2:5:25:41:66:71:77:78:79"

B14 = "14"   # AT&T FirstNet  EARFCN 5330  RSRP -94  — failed in 3A
B12 = "12"   # AT&T standard  EARFCN 5110  RSRP -95  — succeeded in 3D (control)


# ── Helpers ───────────────────────────────────────────────────────────────────
def run(label, command, timeout):
    result = at_command_comms(command, timeout)
    print(f"  RESULT [{label}]:")
    for line in result.strip().splitlines():
        stripped = line.strip()
        if stripped:
            print(f"    {stripped}")
    print()
    return result


def section_header(title):
    bar = "=" * 72
    print(f"\n{bar}")
    print(f"  {title}")
    print(f"{bar}\n")


def test_header(name, expected):
    print(f"  +-- {name}")
    print(f"  |   Expected : {expected}")
    print(f"  +{'-' * 69}\n")


def note(lines):
    for line in lines:
        print(f"  [NOTE] {line}")
    print()


def restore_bands():
    at_command_comms(f'AT+QNWPREFCFG="lte_band",{ALL_LTE_BANDS}', 3)
    at_command_comms(f'AT+QNWPREFCFG="nr5g_band",{ALL_NR5G_BANDS}', 3)


# =============================================================================
# SETUP
# =============================================================================
section_header("SETUP -- Full modem reset to known state")

run("RESET",          "AT+CFUN=1",                                    15)
time.sleep(10)

run("MODE AUTO",      'AT+QNWPREFCFG="mode_pref",AUTO',              3)
run("ALL LTE BANDS",  f'AT+QNWPREFCFG="lte_band",{ALL_LTE_BANDS}',  3)
run("ALL NR5G BANDS", f'AT+QNWPREFCFG="nr5g_band",{ALL_NR5G_BANDS}',3)

note(["Setup complete. Proceeding to unconfirmed behavior tests only."])


# =============================================================================
# TEST A -- CEREG Settlement Polling After COPS=0
#
# Previous result: CEREG returned stat=2 (still searching) after 8s sleep.
# stat=3 (denied) was never observed -- the modem hadn't finished its attempt.
#
# This test replaces the fixed sleep with a polling loop that samples CEREG
# every 2 seconds and records the stat at each sample until it leaves stat=2.
# The total elapsed time at resolution tells us the minimum reliable wait time.
#
# What to record:
#   - The stat value at each 2s interval
#   - The total elapsed seconds when stat first changes from 2
#   - The final settled stat value (expected: 3 for unactivated SIM)
# =============================================================================
section_header("TEST A -- CEREG Settlement Polling After COPS=0")

test_header(
    "A: AT+COPS=0 then poll CEREG every 2s until stat leaves 2",
    "stat=3 (registration denied) after some number of seconds. Recording elapsed time."
)

run("AUTO-REGISTER", "AT+COPS=0", 60)

print("  [POLL] Sampling CEREG every 2 seconds. Recording stat at each interval.")
print()

MAX_POLLS    = 30    # 30 x 2s = 60s ceiling before giving up
poll_count   = 0
settled_stat = None
elapsed      = 0

for i in range(MAX_POLLS):
    time.sleep(2)
    elapsed = (i + 1) * 2
    reg = run(f"CEREG @ {elapsed}s", "AT+CEREG?", 3)

    # Extract the stat digit from "+CEREG: 0,X"
    stat = None
    for line in reg.splitlines():
        if "+CEREG:" in line:
            parts = line.strip().split(",")
            if len(parts) >= 2:
                stat = parts[-1].strip()

    print(f"  [POLL] t={elapsed}s  stat={stat}")

    if stat != "2":
        settled_stat = stat
        print(f"\n  [POLL] Stat left 2 at t={elapsed}s. Settled value: stat={settled_stat}")
        break
else:
    print(f"\n  [POLL] stat=2 persisted for all {MAX_POLLS * 2}s. Modem did not settle.")

print()
note([
    f"Elapsed until resolution: {elapsed}s",
    f"Settled CEREG stat      : {settled_stat}",
    "",
    "stat=3 at resolution --> REGISTRATION_DENIED confirmed.",
    "   Use this elapsed time as the minimum polling ceiling in instKPIcollection.",
    "   Production code should poll at 2s intervals up to this ceiling.",
    "stat=1 or 5          --> SIM unexpectedly registered. Recheck SIM.",
    "stat=2 at ceiling    --> Modem never resolved. Treat as REGISTRATION_DENIED.",
    "                         May indicate deeper issue -- run AT+COPS? to inspect.",
])

# Run C5GREG and COPS? after settlement for completeness
run("5G REG (C5GREG)", "AT+C5GREG?", 3)
run("OPERATOR (COPS?)", "AT+COPS?",   3)


# =============================================================================
# TEST B -- B14 (FirstNet) After COPS=2 with Extended Timing
#
# Previous result: Test 3A returned SEARCH for B14 after COPS=2 with 3s sleep.
# B12 (AT&T standard) and B13 (Verizon) both succeeded with the same timing.
# Two possible causes -- cannot yet be distinguished:
#   1. Timing: 3s was too short after COPS=2 for the first band scan to complete.
#   2. FirstNet restriction: B14 requires a registered state even for LIMSRV camp.
#      FirstNet (Band 14) is reserved for public safety and some deployments
#      restrict air interface access to credentialed devices only.
#
# Method:
#   B1: Deregister. Wait 10s. Set B14. Wait 5s. QNWINFO then QENG.
#   B2: Deregister. Wait 20s. Set B14. Wait 5s. QNWINFO then QENG.
#   B3: Deregister. Wait 20s. Set B12. Wait 5s. QNWINFO then QENG.
#       B3 is the control -- if B12 also fails at 20s, timing is not the issue.
#
# AT+QNWINFO is queried before AT+QENG in each sub-test.
# QNWINFO reports what the modem believes it is currently camped on,
# independent of QENG. If QNWINFO shows No Service and QENG shows SEARCH,
# the modem genuinely has no camp -- ruling out a QENG parsing issue.
# If QNWINFO shows a network but QENG shows SEARCH, there is a state mismatch
# worth investigating further.
#
# What to record for each sub-test:
#   - QNWINFO result before QENG
#   - QENG result
#   - Whether result changed vs 3A (SEARCH)
# =============================================================================
section_header("TEST B -- B14 (FirstNet) After COPS=2 with Extended Timing")

note([
    "Previous 3A result: B14 returned SEARCH after COPS=2 + 3s sleep.",
    "B12 returned LIMSRV with identical timing in 3D. B13 also succeeded.",
    "This test isolates timing vs FirstNet air interface restriction.",
    "B3 (B12 control at 20s) confirms whether longer sleep fixes B14 or not.",
])

# -- Sub-test B1: B14 after COPS=2, 10s deregister wait, 5s band settle -------
test_header(
    "B1: COPS=2 --> wait 10s --> set B14 --> wait 5s --> QNWINFO --> QENG",
    "LTE LIMSRV on B14 if timing was the issue. SEARCH if FirstNet restricts access."
)

run("DEREGISTER", "AT+COPS=2", 15)
print("  [WAIT] 10s post-COPS=2 settle...")
time.sleep(10)

run("CEREG CHECK", "AT+CEREG?", 3)    # Confirm deregistration is complete before proceeding

run(f"SET B{B14}", f'AT+QNWPREFCFG="lte_band",{B14}', 3)
print("  [WAIT] 5s band settle...")
time.sleep(5)

run("QNWINFO B1",           "AT+QNWINFO",          3)
result_b1 = run("QENG B14 (10s wait)", 'AT+QENG="servingcell"', 3)

note([
    "LIMSRV on EARFCN 5330 (B14) --> Timing was the issue. 10s is sufficient.",
    "   Production sleep after COPS=2 must be at least 10s.",
    "SEARCH again --> 10s is still insufficient. Proceed to B2 (20s).",
    "QNWINFO shows service but QENG shows SEARCH --> state mismatch. Note this.",
])

restore_bands()
time.sleep(2)

# -- Sub-test B2: B14 after COPS=2, 20s deregister wait, 5s band settle -------
test_header(
    "B2: COPS=2 --> wait 20s --> set B14 --> wait 5s --> QNWINFO --> QENG",
    "LTE LIMSRV on B14 if 20s is sufficient. SEARCH if FirstNet restricts regardless."
)

run("DEREGISTER", "AT+COPS=2", 15)
print("  [WAIT] 20s post-COPS=2 settle...")
time.sleep(20)

run("CEREG CHECK", "AT+CEREG?", 3)

run(f"SET B{B14}", f'AT+QNWPREFCFG="lte_band",{B14}', 3)
print("  [WAIT] 5s band settle...")
time.sleep(5)

run("QNWINFO B2",           "AT+QNWINFO",          3)
result_b2 = run("QENG B14 (20s wait)", 'AT+QENG="servingcell"', 3)

note([
    "LIMSRV on B14 --> 20s settle resolves the issue. Use 20s as the post-COPS=2 floor.",
    "SEARCH again  --> FirstNet is the cause, not timing. B14 cannot be camped",
    "   in an unregistered state at this location. In production with active SIM,",
    "   re-test to determine if registered access resolves B14.",
    "Compare B2 vs B1: if B1=SEARCH and B2=LIMSRV, the answer is purely timing.",
    "Compare B2 vs B3: if B2=SEARCH and B3=LIMSRV, timing is ruled out for B14.",
])

restore_bands()
time.sleep(2)

# -- Sub-test B3: B12 control after COPS=2, 20s deregister wait ---------------
test_header(
    "B3: COPS=2 --> wait 20s --> set B12 (control) --> wait 5s --> QNWINFO --> QENG",
    "LTE LIMSRV on B12. Control case -- B12 succeeded at 3s before, must succeed here."
)

run("DEREGISTER", "AT+COPS=2", 15)
print("  [WAIT] 20s post-COPS=2 settle...")
time.sleep(20)

run("CEREG CHECK", "AT+CEREG?", 3)

run(f"SET B{B12}", f'AT+QNWPREFCFG="lte_band",{B12}', 3)
print("  [WAIT] 5s band settle...")
time.sleep(5)

run("QNWINFO B3",           "AT+QNWINFO",          3)
result_b3 = run("QENG B12 (20s wait, control)", 'AT+QENG="servingcell"', 3)

note([
    "LIMSRV on B12 --> Control confirmed. B12 works at 20s as expected.",
    "   Now compare against B2 result to isolate B14 behavior.",
    "SEARCH on B12  --> Something changed in the environment. Recheck QSCAN.",
    "   This would invalidate the B2 comparison and require re-running.",
    "",
    "FINAL INTERPRETATION GUIDE:",
    "  B1=LIMSRV                     --> 10s post-COPS=2 sleep is sufficient.",
    "  B1=SEARCH, B2=LIMSRV          --> 20s post-COPS=2 sleep is sufficient.",
    "  B1=SEARCH, B2=SEARCH, B3=LIMSRV --> B14 FirstNet restriction confirmed.",
    "     Handle B14 in production: if SEARCH persists after 20s, tag as SEARCH.",
    "     Do not attempt further extended waits for B14 specifically.",
    "  B1=SEARCH, B2=SEARCH, B3=SEARCH --> Environment changed. Re-run full test.",
])


# =============================================================================
# RESTORE
# =============================================================================
section_header("RESTORE -- Returning modem to production-ready state")

run("MODE AUTO",     'AT+QNWPREFCFG="mode_pref",AUTO',              3)
run("ALL LTE BANDS", f'AT+QNWPREFCFG="lte_band",{ALL_LTE_BANDS}',  3)
run("ALL NR5G BANDS",f'AT+QNWPREFCFG="nr5g_band",{ALL_NR5G_BANDS}',3)
run("REREGISTER",    "AT+COPS=0",                                    30)

print("\n" + "=" * 72)
print("  REMAINING TESTS COMPLETE")
print("=" * 72)
print("""
  Record these results against the expected outcomes above:

  Test A -- CEREG polling:
    Elapsed time at resolution and settled stat value.
    This sets the COPS=0 polling ceiling for instKPIcollection.

  Test B -- B14 timing isolation:
    B1 vs B2 vs B3 comparison dpinetermines whether B14 needs
    extended timing or is a FirstNet restriction regardless of wait.
    Result drives the post-COPS=2 sleep value in instKPIcollection.
""")