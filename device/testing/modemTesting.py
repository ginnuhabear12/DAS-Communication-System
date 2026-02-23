import serial
import time
from atCommandExample import at_command_comms

PORT = '/dev/ttyUSB2'
BAUD = 115200

# Set a hardware-level timeout as a backup
#This creates an instance of serial that communicates with the specific port set in the parameters. Timeout is set to 0.1
#to state that after the 0.1 seconds, the code will continue even if the code is still waiting for an instruction to end when
#using a ser. function.
ser = serial.Serial(PORT, BAUD, timeout=0.1)


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