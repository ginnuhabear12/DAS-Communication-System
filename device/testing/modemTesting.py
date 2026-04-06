#TEST CODE FOR MODEM FUNCTIONALITY TESTS 0.1.3.1 

import serial
import time
from atCommandExample import at_command_comms

bands = ['n2', 'b2', 'b5', 'b12', 'b66']
at_command_comms("AT+CFUN=1", 15)
time.sleep(10)
at_command_comms('AT+QNWPREFCFG="mode_pref",AUTO', 3)

nrBand = []
lteBand = []


for band in bands:
     band = band.strip()
     if band.startswith('b'):
          lteBand.append(band)
     elif band.startswith('n'):
          nrBand.append(band)
          
     

for band in nrBand:
     band_num = band[1:]
     at_command_comms(f'AT+QNWPREFCFG="nr5g_band",{band_num}', 0.3)
     at_command_comms("AT+CPIN?",15)
     time.sleep(3)
      #print(at_command_comms("AT+QCSQ", 0.3))
     print(at_command_comms('AT+QENG="servingcell"',0.3))
  

at_command_comms("AT+COPS=2", 180)

for band in lteBand:
    band_num = band[1:]
    at_command_comms(f'AT+QNWPREFCFG="lte_band",{band_num}', 0.3)
    at_command_comms("AT+CPIN?",15)
    time.sleep(3)
    #print(at_command_comms("AT+QCSQ", 0.3))
    print(at_command_comms('AT+QENG="servingcell"',0.3))
  
at_command_comms("AT+COPS=0", 180)


