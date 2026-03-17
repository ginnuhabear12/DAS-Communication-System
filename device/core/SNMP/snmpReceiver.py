import socket

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 9162

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((LISTEN_IP, LISTEN_PORT))

print(f"UDP listener active on {LISTEN_IP}:{LISTEN_PORT} ... waiting")

try:
    while True:
        data, addr = sock.recvfrom(4096)
        print(f"\n--- Packet received from {addr} ---")
        print(f"Raw bytes: {data.hex()}")
        print(f"Length: {len(data)} bytes")
except KeyboardInterrupt:
    print("Listener stopped")
    sock.close()