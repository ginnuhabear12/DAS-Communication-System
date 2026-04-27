# TEST CODE FOR MODEM FUNCTIONALITY TESTS 0.1.5.4
# No SIM — multi-carrier LTE scan + NR passive scan

import time
from atCommandExample import at_command_comms


bands = [ 'b2','b4', 'b5' ]

nrBand = []
lteBand = []

for band in bands:
    band = band.strip()
    if band.startswith('n'):
        nrBand.append(band)
    elif band.startswith('b'):
        lteBand.append(band)


# --- SETUP ---
# CFUN=1 restarts radio with clean NVM state
print(at_command_comms("AT+CFUN=1", 15))
time.sleep(5)
at_command_comms('AT+QNWPREFCFG="mode_pref",AUTO', 3)

print(at_command_comms("AT+COPS=0", 30))
time.sleep(5)
# No COPS=0 — modem cannot register without SIM


while True:
    # --- NR5G BANDS ---
    for band in nrBand:
        band_num = band[1:]
        at_command_comms(f'AT+QNWPREFCFG="nr5g_band",{band_num}', 0.3)
        print(at_command_comms("AT+CFUN=0", 15))
        time.sleep(3)
        print(at_command_comms("AT+CFUN=1", 15))
        time.sleep(3)
        at_command_comms("AT+QNWINFO", 0.3)
        print(at_command_comms('AT+QENG="servingcell"', 0.3))
        print(at_command_comms("AT+QRSRP", 0.3))

    # --- COPS=2 between loops ---
    # With no SIM the modem is already deregistered, but COPS=2
    # explicitly clears any residual PLMN state before LTE scanning
    at_command_comms('AT+QNWPREFCFG="mode_pref",LTE', 3)
    print(at_command_comms("AT+COPS=2", 30))
    # No sleep — first band change fires immediately after detach

    # --- LTE BANDS ---
    for band in lteBand:
        band_num = band[1:]
        at_command_comms(f'AT+QNWPREFCFG="lte_band",{band_num}', 0.3)
        print(at_command_comms("AT+CFUN=0", 15))
        time.sleep(3)
        print(at_command_comms("AT+CFUN=1", 15))
        time.sleep(3)
        print(at_command_comms('AT+QENG="servingcell"', 0.3))

    print()
    print()
    print("NEW SESSION")


