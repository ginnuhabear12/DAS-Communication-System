import time # Assuming you'll use this for the web server
from datetime import datetime, timedelta
from atCommandExample import at_command_comms
from core.models import KPIReading, SamplingSession
from core.rules import AT_CMD_SERVING_CELL, AT_CMD_5G_BAND_CONFIG, AT_CMD_LTE_BAND_CONFIG, MIN_VALID_RSRP, MAX_VALID_RSRP
from core.rules import MIN_VALID_RSRQ, MAX_VALID_RSRQ, MIN_VALID_SINR, MAX_VALID_SINR, MIN_VALID_RSSI, MAX_VALID_RSSI
import re


# method to set retrys in the case of an ERROR
# will need to have the AT command as the input
def send_at_command_with_retry(command, max_retries=3):
    # loop in case there are retries for AT commands needed (max 3)
    for attempt in range(max_retries):
        response = at_command_comms(command) # Your existing method
        
        # return the response if there are no ERROR messages
        if response != "ERROR":
            return response
        
        print(f"Attempt {attempt + 1} failed for {command}. Retrying...")
        time.sleep(0.3) # Short cool-down before retry
    
    # raise an exception to be caught in a try, except code so that it can be handled within the mainKPI loop
    raise Exception(f"Modem Alert: Command {command} failed after {max_retries} attempts.")


def parse_and_validate_serving_cell_response(raw_response: str) -> dict:
    """
    Parses the AT+QENG="servingcell" response.
    Expected format (LTE example): +QENG: "servingcell","NOCONN","LTE","FDD",405,87,12345,100,20,3,15,15,-95,-12,-75,10,...
    """
    parsed = {}

    try:
        # Clean the string: remove newlines and leading/trailing whitespace
        clean_str = raw_response.strip().replace('\r', '').replace('\n', '')

        # Use regex to find all values inside quotes or between commas
        # This handles strings ("LTE") and numbers (12345)
        parts = re.findall(r'[^",\s]+|"(?:\\"|[^"])*"', clean_str)

        # Remove quotes from strings if they exist (e.g., "LTE" -> LTE)
        parts = [p.strip('"') for p in parts]

        # Check if this is a valid +QENG response
        if "servingcell" not in parts:
            return parsed # Return empty if it's not the right command output

        # Logic for LTE (Usually parts[2] is "LTE")
        if "LTE" in parts:
            # Note: Indexing depends on your specific modem's firmware version
            # These are standard mappings for many Quectel modules:
            parsed['pci'] = int(parts[7])      # Physical Cell ID
            parsed['earfcn'] = int(parts[8])   # EARFCN
            parsed['rsrp'] = float(parts[12])  # RSRP
            parsed['rsrq'] = float(parts[13])  # RSRQ
            parsed['rssi'] = float(parts[14])  # RSSI
            parsed['sinr'] = float(parts[15])  # SINR
            
        # Logic for 5G (NR5G)
        elif "NR5G" in parts or "5G" in parts:
            # 5G indexing is different; often RSRP is at a different position
            parsed['pci'] = int(parts[6])
            parsed['earfcn'] = int(parts[7])
            parsed['rsrp'] = float(parts[11])
            parsed['rsrq'] = float(parts[12])
            parsed['sinr'] = float(parts[13])

    except (IndexError, ValueError, TypeError) as e:
        # If parsing fails, we return the empty dict, 
        # which triggers the retry logic in your main loop.
        print(f"Parsing Error: {e}")

    return parsed


def instKPIcollection(bands: list[str], startTime: datetime):
    
    # this is the list in which the KPIs collected in the following loop will be appended.
    loopKPIs = []
    
    for band in bands:
        
        # Initialize a "blank" KPI object for this band in case of failure
        currentKPIs = KPIReading(startTime, 0, 0, 0, 0, 0, 0, band)

        
        # try, except error handling in case of band failing to lock. This will catch the exception from the send_at_command_with_retry method
        try:

            #check whether the band is lte or 5G
            #to use this, the person that takes user input needs to make sure that the universal bands within the bands list all have the same
            #format of the first character being a letter and not a space
            if band.startswith('n') or band.startswith('N'):  
               
                # If the method returns anything but an error, signifying that the band was selected, the code will continue normally with no exceptions
                # the command depends on the band being set, so this instruction will dynamically change
                #based on the band variable. band[1,:] removes the first value of the string, which is always the letter to just leave the band number
                send_at_command_with_retry(AT_CMD_5G_BAND_CONFIG + band[1:]) 
                print("5G band configured")
            elif band.startswith('b') or band.startswith('B'): 
                #will need to include: use AT+QNWPREFCFG= ”lte_band”,band1:band2:band
                send_at_command_with_retry(AT_CMD_LTE_BAND_CONFIG + band[1:])
                print("LTE band configured")

        except Exception as e:
            print(f"ALERT: Failed to lock {band}. Hardware/Band Error: {e}") #
            loopKPIs.append(currentKPIs) # Append the completely blank KPI readings to signify that the band wasn't reached
            continue # SKIP the rest of this loop and move to the NEXT band
            
            #IMPORTANT: Even if there is an error with the modem, the loop will continue for the entire sampling session
            #At the end, once the for loop is over, the values will be assessed, and alerts will be processed based on the data given from the exceptions
            #Will use the AlarmEvent class from models to handle this

       # --- START OF UPDATED SERVING CELL LOGIC ---
        
        # We try up to 3 times to get valid signal data
        for attempt in range(3):
            try:
                # 1. Get raw data from modem
                rawResponse = send_at_command_with_retry(AT_CMD_SERVING_CELL)
                
                # 2. Parse the string into a dictionary
                parsed_data = parse_and_validate_serving_cell_response(rawResponse)
                
                # 3. Check if values are within valid ranges (defined in your core.rules)
                is_valid = (
                    MIN_VALID_RSRP <= parsed_data.get('rsrp', 99999) <= MAX_VALID_RSRP and
                    MIN_VALID_SINR <= parsed_data.get('sinr', 99999) <= MAX_VALID_SINR and
                    MIN_VALID_RSRQ <= parsed_data.get('rsrq', 99999) <= MAX_VALID_RSRQ
                )
                
                # 4. Map parsed data to our class instance
                currentKPIs.rsrp = parsed_data.get('rsrp', 0)
                currentKPIs.sinr = parsed_data.get('sinr', 0)
                currentKPIs.rssi = parsed_data.get('rssi', 0)
                currentKPIs.rsrq = parsed_data.get('rsrq', 0)
                currentKPIs.pci  = parsed_data.get('pci', 0)
                currentKPIs.earfcn = parsed_data.get('earfcn', 0)

                if is_valid:
                    # Data is good! Break the retry loop early.
                    break
                else:
                    print(f"Attempt {attempt + 1}: Data out of range for {band}. Retrying...")
            
            except Exception as e:
                print(f"Attempt {attempt + 1}: Parsing error on {band}: {e}")
            
            # Brief pause before retrying the modem command
            time.sleep(0.2)

        # After the loop (either successful or 3 attempts reached), append the data.
        # If it failed 3 times, currentKPIs still holds the last (potentially invalid) values.
        loopKPIs.append(currentKPIs)
        
        
