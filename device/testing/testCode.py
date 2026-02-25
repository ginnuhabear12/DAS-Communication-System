import time # Assuming you'll use this for the web server
from datetime import datetime, timedelta

# --- Utility Functions ---



def getSleepTime(): #Maybe 
    test = 3
    print(test)

#numBands will likely be configured within the user input, so this will need to be scaled whenever we get a user input constant/class
#bands will also be configured within user input, so this will also be scaled to whatever list of bands is saved
def instKPIcollection(numBands: int, bands: list, startTime: datetime):
    # 
    for i in range(numBands):
        # Call at_command_comms method and use the following instructions: 
        #
        k = 3
    

def mainKPI():

    while True:
    
        endTime = datetime.now() + timedelta(minutes = 30)
        print(endTime.isoformat())

        for i in range(5):
            #initiate KPI collection
            startTime = datetime.now()

            # 1. Call at_command_comms 

            #


        

if __name__ == "__main__":
    # This is the ONLY part that actually starts running
    mainKPI()
















