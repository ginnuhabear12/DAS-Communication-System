from pysnmp.entity import engine, config
from pysnmp.carrier.asyncore.dgram import udp
from pysnmp.entity.rfc3413 import ntfrcv

def cbFun(snmpEngine, stateReference, contextEngineId, contextName, varBinds, cbCtx):
    print("\n--- New Alarm Received ---")
    for oid, val in varBinds:
        print(f"OID: {oid.prettyPrint()} = Value: {val.prettyPrint()}")

snmpEngine = engine.SnmpEngine()

# 1) Transport: listen for traps
# Use 0.0.0.0 to accept from other devices on the network
# Use 1162 for non-root testing; change to 162 if running as sudo/admin
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 1162 # same reason as sender - eventually change to 162

config.addTransport(
    snmpEngine,
    udp.domainName,
    udp.UdpTransport().openServerMode((LISTEN_IP, LISTEN_PORT))
)

# 2) SNMP v1/v2c security name + community
# 'my-area' is the internal securityName; 'public' is the community string
config.addV1System(snmpEngine, "my-area", "public")

# 3) VACM access control (allow this community to receive notifications)
config.addVacmUser(
    snmpEngine,
    2,              # SNMPv2c (use 1 for v1)
    "my-area",
    "noAuthNoPriv",
    readSubTree=(1, 3, 6, 1),   # iso.org.dod.internet
    writeSubTree=(1, 3, 6, 1),
    notifySubTree=(1, 3, 6, 1)
)

# 4) Register notification receiver
ntfrcv.NotificationReceiver(snmpEngine, cbFun)

print(f"Trap listener live on {LISTEN_IP}:{LISTEN_PORT} ... waiting")
snmpEngine.transportDispatcher.jobStarted(1)
try:
    snmpEngine.transportDispatcher.runDispatcher()
except KeyboardInterrupt:
    snmpEngine.transportDispatcher.closeDispatcher()