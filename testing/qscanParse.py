import time # Assuming you'll use this for the web server
from datetime import datetime, timedelta
import re




# TEST CODE FOR PARSING OF QSCAN VALUES
# --- PRE-EXISTING STORAGE (Mocking Class Attributes) ---
serving_cells_lte = []
serving_cells_5g = []

# --- TARGET INPUTS ---
# Format: (RAT_Type, Frequency_Value)
target_configs = [
    ("NR5G", 396970),
    ("NR5G", 125290),
    ("LTE", 1000)
]

def parse_qscan(raw_output):
    results = {"LTE": [], "NR5G": []}
    lines = raw_output.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if "+QSCAN:" in line:
            # 1. Strip headers and quotes
            clean_line = line.replace("+QSCAN:", "").replace('"', '').strip()
            raw_list = clean_line.split(',')
            
            # 2. Convert to numbers where possible
            final_values = []
            for item in raw_list:
                item = item.strip()
                try:
                    # Attempt to turn the text into a whole number
                    final_values.append(int(item))
                except ValueError:
                    # If it fails (like for "-" or "LTE"), keep it as a string
                    final_values.append(item)

            rat_type = final_values[0]

            # 3. Map values to keys
            if rat_type == "LTE":
                results["LTE"].append({
                    "RAT": "LTE", "MCC": final_values[1], "MNC": final_values[2], "FREQ": final_values[3],
                    "PCI": final_values[4], "RSRP": final_values[5], "RSRQ": final_values[6],
                    "srxlev": final_values[7], "squal": final_values[8]
                })
            elif rat_type == "NR5G":
                results["NR5G"].append({
                    "RAT": "NR5G", "MCC": final_values[1], "MNC": final_values[2], "FREQ": final_values[3],
                    "PCI": final_values[4], "RSRP": final_values[5], "RSRQ": final_values[6],
                    "srxlev": final_values[7], "SCS": final_values[8]
                })
                
    return results

# --- TEST DATA ---
qscan_data = """
+QSCAN: "NR5G",310,410,658080,543,-109,-14,-,1
+QSCAN: "NR5G",313,100,658080,543,-109,-14,-,1
+QSCAN: "NR5G",310,410,177150,552,-109,-15,-,0
+QSCAN: "NR5G",313,100,177150,552,-109,-15,-,0
+QSCAN: "NR5G",310,260,501390,364,-104,-11,7,1
+QSCAN: "NR5G",310,260,396970,387,-103,-11,16,0
+QSCAN: "NR5G",310,260,125290,278,-114,-13,3,0
+QSCAN: "NR5G",310,260,521310,364,-109,-12,2,1
+QSCAN: "LTE",310,410,1000,402,-109,-10,17,21
+QSCAN: "LTE",313,100,1000,402,-109,-10,17,21
+QSCAN: "LTE",310,410,66836,367,-115,-16,11,15
+QSCAN: "LTE",313,100,66836,367,-115,-16,11,15
+QSCAN: "LTE",310,260,5035,253,-125,-17,-1,121
+QSCAN: "LTE",311,480,2450,336,-118,-12,2,111
+QSCAN: "LTE",311,480,66536,336,-125,-20,-,109
+QSCAN: "LTE",311,480,5230,336,-118,-19,10,109
+QSCAN: "LTE",310,260,8115,447,-,-,-,0
+QSCAN: "LTE",311,480,8315,336,-,-,-,0
+QSCAN: "LTE",310,410,9260,243,-,-,-,0
+QSCAN: "LTE",313,100,9260,243,-,-,-,0
+QSCAN: "LTE",310,410,9260,409,-,-,-,0
+QSCAN: "LTE",313,100,9260,409,-,-,-,0
+QSCAN: "LTE",310,260,66711,111,-108,-12,16,116
"""

parsed = parse_qscan(qscan_data)

# Print results to see the difference
print("--- LTE CELLS ---")
for cell in parsed["LTE"]:
    print(cell)

print("\n--- 5G CELLS ---")
for cell in parsed["NR5G"]:
    print(cell)

print(f"The frequencies that are being searched for: {target_configs}")

combined_list = parsed["LTE"] + parsed["NR5G"]

# --- SCALABLE FILTERING LOGIC ---
for target_rat, target_freq in target_configs:
    for cell in combined_list:
        if cell["RAT"] == target_rat and cell["FREQ"] == target_freq:
            # Dispatch data to the appropriate storage list
            if target_rat == "LTE":
                serving_cells_lte.append(cell)
            elif target_rat == "NR5G":
                serving_cells_5g.append(cell)

# --- CONFIRMATION PRINTOUT ---
print(f"Update Complete: {len(serving_cells_lte)} LTE cell(s) and {len(serving_cells_5g)} 5G cell(s) verified.")

print("\n--- STORED LTE SERVING CELLS ---")
for cell in serving_cells_lte:
    print(cell)

print("\n--- STORED 5G SERVING CELLS ---")
for cell in serving_cells_5g:
    print(cell)







#TEST CODE FOR MODEM FUNCTIONALITY TESTS 0.1.3.1 

import serial
import time
from testing.testing.atCommandExample import at_command_comms

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
     at_command_comms("AT+QNWINFO", 0.3)
      #print(at_command_comms("AT+QCSQ", 0.3))
     print(at_command_comms('AT+QENG="servingcell"',0.3))
     print(at_command_comms("AT+QRSRP",0.3))

at_command_comms("AT+COPS=2", 180)

for band in lteBand:
    band_num = band[1:]
    at_command_comms(f'AT+QNWPREFCFG="lte_band",{band_num}', 0.3)
    at_command_comms("AT+CPIN?",15)
    time.sleep(3)
    at_command_comms("AT+QNWINFO", 0.3)
    #print(at_command_comms("AT+QCSQ", 0.3))
    print(at_command_comms('AT+QENG="servingcell"',0.3))
  
at_command_comms("AT+COPS=0", 180)


