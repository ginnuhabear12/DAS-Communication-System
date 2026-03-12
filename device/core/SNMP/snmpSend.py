import asyncio
from pysnmp.hlapi.v3arch.asyncio import (
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

NMS_IP = "10.231.136.163"   # laptop IP for testing - change to company server IP in production
NMS_PORT = 1162              # change to 162 in production
COMMUNITY = "public"

async def send_cellular_alarm(rsrp_value: int, status_msg: str) -> None:
    """Sends an SNMP v2c TRAP representing a cellular signal alarm."""
    snmpEngine = SnmpEngine()

    errorIndication, errorStatus, errorIndex, varBinds = await sendNotification(
        snmpEngine,
        CommunityData(COMMUNITY, mpModel=1),
        await UdpTransportTarget.create((NMS_IP, NMS_PORT)),
        ContextData(),
        "trap",
        NotificationType(
            ObjectIdentity("1.3.6.1.6.3.1.1.5.3")
        ).addVarBinds(
            ("1.3.6.1.4.1.12345.1.1.0", Integer32(rsrp_value)),
            ("1.3.6.1.4.1.12345.1.2.0", OctetString(status_msg)),
        ),
    )

    snmpEngine.closeDispatcher()

    if errorIndication:
        print(f"Notification failed: {errorIndication}")
    elif errorStatus:
        print(f"Notification failed: {errorStatus.prettyPrint()}")
    else:
        print(f"Success: Sent Alarm (RSRP: {rsrp_value})")

if __name__ == "__main__":
    print(f"Sending to: {NMS_IP}:{NMS_PORT}")
    print("Simulating DAS Signal Failure...")
    asyncio.run(send_cellular_alarm(-115, "Critical: RSRP below threshold"))