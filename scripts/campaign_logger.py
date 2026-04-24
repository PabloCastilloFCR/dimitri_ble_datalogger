import asyncio
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional

from bleak import BleakScanner, BleakClient

# =====================================================
# BLE UUIDs
# =====================================================
SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214".lower()
CMD_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214"
STATUS_UUID = "19B10002-E8F2-537E-4F6C-D104768A1214"
DATA_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"
DEVICE_NAME_PREFIX = "XIAO-IMU"

OUTPUT_DIR = Path(__file__).parent.parent / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =====================================================
# Conversion Helpers
# =====================================================
def raw_to_g(raw: int, fs_g: int) -> float:
    sensitivities_mg_per_lsb = {2: 0.061, 4: 0.122, 8: 0.244, 16: 0.488}
    sens = sensitivities_mg_per_lsb.get(fs_g, 0.244)
    return raw * sens / 1000.0

def raw_to_m_s2(raw: int, fs_g: int) -> float:
    return raw_to_g(raw, fs_g) * 9.80665

# =====================================================
# Device Session
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
    ack_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    ack_task: Optional[asyncio.Task] = None

    async def ack_worker(self):
        while True:
            msg = await self.ack_queue.get()
            if msg is None: break
            try:
                await self.client.write_gatt_char(CMD_UUID, msg.encode("utf-8"), response=True)
            except Exception: pass
            finally: self.ack_queue.task_done()

    async def connect(self):
        self.client = BleakClient(self.address)
        await self.client.connect()
        self.ack_task = asyncio.create_task(self.ack_worker())
        
        def handle_data(sender, data: bytearray):
            if not data: return
            ptype = data[0]
            if ptype == 0xA0: # Header
                self.header = {
                    "axis": chr(data[1]),
                    "range_g": data[2],
                    "odr": data[3] | (data[4] << 8),
                    "sample_count": data[7] | (data[8] << 8),
                    "capture_elapsed_us": int.from_bytes(data[9:13], "little"),
                }
                asyncio.create_task(self.ack_queue.put("ACKH"))
            elif ptype == 0xA1: # Data
                p_idx = data[1] | (data[2] << 8)
                n_samples = data[3]
                self.packet_indices.add(p_idx)
                for i in range(n_samples):
                    val = int.from_bytes(data[4+i*2 : 6+i*2], "little", signed=True)
                    self.samples.append(val)
                asyncio.create_task(self.ack_queue.put(f"ACK,{p_idx}"))
            elif ptype == 0xAF: # End
                self.received_end = True
                asyncio.create_task(self.ack_queue.put("ACKEND"))
                self.done_event.set()

        await self.client.start_notify(DATA_UUID, handle_data)

    async def disconnect(self):
        if self.ack_task:
            self.ack_queue.put_nowait(None)
            await self.ack_task
        if self.client: await self.client.disconnect()

# =====================================================
# Campaign Logic
# =====================================================
async def run_capture(sessions: List[DeviceSession], axis: str, range_g: int, duration_ms: int, mode: int, freq: float):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\nStarting capture: Mode={mode}, Freq={freq}Hz, Axis={axis}...")
    
    # Send STARTAT to all
    start_cmds = [s.client.write_gatt_char(CMD_UUID, f"STARTAT,{axis},{range_g},{duration_ms},1000".encode(), response=True) for s in sessions]
    await asyncio.gather(*start_cmds)
    
    # Wait for completion
    print("Waiting for data...")
    await asyncio.gather(*(s.done_event.wait() for s in sessions))
    
    # Save files
    for s in sessions:
        filename = OUTPUT_DIR / f"{timestamp}_{s.name}_{axis}_mode{mode}_{freq}Hz.csv"
        
        elapsed_us = s.header.get("capture_elapsed_us", 0)
        dt_s = (elapsed_us / 1e6) / len(s.samples) if s.samples and elapsed_us else 0
        fs_g = s.header.get("range_g", range_g)

        with open(filename, "w", newline="", encoding="utf-8") as f:
            f.write(f"# Device: {s.name}\n")
            f.write(f"# Axis: {axis}\n")
            f.write(f"# Failure Mode: {mode}\n")
            f.write(f"# Excitation Frequency: {freq} Hz\n")
            f.write(f"# ODR: {s.header.get('odr', 0)} Hz\n")
            f.write(f"# Timestamp: {datetime.now().isoformat()}\n")
            
            writer = csv.writer(f)
            writer.writerow(["sample_idx", "time_s", f"{axis.lower()}_raw", f"{axis.lower()}_g", f"{axis.lower()}_m_s2"])
            for idx, raw in enumerate(s.samples):
                writer.writerow([idx, idx * dt_s, raw, raw_to_g(raw, fs_g), raw_to_m_s2(raw, fs_g)])
        
        print(f" Saved: {filename.name} ({len(s.samples)} samples)")
        # Reset for next run
        s.samples.clear()
        s.packet_indices.clear()
        s.done_event.clear()
        s.received_end = False

async def main():
    print("=== ML Campaign Logger ===")
    
    # Configuration
    try:
        mode = int(input("Enter Failure Mode (integer): ") or 0)
        freq = float(input("Enter Excitation Frequency (Hz): ") or 0.0)
        axis = input("Enter Axis (X/Y/Z) [X]: ").upper() or "X"
        duration = int(input("Enter Duration (ms) [1000]: ") or 1000)
        range_g = int(input("Enter Range (2/4/8/16) [8]: ") or 8)
    except ValueError as e:
        print(f"Invalid input: {e}")
        return

    print("\nScanning for XIAO-IMU devices...")
    devices = await BleakScanner.discover(timeout=5.0)
    targets = [d for d in devices if (d.name or "").startswith(DEVICE_NAME_PREFIX)]
    
    if not targets:
        print("No devices found.")
        return

    print(f"Found {len(targets)} devices. Connecting...")
    sessions = [DeviceSession(name=t.name, address=t.address) for t in targets]
    await asyncio.gather(*(s.connect() for s in sessions))
    
    try:
        while True:
            await run_capture(sessions, axis, range_g, duration, mode, freq)
            
            print("\nOptions: [Enter] Repeat | [f] Change Freq | [m] Change Mode | [a] Change Axis | [q] Quit")
            choice = input("> ").lower()
            
            if choice == 'q': break
            elif choice == 'f': freq = float(input(f"New Freq (current {freq}Hz): ") or freq)
            elif choice == 'm': mode = int(input(f"New Mode (current {mode}): ") or mode)
            elif choice == 'a': axis = input(f"New Axis (current {axis}): ").upper() or axis
    
    finally:
        print("Disconnecting...")
        await asyncio.gather(*(s.disconnect() for s in sessions))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
