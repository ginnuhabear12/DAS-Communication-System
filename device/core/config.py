#Class configuration for KPI values
from dataclasses import dataclass, asdict
import json
from datetime import datetime

#create class for KPis. @dataclass is a class creation shortcut that prevents needing
#to write __init__() method
@dataclass
class ServingCellKPI:
    #set data type of variables. These will need to be input when creating a new 
    #instance of a KPI ex: KPI = ServingCellKPI(band,pci,rsrp,rsrq,rssi,sinr)
    band: str
    pci: int
    rsrp: float
    rsrq: float
    rssi: float
    sinr: float
    #timestamp set to none so that a timestamp isn't needed to be input whenever 
    #a new serving cell KPI instance is being created.
    timestamp: str = None

    # self is used as the input for this method. self refers to the specific instance
    # of the class. 
    # __post_init__ is the function that is created directly after the initiation
    #of the class.
    def __post_init__(self):
        #this states that if no timestamp value of this specific instance of the class
        #is input whenever the instance is created, a value of the current time 
        #is assigned to the timestamp variable for this instance of the newly created instance
        if self.timestamp is None:
            #datetime.now() gets the current local date and time for the system
            #.isoformat() converts the time object into a string for easier reading & handling
            self.timestamp = datetime.now().isoformat()
            
    # this method converts the KPI class into a python dictionary to make conversion
    # into JSON formatting easier.
    def to_dict(self):
        return asdict(self)