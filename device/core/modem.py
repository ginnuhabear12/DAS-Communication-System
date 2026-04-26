import serial
import time
from datetime import datetime
import re

# ═══════════════════════════════════════════════════════════════════════════════
# Timestamp Helper
# ═══════════════════════════════════════════════════════════════════════════════
def _ts():
    """Return current timestamp in HH:MM:SS.mmm format."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


#AT COMMAND METHOD

PORT = '/dev/ttyUSB2'
BAUD = 115200
# Set a hardware-level timeout as a backup
#This creates an instance of serial that communicates with the specific port set in the parameters. Timeout is set to 0.1
#to state that after the 0.1 seconds, the code will continue even if the code is still waiting for an instruction to end when
#using a ser. function.
try:
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
except serial.SerialException as e:
    # Port not available at startup — ser is set to None so the rest of
    # the module loads. at_command_comms will raise SerialException on
    # first use, which send_at_command_with_retry catches and routes to
    # the USB detection and restart logic in kpi_collection.py.  ← stale
    print(f"{_ts()} [MODEM] WARNING: Could not open {PORT} at startup: {e} — "
          f"modem may not be connected.")
    ser = None

#function for using AT commands in python. Call this function by inputing the AT command and timeout from AT command pdf
# and you will receive the response in string format
def at_command_comms(command, timeout):

    if ser is None:
        raise serial.SerialException(f"Serial port {PORT} was not available at startup.")
    #clear the serial data so that the only data read is the info in the port being used in ser.
    ser.reset_input_buffer()
    #input AT command
    ser.write((command + "\r\n").encode())
    
    start_time = time.time()
    full_response = ""
    
    print(f"{_ts()} --- Sending: {command} (Waiting up to {timeout}s) ---")
    
    while (time.time() - start_time) < timeout:
        #if there are more than 0 bytes in the serial RAM (return data from AT commands), then proceed with the following code
        if ser.in_waiting > 0:
            # Read everything available and append it
            # Since the ser.in_waiting results in data, that is what will be read and decoded to string
            # .decode converts the received data from AT commands from Bytes to string by using the utf-8 look-up table conversions
            new_data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
            full_response += new_data
            
          
            
            # Stop ONLY when we see the final status from the modem
            if "OK" in full_response or "ERROR" in full_response:
                break
        
        time.sleep(0.05) # Check the "mailbox" every 100ms

    # send back clean, easier to handle data
    #remove OK after a successful response and use strip() to remove spaces or indents before or after the response
    if "OK" in full_response:
        return re.sub(r'\r?\nOK\r?\n?$', '', full_response).strip()
    
    #return ERROR anyways to notify an unsuccessful AT command
    elif "ERROR" in full_response:
        return "ERROR"
    else:
        # Modem did not respond with OK or ERROR within the timeout window.
        # Returning "TIMEOUT" rather than an empty string so callers can
        # distinguish between genuine modem silence and a successful command
        # that returned only OK with no payload — both previously produced "".
        return "TIMEOUT"
