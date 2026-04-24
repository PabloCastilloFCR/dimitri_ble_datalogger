import asyncio
import csv
from datetime import datetime
from bleak import BleakScanner, BleakClient

DEVICE_NAME = "XIAO-IMU"

CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
STATUS_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"
DATA_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"

status_messages = []
samples = []
header = {}
received_end = False
packet_indices = set()

def parse_header(data: bytearray):
    return {
        "packet_type": data[0],
        "odr": data[1] | (data[2] << 8),
        "duration_ms": data[3] | (data[4] << 8),
        "sample_count": data[5] | (data[6] << 8),
        "overflow_ram": data[7],
    }

def parse_data_packet(data: bytearray):
    packet_index = data[1] | (data[2] << 8)
    n_samples = data[3]
    rows = []

    offset = 4
    for _ in range(n_samples):
      if offset + 5 >= len(data):
          break

      x = int.from_bytes(data[offset:offset+2], byteorder="little", signed=True)
      y = int.from_bytes(data[offset+2:offset+4], byteorder="little", signed=True)
      z = int.from_bytes(data[offset+4:offset+6], byteorder="little", signed=True)
      rows.append((packet_index, x, y, z))
      offset += 6

    return packet_index, rows

async def main():
    global received_end
    done_event = asyncio.Event()

    print("Buscando dispositivo...")
    devices = await BleakScanner.discover(timeout=8.0)

    target = None
    for d in devices:
        name = d.name or ""
        print(f"{name} | {d.address}")
        if name == DEVICE_NAME:
            target = d

    if target is None:
        print("No se encontro XIAO-IMU")
        return

    print(f"Conectando a {target.name} ({target.address})")

    async with BleakClient(target.address) as client:
        print("Conectado:", client.is_connected)

        loop = asyncio.get_running_loop()

        async def send_ack(msg: str):
            await client.write_gatt_char(CMD_UUID, msg.encode("utf-8"), response=True)

        def handle_status(sender, data: bytearray):
            msg = data.decode("utf-8", errors="replace")
            status_messages.append(msg)
            print("STATUS:", msg)

        def handle_data(sender, data: bytearray):
            global received_end

            if not data:
                return

            ptype = data[0]

            if ptype == 0xA0:
                h = parse_header(data)
                header.update(h)
                print("HEADER:", header)
                loop.create_task(send_ack("ACKH"))

            elif ptype == 0xA1:
                packet_index, rows = parse_data_packet(data)
                packet_indices.add(packet_index)
                samples.extend(rows)
                loop.create_task(send_ack(f"ACK,{packet_index}"))

            elif ptype == 0xAF:
                received_end = True
                print("DATA END recibido")
                loop.create_task(send_ack("ACKEND"))
                done_event.set()

            else:
                print("Paquete desconocido:", data)

        await client.start_notify(STATUS_UUID, handle_status)
        await client.start_notify(DATA_UUID, handle_data)

        cmd = "START,416,500"
        print("Enviando:", cmd)
        await client.write_gatt_char(CMD_UUID, cmd.encode("utf-8"), response=True)

        await asyncio.wait_for(done_event.wait(), timeout=60.0)

        # margen por si llega un último estado
        await asyncio.sleep(0.5)

        await client.stop_notify(DATA_UUID)
        await client.stop_notify(STATUS_UUID)

    print()
    print("Resumen:")
    print("Header:", header)
    print("Muestras reconstruidas:", len(samples))
    print("END recibido:", received_end)

    expected = header.get("sample_count", 0)
    expected_packets = (expected + 1) // 2 if expected else 0
    print("Paquetes esperados:", expected_packets)
    print("Paquetes recibidos:", len(packet_indices))

    missing_packets = sorted(set(range(expected_packets)) - packet_indices) if expected_packets else []
    print("Paquetes faltantes:", missing_packets[:20], "..." if len(missing_packets) > 20 else "")

    odr = header.get("odr", 416)
    csv_rows = []

    for idx, (_, x, y, z) in enumerate(samples):
        time_s = idx / float(odr)
        csv_rows.append([idx, time_s, x, y, z])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"imu_capture_{timestamp}.csv"

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sample_idx", "time_s", "x_raw", "y_raw", "z_raw"])
        writer.writerows(csv_rows)

    print(f"CSV guardado: {filename}")

    if expected:
        print(f"Cobertura: {len(samples)}/{expected} = {100*len(samples)/expected:.1f}%")

if __name__ == "__main__":
    asyncio.run(main())