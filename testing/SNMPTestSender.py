from pysnmp.hlapi import (
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    NotificationType,
    ObjectIdentity,
    Integer32,
    OctetString,
    sendNotification,
)

# Configuration for the Customer's NMS
NMS_IP = "127.0.0.1"     # change to your test bench / NMS IP - right  now it is set to run locally 
NMS_PORT = 1162      # traps usually go to 162, 1162 is a tester port, but Wireshark won't recognize it as SNMP, it will be decoded as UDP
COMMUNITY = "public"

def send_cellular_alarm(rsrp_value: int, status_msg: str) -> None:
    """Sends an SNMP v2c TRAP representing a cellular signal alarm."""

    # These OIDs would be defined in your Private MIB, im pretty sure we define our own OID for each scenario, but everything before ".12345..." has to stay the same
    OID_RSRP = "1.3.6.1.4.1.12345.1.1.0"
    OID_STATUS = "1.3.6.1.4.1.12345.1.2.0"

    iterator = sendNotification(
        SnmpEngine(),
        CommunityData(COMMUNITY, mpModel=1),          # mpModel=1 => SNMP v2c
        UdpTransportTarget((NMS_IP, NMS_PORT)),
        ContextData(),
        "trap",
        NotificationType(
            ObjectIdentity("1.3.6.1.6.3.1.1.5.3")     # linkDown (standard trap)
        ).addVarBinds(
            (OID_RSRP, Integer32(rsrp_value)),
            (OID_STATUS, OctetString(status_msg)),
        ),
    )

    errorIndication, errorStatus, errorIndex, varBinds = next(iterator)

    if errorIndication:
        print(f"Notification failed: {errorIndication}")
    elif errorStatus:
        print(f"Notification failed: {errorStatus.prettyPrint()} at {errorIndex}")
    else:
        print(f"Success: Sent Alarm (RSRP: {rsrp_value})")

if __name__ == "__main__":
    print("Simulating DAS Signal Failure...")
    send_cellular_alarm(-115, "Critical: RSRP below threshold")  # this is what is displayed in Wireshark

print("Sending to:", NMS_IP, NMS_PORT)
