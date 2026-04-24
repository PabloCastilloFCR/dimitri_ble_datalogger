import asyncio
from bleak import BleakScanner, BleakClient

DEVICE_NAME = "XIAO-PING"
CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
RESP_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"

received_rows = []

async def main():
    print("Buscando dispositivo...")
    devices = await BleakScanner.discover(timeout=8.0)

    target = None
    for d in devices:
      name = d.name or ""
      print(f"{name} | {d.address}")
      if name == DEVICE_NAME:
          target = d

    if target is None:
        print("No se encontro XIAO-PING")
        return

    print(f"Conectando a {target.name} ({target.address})")

    async with BleakClient(target.address) as client:
        print("Conectado:", client.is_connected)

        done_event = asyncio.Event()

        def handle_notify(sender, data: bytearray):
            msg = data.decode("utf-8", errors="replace")
            print("NOTIFY:", msg)

            if msg.startswith("DATA,"):
                parts = msg.split(",")
                if len(parts) == 5:
                    idx = int(parts[1])
                    x = int(parts[2])
                    y = int(parts[3])
                    z = int(parts[4])
                    received_rows.append((idx, x, y, z))

            if msg == "END":
                done_event.set()

        await client.start_notify(RESP_UUID, handle_notify)

        cmd = "START,416,1000"
        print("Enviando:", cmd)
        await client.write_gatt_char(CMD_UUID, cmd.encode("utf-8"), response=True)

        await asyncio.wait_for(done_event.wait(), timeout=20.0)

        await client.stop_notify(RESP_UUID)

    print()
    print(f"Filas recibidas: {len(received_rows)}")
    for row in received_rows[:5]:
        print(row)

if __name__ == "__main__":
    asyncio.run(main())