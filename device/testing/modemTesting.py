import serial
import time

PORT = '/dev/ttyUSB2'
BAUD = 115200

# Set a hardware-level timeout as a backup
#This creates an instance of serial that communicates with the specific port set in the parameters. Timeout is set to 0.1
#to state that after the 0.1 seconds, the code will continue even if the code is still waiting for an instruction to end when
#using a ser. function.
ser = serial.Serial(PORT, BAUD, timeout=0.1)

#function for using AT commands in python. Call this function by inputing the AT command and timeout from AT command pdf
# and you will receive the response in string format
def at_command_comms(command, timeout):
    #clear the serial data so that the only data read is the info in the port being used in ser.
    ser.reset_input_buffer()
    #input AT command
    ser.write((command + "\r\n").encode())
    
    start_time = time.time()
    full_response = ""
    
    print(f"--- Sending: {command} (Waiting up to {timeout}s) ---")
    
    while (time.time() - start_time) < timeout:
        #if there are more than 0 bytes in the serial RAM (return data from AT commands), then proceed with the following code
        if ser.in_waiting > 0:
            # Read everything available and append it
            # Since the ser.in_waiting results in data, that is what will be read and decoded to string
            # .decode converts the received data from AT commands from Bytes to string by using the utf-8 look-up table conversions
            new_data = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
            full_response += new_data
            
            # Print live so you see the scan results as they come in. This is done by the flush=True
            print(new_data, end="")
            
            # Stop ONLY when we see the final status from the modem
            if "OK" in full_response or "ERROR" in full_response:
                break
        
        time.sleep(0.05) # Check the "mailbox" every 100ms
        
    return full_response

def main():
    # 1. Power on full functionality
    at_command_comms("AT+CFUN=1", 2)
    
    # 2. Scan for BOTH LTE and 5G (Parameter 3)
    # This can take 30-60 seconds, so we set a long timeout
    results = at_command_comms("AT+QSCAN=3", 60)
    
    print("\n--- Scan Complete ---")

#This is for safety. __main__ is attributed to the file that is being ran. This means that other python files or scripts won't
#accidentally start running along with this one.
if __name__ == "__main__":
    main()