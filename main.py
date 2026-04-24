import asyncio
import csv
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional, Callable

import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from bleak import BleakScanner, BleakClient

# =====================================================
# BLE UUIDs
# =====================================================
SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214".lower()
CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
STATUS_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"
DATA_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"
DEVICE_NAME_PREFIX = "XIAO-IMU"

OUTPUT_DIR = Path("ble_captures_multi")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================
# Helpers
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
                response=response,
            )

    async def ack_worker(self):
        while True:
            msg = await self.ack_queue.get()
            if msg is None:
                self.ack_queue.task_done()
                break
            try:
                await self.safe_write_cmd(msg, response=True)
            finally:
                self.ack_queue.task_done()

    async def connect(self, status_cb: Callable[[str], None]):
        self.client = BleakClient(self.address)
        await self.client.connect()
        if not self.client.is_connected:
            raise RuntimeError(f"No fue posible conectar con {self.name} ({self.address})")

        self.ack_task = asyncio.create_task(self.ack_worker())
        loop = asyncio.get_running_loop()

        def handle_status(sender, data: bytearray):
            msg = data.decode("utf-8", errors="replace")
            self.status_messages.append(msg)
            status_cb(f"[{self.name}] STATUS: {msg}")

        def handle_data(sender, data: bytearray):
            if not data:
                return
            ptype = data[0]
            try:
                if ptype == 0xA0:
                    self.header = parse_header(data)
                    status_cb(f"[{self.name}] HEADER: {self.header}")
                    loop.create_task(self.ack_queue.put("ACKH"))
                elif ptype == 0xA1:
                    packet_index, values = parse_data_packet(data)
                    self.packet_indices.add(packet_index)
                    self.samples.extend(values)
                    loop.create_task(self.ack_queue.put(f"ACK,{packet_index}"))
                elif ptype == 0xAF:
                    self.received_end = True
                    status_cb(f"[{self.name}] DATA END recibido")
                    loop.create_task(self.ack_queue.put("ACKEND"))
                    self.done_event.set()
            except Exception as e:
                status_cb(f"[{self.name}] Error procesando paquete: {e}")

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

    async def send_startat(self, axis: str, range_g: int, duration_ms: int, delay_ms: int, status_cb: Callable[[str], None]):
        self.header.clear()
        self.samples.clear()
        self.packet_indices.clear()
        self.received_end = False
        self.status_messages.clear()
        self.done_event = asyncio.Event()

        cmd = f"STARTAT,{axis},{range_g},{duration_ms},{delay_ms}"
        status_cb(f"[{self.name}] Enviando comando: {cmd}")
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
        fs_g = self.header.get("range_g", 8)
        axis_name = self.header.get("axis", "X")

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
                self.header.get("odr", 0),
                self.header.get("capture_elapsed_us", 0),
            ])
        return rows, axis_name, fs_g

    def save_csv(self, timestamp: str) -> Path:
        rows, axis_name, _ = self.build_rows()
        csv_filename = OUTPUT_DIR / f"{self.name}_{axis_name.lower()}_{timestamp}.csv"
        with open(csv_filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "sample_idx", "time_s", f"{axis_name.lower()}_raw", f"{axis_name.lower()}_g", f"{axis_name.lower()}_m_s2",
                "device_name", "odr_hz", "capture_elapsed_us",
            ])
            writer.writerows(rows)
        return csv_filename


async def discover_target_devices(timeout: float = 10.0) -> List[DeviceSession]:
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    sessions = []
    seen_addresses = set()
    unnamed_count = 0

    for _, (device, adv) in devices.items():
        name = device.name or adv.local_name or ""
        service_uuids = [s.lower() for s in (adv.service_uuids or [])]

        is_target = SERVICE_UUID in service_uuids or name.startswith(DEVICE_NAME_PREFIX)
        if not is_target:
            continue
        if device.address in seen_addresses:
            continue

        seen_addresses.add(device.address)
        if not name:
            unnamed_count += 1
            name = f"XIAO-IMU-UNKNOWN-{unnamed_count}"

        sessions.append(DeviceSession(name=name, address=device.address))

    return sessions[:3]


class BleWorker:
    def __init__(self, ui_callback: Callable[[str], None], finished_callback: Callable[[List[DeviceSession], str], None]):
        self.ui_callback = ui_callback
        self.finished_callback = finished_callback
        self._thread: Optional[threading.Thread] = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def run_measurement(self, axis: str, range_g: int, duration_ms: int, delay_ms: int):
        if self._busy:
            raise RuntimeError("Ya hay una medición en curso")
        self._busy = True

        def target():
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                sessions = asyncio.run(self._async_measure(axis, range_g, duration_ms, delay_ms))
                self.finished_callback(sessions, timestamp)
            except Exception as e:
                self.ui_callback(f"ERROR global: {e}")
                self.finished_callback([], "")
            finally:
                self._busy = False

        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    async def _async_measure(self, axis: str, range_g: int, duration_ms: int, delay_ms: int) -> List[DeviceSession]:
        sessions: List[DeviceSession] = []
        try:
            self.ui_callback("Buscando dispositivos BLE...")
            sessions = await discover_target_devices(timeout=10.0)
            if not sessions:
                raise RuntimeError("No se encontró ningún XIAO-IMU disponible")

            self.ui_callback(f"Se encontraron {len(sessions)} dispositivo(s)")
            for s in sessions:
                self.ui_callback(f"  - {s.name} | {s.address}")

            self.ui_callback("Conectando dispositivos...")
            await asyncio.gather(*(s.connect(self.ui_callback) for s in sessions))
            self.ui_callback("Dispositivos conectados")

            self.ui_callback("Enviando comando STARTAT...")
            await asyncio.gather(*(s.send_startat(axis, range_g, duration_ms, delay_ms, self.ui_callback) for s in sessions))

            self.ui_callback("Esperando finalización...")
            results = await asyncio.gather(*(s.wait_until_done(timeout_s=180.0) for s in sessions), return_exceptions=True)
            for s, res in zip(sessions, results):
                if isinstance(res, Exception):
                    self.ui_callback(f"[{s.name}] Timeout o error esperando datos: {res}")

            # Pequeño drenaje para notificaciones rezagadas
            await asyncio.sleep(0.5)

            return sessions
        finally:
            if sessions:
                self.ui_callback("Cerrando conexiones BLE...")
                await asyncio.gather(*(s.disconnect() for s in sessions), return_exceptions=True)
                self.ui_callback("Conexiones cerradas")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("BLE Multi-IMU Logger")
        self.root.geometry("1300x820")

        self.worker = BleWorker(self.threadsafe_log, self.threadsafe_finished)
        self.latest_sessions: List[DeviceSession] = []

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Eje").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.axis_var = tk.StringVar(value="X")
        axis_box = ttk.Combobox(top, textvariable=self.axis_var, values=["X", "Y", "Z"], width=8, state="readonly")
        axis_box.grid(row=1, column=0, padx=5, pady=5)

        ttk.Label(top, text="Rango [g]").grid(row=0, column=1, sticky="w", padx=5, pady=5)
        self.range_var = tk.StringVar(value="8")
        range_box = ttk.Combobox(top, textvariable=self.range_var, values=["2", "4", "8", "16"], width=8, state="readonly")
        range_box.grid(row=1, column=1, padx=5, pady=5)

        ttk.Label(top, text="Muestreo [ms]").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        self.duration_var = tk.StringVar(value="1000")
        ttk.Entry(top, textvariable=self.duration_var, width=12).grid(row=1, column=2, padx=5, pady=5)

        ttk.Label(top, text="Desfase inicio [ms]").grid(row=0, column=3, sticky="w", padx=5, pady=5)
        self.delay_var = tk.StringVar(value="3000")
        ttk.Entry(top, textvariable=self.delay_var, width=12).grid(row=1, column=3, padx=5, pady=5)

        self.measure_btn = ttk.Button(top, text="Tomar Medición", command=self.on_measure)
        self.measure_btn.grid(row=1, column=4, padx=15, pady=5)

        self.status_var = tk.StringVar(value="Listo")
        ttk.Label(top, textvariable=self.status_var).grid(row=1, column=5, sticky="w", padx=10)

        mid = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(mid)
        right = ttk.Frame(mid)
        mid.add(left, weight=1)
        mid.add(right, weight=3)

        ttk.Label(left, text="Log de ejecución").pack(anchor="w")
        self.log_text = tk.Text(left, height=30, width=45)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(right, text="Gráfico").pack(anchor="w")
        self.fig = Figure(figsize=(8, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("Esperando medición")
        self.ax.set_xlabel("Time [s]")
        self.ax.set_ylabel("Acceleration [m/s²]")
        self.ax.grid(True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        self.summary_var = tk.StringVar(value="Sin mediciones aún")
        ttk.Label(bottom, textvariable=self.summary_var).pack(anchor="w")

    def threadsafe_log(self, msg: str):
        self.root.after(0, lambda: self._append_log(msg))

    def threadsafe_finished(self, sessions: List[DeviceSession], timestamp: str):
        self.root.after(0, lambda: self._on_measurement_finished(sessions, timestamp))

    def _append_log(self, msg: str):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.status_var.set(msg[:120])

    def on_measure(self):
        if self.worker.busy:
            messagebox.showwarning("Medición en curso", "Ya hay una medición en curso.")
            return

        try:
            axis = self.axis_var.get().strip().upper()
            range_g = int(self.range_var.get())
            duration_ms = int(self.duration_var.get())
            delay_ms = int(self.delay_var.get())

            if axis not in {"X", "Y", "Z"}:
                raise ValueError("El eje debe ser X, Y o Z")
            if range_g not in {2, 4, 8, 16}:
                raise ValueError("El rango debe ser 2, 4, 8 o 16")
            if duration_ms <= 0:
                raise ValueError("El tiempo de muestreo debe ser mayor que 0")
            if delay_ms < 0:
                raise ValueError("El desfase debe ser mayor o igual que 0")

        except Exception as e:
            messagebox.showerror("Parámetros inválidos", str(e))
            return

        self.measure_btn.config(state=tk.DISABLED)
        self.summary_var.set("Midiendo...")
        self._append_log("=" * 60)
        self._append_log(
            f"Nueva medición | axis={axis} | range=±{range_g}g | duration={duration_ms} ms | delay={delay_ms} ms"
        )

        try:
            self.worker.run_measurement(axis, range_g, duration_ms, delay_ms)
        except Exception as e:
            self.measure_btn.config(state=tk.NORMAL)
            messagebox.showerror("Error", str(e))

    def _on_measurement_finished(self, sessions: List[DeviceSession], timestamp: str):
        self.measure_btn.config(state=tk.NORMAL)
        self.latest_sessions = sessions

        if not sessions:
            self.summary_var.set("La medición falló o no encontró dispositivos")
            return

        self.ax.clear()
        plotted = 0
        summary_parts = []

        for s in sessions:
            try:
                rows, axis_name, _ = s.build_rows()
                if not rows:
                    continue

                t = [r[1] for r in rows]
                vals_ms2 = [r[4] for r in rows]
                self.ax.plot(t, vals_ms2, label=s.name)
                plotted += 1

                csv_path = s.save_csv(timestamp)
                missing = s.missing_packets()
                summary_parts.append(
                    f"{s.name}: N={len(rows)}, faltantes={len(missing)}, csv={csv_path.name}"
                )
                self._append_log(f"[{s.name}] CSV guardado: {csv_path}")
            except Exception as e:
                self._append_log(f"[{s.name}] Error postproceso: {e}")

        self.ax.set_title("Comparación de aceleración entre dispositivos")
        self.ax.set_xlabel("Time [s]")
        self.ax.set_ylabel(f"Acceleration {self.axis_var.get().upper()} [m/s²]")
        self.ax.grid(True)
        if plotted > 0:
            self.ax.legend()
        self.fig.tight_layout()
        self.canvas.draw_idle()

        if summary_parts:
            self.summary_var.set(" | ".join(summary_parts))
        else:
            self.summary_var.set("Medición completada, pero sin datos para graficar")


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
