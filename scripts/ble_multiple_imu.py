import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional

import matplotlib.pyplot as plt
from bleak import BleakScanner, BleakClient

# =====================================================
# UUIDs BLE
# =====================================================
SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214".lower()
CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
STATUS_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"
DATA_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"

# =====================================================
# Descubrimiento
# =====================================================
DEVICE_NAME_PREFIX = "XIAO-IMU"
MIN_DEVICES = 1
MAX_DEVICES = 3

# =====================================================
# Configuración del ensayo
# =====================================================
AXIS = "X"              # X, Y o Z
RANGE_G = 8             # 2, 4, 8, 16
DURATION_MS = 1000      # duración captura
START_DELAY_MS = 4000   # retardo para sincronizar arranque
SESSION_TIMEOUT_S = 180.0

# =====================================================
# Salida
# =====================================================
OUTPUT_DIR = Path(__file__).parent.parent / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================
# Conversión de unidades
# =====================================================
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


# =====================================================
# Parsing protocolo
# =====================================================
def parse_header(data: bytearray) -> Dict:
    if len(data) < 16:
        raise ValueError(f"Header demasiado corto: {len(data)} bytes")

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
    if len(data) < 4:
        raise ValueError(f"Paquete A1 demasiado corto: {len(data)} bytes")

    packet_index = data[1] | (data[2] << 8)
    n_samples = data[3]
    values = []

    offset = 4
    for _ in range(n_samples):
        if offset + 1 >= len(data):
            break
        x = int.from_bytes(data[offset:offset + 2], byteorder="little", signed=True)
        values.append(x)
        offset += 2

    return packet_index, values


# =====================================================
# Sesión por dispositivo
# =====================================================
@dataclass
class DeviceSession:
    name: str
    address: str
    client: Optional[BleakClient] = None

    header: Dict = field(default_factory=dict)
    samples: List[int] = field(default_factory=list)
    packet_indices: Set[int] = field(default_factory=set)
    received_end: bool = False
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_messages: List[str] = field(default_factory=list)

    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ack_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    ack_task: Optional[asyncio.Task] = None

    async def safe_write_cmd(self, msg: str, response: bool = True):
        if self.client is None or not self.client.is_connected:
            raise RuntimeError(f"[{self.name}] Cliente BLE no conectado")

        async with self.write_lock:
            await self.client.write_gatt_char(
                CMD_UUID,
                msg.encode("utf-8"),
                response=response
            )

    async def ack_worker(self):
        while True:
            msg = await self.ack_queue.get()
            if msg is None:
                self.ack_queue.task_done()
                break

            try:
                await self.safe_write_cmd(msg, response=True)
            except Exception as e:
                print(f"[{self.name}] Error enviando ACK '{msg}': {e}")
            finally:
                self.ack_queue.task_done()

    async def connect(self):
        self.client = BleakClient(self.address)
        await self.client.connect()

        if not self.client.is_connected:
            raise RuntimeError(f"No fue posible conectar con {self.name} ({self.address})")

        self.ack_task = asyncio.create_task(self.ack_worker())
        loop = asyncio.get_running_loop()

        def handle_status(sender, data: bytearray):
            msg = data.decode("utf-8", errors="replace")
            self.status_messages.append(msg)
            print(f"[{self.name}] STATUS: {msg}")

        def handle_data(sender, data: bytearray):
            if not data:
                return

            ptype = data[0]

            try:
                if ptype == 0xA0:
                    self.header = parse_header(data)
                    print(f"[{self.name}] HEADER: {self.header}")
                    loop.create_task(self.ack_queue.put("ACKH"))

                elif ptype == 0xA1:
                    packet_index, values = parse_data_packet(data)
                    self.packet_indices.add(packet_index)
                    self.samples.extend(values)
                    loop.create_task(self.ack_queue.put(f"ACK,{packet_index}"))

                elif ptype == 0xAF:
                    self.received_end = True
                    print(f"[{self.name}] DATA END recibido")
                    loop.create_task(self.ack_queue.put("ACKEND"))
                    self.done_event.set()

                else:
                    print(f"[{self.name}] Tipo de paquete desconocido: 0x{ptype:02X}")

            except Exception as e:
                print(f"[{self.name}] Error procesando paquete: {e}")

        await self.client.start_notify(STATUS_UUID, handle_status)
        await self.client.start_notify(DATA_UUID, handle_data)

    async def disconnect(self):
        if self.client is not None:
            try:
                await self.client.stop_notify(DATA_UUID)
            except Exception:
                pass

            try:
                await self.client.stop_notify(STATUS_UUID)
            except Exception:
                pass

        if self.ack_task is not None:
            try:
                await self.ack_queue.put(None)
                await self.ack_task
            except Exception:
                pass

        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def send_startat(self, axis: str, range_g: int, duration_ms: int, delay_ms: int):
        self.header.clear()
        self.samples.clear()
        self.packet_indices.clear()
        self.received_end = False
        self.status_messages.clear()
        self.done_event = asyncio.Event()

        cmd = f"STARTAT,{axis},{range_g},{duration_ms},{delay_ms}"
        print(f"[{self.name}] Enviando comando: {cmd}")
        await self.safe_write_cmd(cmd, response=True)

    async def wait_until_done(self, timeout_s: float):
        await asyncio.wait_for(self.done_event.wait(), timeout=timeout_s)

    def expected_packet_count(self) -> int:
        expected_samples = self.header.get("sample_count", 0)
        return (expected_samples + 7) // 8 if expected_samples else 0

    def missing_packets(self) -> List[int]:
        expected = self.expected_packet_count()
        if expected == 0:
            return []
        return sorted(set(range(expected)) - self.packet_indices)

    def build_rows(self):
        capture_elapsed_us = self.header.get("capture_elapsed_us", 0)
        fs_g = self.header.get("range_g", RANGE_G)
        axis_name = self.header.get("axis", AXIS)

        if capture_elapsed_us > 0 and len(self.samples) > 0:
            dt_s = (capture_elapsed_us / 1e6) / len(self.samples)
        else:
            dt_s = 0.0

        rows = []
        for idx, raw in enumerate(self.samples):
            time_s = idx * dt_s
            g_val = raw_to_g(raw, fs_g)
            ms2_val = raw_to_m_s2(raw, fs_g)
            rows.append([
                idx,
                time_s,
                raw,
                g_val,
                ms2_val,
                self.name,
                self.address,
                self.header.get("odr", 0),
                self.header.get("capture_elapsed_us", 0),
                self.header.get("read_failures", 0),
                self.header.get("overflow_ram", 0),
            ])

        return rows, axis_name, fs_g

    def save_csv(self, timestamp: str):
        rows, axis_name, _ = self.build_rows()
        csv_filename = OUTPUT_DIR / f"{self.name}_{axis_name.lower()}_{timestamp}.csv"

        with open(csv_filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "sample_idx",
                "time_s",
                f"{axis_name.lower()}_raw",
                f"{axis_name.lower()}_g",
                f"{axis_name.lower()}_m_s2",
                "device_name",
                "ble_address",
                "odr_hz",
                "capture_elapsed_us",
                "read_failures",
                "overflow_ram",
            ])
            writer.writerows(rows)

        print(f"[{self.name}] CSV guardado: {csv_filename}")

    def save_plot(self, timestamp: str):
        rows, axis_name, fs_g = self.build_rows()

        if not rows:
            print(f"[{self.name}] Sin datos para graficar.")
            return

        t = [r[1] for r in rows]
        vals_ms2 = [r[4] for r in rows]

        plt.figure(figsize=(10, 4))
        plt.plot(t, vals_ms2)
        plt.xlabel("Time [s]")
        plt.ylabel(f"Acceleration {axis_name} [m/s²]")
        plt.title(
            f"{self.name} | {axis_name} acceleration | "
            f"range=±{fs_g}g | ODR={self.header.get('odr', 0)} Hz | N={len(rows)}"
        )
        plt.grid(True)
        plt.tight_layout()

        plot_filename = OUTPUT_DIR / f"{self.name}_{axis_name.lower()}_{timestamp}.png"
        plt.savefig(plot_filename, dpi=150)
        plt.close()

        print(f"[{self.name}] Plot guardado: {plot_filename}")


# =====================================================
# Descubrimiento de dispositivos
# =====================================================
async def discover_target_devices(timeout: float = 10.0) -> List[DeviceSession]:
    print("Buscando dispositivos BLE...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    sessions = []
    seen_addresses = set()
    unnamed_count = 0

    for address, (device, adv) in devices.items():
        name = device.name or adv.local_name or ""
        service_uuids = [s.lower() for s in (adv.service_uuids or [])]

        print(
            f"Detectado: name='{name}' | address={device.address} | "
            f"services={service_uuids}"
        )

        is_target = False

        if SERVICE_UUID in service_uuids:
            is_target = True
        elif name.startswith(DEVICE_NAME_PREFIX):
            is_target = True

        if not is_target:
            continue

        if device.address in seen_addresses:
            continue

        seen_addresses.add(device.address)

        if not name:
            unnamed_count += 1
            name = f"XIAO-IMU-UNKNOWN-{unnamed_count}"

        sessions.append(DeviceSession(name=name, address=device.address))

    if len(sessions) < MIN_DEVICES:
        raise RuntimeError("No se encontró ningún dispositivo XIAO-IMU disponible.")

    sessions = sessions[:MAX_DEVICES]

    print(f"\nSe encontraron {len(sessions)} dispositivo(s) utilizables:")
    for s in sessions:
        print(f"  - {s.name} | {s.address}")

    return sessions


# =====================================================
# Plot comparativo
# =====================================================
def save_comparison_plot(sessions: List[DeviceSession], timestamp: str):
    valid = []
    for s in sessions:
        rows, axis_name, _ = s.build_rows()
        if rows:
            valid.append((s.name, axis_name, rows))

    if len(valid) < 2:
        print("No hay suficientes datos para el plot comparativo.")
        return

    plt.figure(figsize=(12, 5))
    for device_name, axis_name, rows in valid:
        t = [r[1] for r in rows]
        vals_ms2 = [r[4] for r in rows]
        plt.plot(t, vals_ms2, label=device_name)

    plt.xlabel("Time [s]")
    plt.ylabel(f"Acceleration {valid[0][1]} [m/s²]")
    plt.title("Comparación de aceleración entre dispositivos")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()

    filename = OUTPUT_DIR / f"comparison_{valid[0][1].lower()}_{timestamp}.png"
    plt.savefig(filename, dpi=150)
    plt.close()

    print(f"[COMPARE] Plot comparativo guardado: {filename}")


# =====================================================
# Main
# =====================================================
async def main():
    sessions = []

    try:
        sessions = await discover_target_devices(timeout=10.0)

        if not sessions:
            print("No se encontraron dispositivos utilizables.")
            return

        print(f"\nConectando a {len(sessions)} dispositivo(s) en paralelo...")
        await asyncio.gather(*(s.connect() for s in sessions))
        print("Dispositivos conectados.")

        print(f"\nEnviando STARTAT a {len(sessions)} dispositivo(s)...")
        await asyncio.gather(*(
            s.send_startat(
                axis=AXIS,
                range_g=RANGE_G,
                duration_ms=DURATION_MS,
                delay_ms=START_DELAY_MS
            )
            for s in sessions
        ))

        print(f"\nEsperando finalización de {len(sessions)} dispositivo(s)...")
        results = await asyncio.gather(
            *(s.wait_until_done(timeout_s=SESSION_TIMEOUT_S) for s in sessions),
            return_exceptions=True
        )

        for s, res in zip(sessions, results):
            if isinstance(res, Exception):
                print(f"[{s.name}] Timeout o error esperando datos: {res}")

        print("\nResumen por dispositivo:")
        for s in sessions:
            expected_samples = s.header.get("sample_count", 0)
            expected_packets = s.expected_packet_count()
            missing = s.missing_packets()

            print(f"\n--- {s.name} ---")
            print("Address:", s.address)
            print("Header:", s.header)
            print("Muestras reconstruidas:", len(s.samples))
            print("END recibido:", s.received_end)
            print("Paquetes esperados:", expected_packets)
            print("Paquetes recibidos:", len(s.packet_indices))
            print("Paquetes faltantes:", missing)
            print("Sample count esperado:", expected_samples)
            print("Read failures:", s.header.get("read_failures"))
            print("Overflow RAM:", s.header.get("overflow_ram"))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        print("\nGuardando CSV y plots...")
        for s in sessions:
            try:
                s.save_csv(timestamp)
            except Exception as e:
                print(f"[{s.name}] Error guardando CSV: {e}")

            try:
                s.save_plot(timestamp)
            except Exception as e:
                print(f"[{s.name}] Error guardando plot: {e}")

        if len(sessions) > 1:
            try:
                save_comparison_plot(sessions, timestamp)
            except Exception as e:
                print(f"[COMPARE] Error generando plot comparativo: {e}")

        print("\nProceso terminado.")

    except Exception as e:
        print(f"\nError en main(): {e}")

    finally:
        if sessions:
            print("\nCerrando conexiones BLE...")
            await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
            print("Conexiones cerradas.")


if __name__ == "__main__":
    asyncio.run(main())