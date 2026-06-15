"""
Module: modem.py
Purpose: Manages the serial connection to the Quectel modem and provides the
         core AT command interface used by all other modules that need modem data.
         All higher-level KPI collection and modem control logic calls through here.
"""
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


# ══════════════════════════════════════════════════════════════════════════════
# Serial Port Initialization
# Opens the serial connection to the modem at startup.
# PORT and BAUD must match the modem's USB serial interface configuration.
# ══════════════════════════════════════════════════════════════════════════════

PORT = '/dev/ttyUSB2'  # USB serial port the Quectel modem is mapped to on this device
BAUD = 115200          # Baud rate matching the modem's default serial interface speed
# Set a hardware-level timeout as a backup
#This creates an instance of serial that communicates with the specific port set in the parameters. Timeout is set to 0.1
#to state that after the 0.1 seconds, the code will continue even if the code is still waiting for an instruction to end when
#using a ser. function.
try:
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
except serial.SerialException as e:
    # Port not available at startup — ser is set to None, so the rest of
    # the module loads. at_command_comms will raise SerialException on
    # first use, which send_at_command_with_retry catches and routes to
    # the USB detection and restart logic in kpi_collection.py.  ← stale
    print(f"{_ts()} [MODEM] WARNING: Could not open {PORT} at startup: {e} — "
          f"modem may not be connected.")
    ser = None

# ══════════════════════════════════════════════════════════════════════════════
# AT Command Interface
# ══════════════════════════════════════════════════════════════════════════════
#function for using AT commands in python. Call this function by inputing the AT command and timeout from AT command pdf
# and you will receive the response in string format
def at_command_comms(command, timeout):
 """
    Send a single AT command to the modem and return its response as a string.

    This is the sole entry point for all modem communication in the system.
    All other modules (KPI collection, band switching, modem control) call
    this function rather than writing to the serial port directly.

    How it works:
        1. Clears the serial input buffer to discard any stale data.
        2. Writes the AT command followed by CR+LF (required by modem spec).
        3. Polls the serial buffer in a tight loop until the modem returns
           a final status line ("OK" or "ERROR"), or the timeout expires.
        4. Returns a cleaned string, "ERROR", or "TIMEOUT" depending on outcome.

    Args:
        command (str): The AT command string to send (e.g. 'AT+QENG="servingcell"').
                       Defined as constants in constants.py — do not hardcode here.
        timeout (int/float): Maximum seconds to wait for a complete modem response.
                             Refer to the Quectel AT command specification for
                             recommended timeout values per command.

    Returns:
        str: One of three possible outcomes:
             - The modem's response payload with the trailing "OK" stripped and
               whitespace cleaned — ready for parsing by the caller.
             - "ERROR"   — modem acknowledged the command but reported a failure.
             - "TIMEOUT" — modem did not respond within the timeout window.
                           Distinguishable from a zero-payload OK response,
                           which would previously both return an empty string.
    """

    # Guard: if the port failed to open at startup, raise immediately so the
    # caller's retry logic can handle it rather than silently doing nothing.
    if ser is None:
        raise serial.SerialException(f"Serial port {PORT} was not available at startup.")
    
    # Flush any leftover bytes from previous commands so the response buffer
    # only contains data from this specific command invocation.
    ser.reset_input_buffer()
    #input AT command
    ser.write((command + "\r\n").encode())
    
    start_time = time.time()
    full_response = ""
    
    print(f"{_ts()} --- Sending: {command} (Waiting up to {timeout}s) ---")


    # Poll the serial buffer repeatedly until the modem sends a terminal status
    # line or the timeout expires. The modem sends responses in chunks, so we
    # accumulate them into full_response rather than reading once and returning.
    while (time.time() - start_time) < timeout:
        #if there are more than 0 bytes in the serial RAM (return data from AT commands), then proceed with the following code
        if ser.in_waiting > 0:
            # Read everything available and append it
            # Since the ser.in_waiting results in data, that is what will be read and decoded to string
            # .decode converts the received data from AT commands from Bytes to string by using the utf-8 look-up table conversions
            new_data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
            full_response += new_data
            
          
            
            # "OK" and "ERROR" are the modem's terminal response markers.
            # Once either appears, the full response has been received and we
            # can stop polling rather than waiting for the timeout to expire.
            if "OK" in full_response or "ERROR" in full_response:
                break
        
        # Brief sleep between polls to avoid burning CPU while waiting.
        # 50ms interval is short enough to not miss fast modem responses.
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
