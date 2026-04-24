import asyncio
from bleak import BleakScanner, BleakClient

DEVICE_NAME = "XIAO-PING"
CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
RESP_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"

async def main():
    print("Buscando dispositivo...")
    devices = await BleakScanner.discover(timeout=8.0)

    target = None
    for d in devices:
        print(f"{d.name} | {d.address}")
        if (d.name or "") == DEVICE_NAME:
            target = d

    if target is None:
        print("No se encontro XIAO-PING")
        return

    print(f"Conectando a {target.name} ({target.address})")

    async with BleakClient(target.address) as client:
        print("Conectado:", client.is_connected)

        def handle_notify(sender, data: bytearray):
            try:
                print("NOTIFY:", data.decode("utf-8"))
            except Exception:
                print("NOTIFY RAW:", data)

        await client.start_notify(RESP_UUID, handle_notify)

        print("Enviando PING")
        await client.write_gatt_char(CMD_UUID, b"PING", response=True)

        await asyncio.sleep(2.0)
        await client.stop_notify(RESP_UUID)

if __name__ == "__main__":
    asyncio.run(main())