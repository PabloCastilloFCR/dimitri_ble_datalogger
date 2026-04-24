import asyncio
import csv
from datetime import datetime
import matplotlib.pyplot as plt
from bleak import BleakScanner, BleakClient

DEVICE_NAME = "XIAO-IMU"

CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
STATUS_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"
DATA_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"

# -------------------------
# Configuración del ensayo
# -------------------------
AXIS = "X"          # X, Y o Z
RANGE_G = 8         # 2, 4, 8, 16
DURATION_MS = 1000  # ms

def raw_to_g(raw: int, fs_g: int) -> float:
    sensitivities_mg_per_lsb = {
        2: 0.061,
        4: 0.122,
        8: 0.244,
        16: 0.488,
    }
    sens = sensitivities_mg_per_lsb[fs_g]
    return raw * sens / 1000.0

def raw_to_m_s2(raw: int, fs_g: int) -> float:
    return raw_to_g(raw, fs_g) * 9.80665

def parse_header(data: bytearray):
    return {
        "packet_type": data[0],
        "axis": chr(data[1]),
        "range_g": data[2],
        "odr": data[3] | (data[4] << 8),
        "duration_ms": data[5] | (data[6] << 8),
        "sample_count": data[7] | (data[8] << 8),
        "capture_elapsed_us": (
            data[9]
            | (data[10] << 8)
            | (data[11] << 16)
            | (data[12] << 24)
        ),
        "overflow_ram": data[13],
        "read_failures": data[14] | (data[15] << 8),
    }

def parse_data_packet(data: bytearray):
    packet_index = data[1] | (data[2] << 8)
    n_samples = data[3]
    values = []

    offset = 4
    for _ in range(n_samples):
        if offset + 1 >= len(data):
            break
        x = int.from_bytes(data[offset:offset+2], byteorder="little", signed=True)
        values.append(x)
        offset += 2

    return packet_index, values

async def main():
    print("Buscando dispositivo...")
    devices = await BleakScanner.discover(timeout=8.0)

    target = None
    for d in devices:
        print(f"{d.name or ''} | {d.address}")
        if (d.name or "") == DEVICE_NAME:
            target = d

    if target is None:
        print("No se encontro XIAO-IMU")
        return

    print(f"Conectando a {target.name} ({target.address})")

    header = {}
    samples = []
    packet_indices = set()
    received_end = False
    done_event = asyncio.Event()

    async with BleakClient(target.address) as client:
        print("Conectado:", client.is_connected)
        loop = asyncio.get_running_loop()

        async def send_ack(msg: str):
            await client.write_gatt_char(CMD_UUID, msg.encode("utf-8"), response=True)

        def handle_status(sender, data: bytearray):
            msg = data.decode("utf-8", errors="replace")
            print("STATUS:", msg)

        def handle_data(sender, data: bytearray):
            nonlocal received_end

            if not data:
                return

            ptype = data[0]

            if ptype == 0xA0:
                header.update(parse_header(data))
                print("HEADER:", header)
                loop.create_task(send_ack("ACKH"))

            elif ptype == 0xA1:
                packet_index, values = parse_data_packet(data)
                packet_indices.add(packet_index)
                samples.extend(values)
                loop.create_task(send_ack(f"ACK,{packet_index}"))

            elif ptype == 0xAF:
                received_end = True
                print("DATA END recibido")
                loop.create_task(send_ack("ACKEND"))
                done_event.set()

        await client.start_notify(STATUS_UUID, handle_status)
        await client.start_notify(DATA_UUID, handle_data)

        cmd = f"START,{AXIS},{RANGE_G},{DURATION_MS}"
        print("Enviando:", cmd)
        await client.write_gatt_char(CMD_UUID, cmd.encode("utf-8"), response=True)

        await asyncio.wait_for(done_event.wait(), timeout=120.0)
        await asyncio.sleep(0.5)

        await client.stop_notify(DATA_UUID)
        await client.stop_notify(STATUS_UUID)

    expected = header.get("sample_count", 0)
    capture_elapsed_us = header.get("capture_elapsed_us", 0)
    expected_packets = (expected + 2) // 3 if expected else 0
    missing_packets = sorted(set(range(expected_packets)) - packet_indices) if expected_packets else []

    print("\nResumen:")
    print("Header:", header)
    print("Muestras reconstruidas:", len(samples))
    print("END recibido:", received_end)
    print("Paquetes esperados:", expected_packets)
    print("Paquetes recibidos:", len(packet_indices))
    print("Paquetes faltantes:", missing_packets)

    # Base temporal honesta según tiempo real de captura
    if capture_elapsed_us > 0 and len(samples) > 0:
        dt_s = (capture_elapsed_us / 1e6) / len(samples)
    else:
        dt_s = 0.0

    rows = []
    fs_g = header.get("range_g", RANGE_G)
    axis_name = header.get("axis", AXIS)

    for idx, raw in enumerate(samples):
        time_s = idx * dt_s
        g_val = raw_to_g(raw, fs_g)
        ms2_val = raw_to_m_s2(raw, fs_g)
        rows.append([idx, time_s, raw, g_val, ms2_val])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"{axis_name.lower()}_capture_{timestamp}.csv"

    with open(csv_filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sample_idx", "time_s", f"{axis_name.lower()}_raw", f"{axis_name.lower()}_g", f"{axis_name.lower()}_m_s2"])
        writer.writerows(rows)

    print(f"CSV guardado: {csv_filename}")

    # Plot rápido
    if rows:
        t = [r[1] for r in rows]
        vals_ms2 = [r[4] for r in rows]

        plt.figure(figsize=(10, 4))
        plt.plot(t, vals_ms2)
        plt.xlabel("Time [s]")
        plt.ylabel(f"Acceleration {axis_name} [m/s²]")
        plt.title(
            f"{axis_name} acceleration | range=±{fs_g}g | ODR cfg={header.get('odr', 0)} Hz | N={len(samples)}"
        )
        plt.grid(True)
        plt.tight_layout()

        plot_filename = f"{axis_name.lower()}_capture_{timestamp}.png"
        plt.savefig(plot_filename, dpi=150)
        plt.show()

        print(f"Plot guardado: {plot_filename}")

if __name__ == "__main__":
    asyncio.run(main())