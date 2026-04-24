## dimitri_ble_datalogger — BLE Datalogger for XIAO-IMU

| Field | Value |
|-------|-------|
| **Document ID** | dimitri-ble-datalogger-FSD-v1 |
| **Platform** | Windows 10 / 11, Linux, macOS |
| **Runtime** | Python 3.9+ |
| **External deps** | `bleak`, `matplotlib`, `tkinter` |
| **Install location** | Repository directory |
| **Data location** | `data/` (CSV, PNG) |

---

## 1. Goals

**G1 — High-Frequency BLE Capture**  
Capture accelerometer data from XIAO-IMU devices at high frequency using Bluetooth Low Energy.

**G6 — ML Campaign Management**  
Automate the injection of session metadata (failure modes, frequencies) into data files to facilitate training of Machine Learning models.


**G2 — Multi-Device Synchronization**  
Support simultaneous connection and data retrieval from up to 3 devices with configurable start delays for synchronization.

**G3 — Real-Time Visualization**  
Provide a graphical interface to monitor connection status and visualize captured waveforms immediately after the session.

**G4 — Specialized CLI Tooling**  
Offer optimized command-line scripts for specific use cases (e.g., single-axis high-performance capture, connection testing).

**G5 — Standardized Data Export**  
Persist all captured data into standard CSV files with physical unit conversion (G-force and m/s²).

---

## 2. Constraints & Assumptions

- **Hardware**: Only supports devices exposing the specific XIAO-IMU BLE service (`19B10000-...`).
- **BLE Bandwidth**: High-frequency data is buffered on the device and transmitted in chunks; real-time streaming is limited by BLE MTU and connection intervals.
- **Operating System**: While the core logic is cross-platform, the GUI depends on `tkinter` and `matplotlib` (TkAgg).
- **Protocol**: Relies on a specific packet-based notification protocol (`0xA0`, `0xA1`, `0xAF`).

---

## 3. System Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                    Python Interpreter                        │
│                                                              │
│   ┌──────────────┐    ┌──────────────────┐  ┌─────────────┐  │
│   │ GUI (Tkinter)│◄──►│  BleWorker       │◄─┤ CLI Scripts │  │
│   │ (main.py)    │    │ (Asyncio Loop)   │  │ (scripts/*) │  │
│   └──────────────┘    └──────────────────┘  └─────────────┘  │
│          │                     │                             │
│          │            ┌──────────────────┐                   │
│          │            │ DeviceSession    │                   │
│          ▼            │ (ACK Management) │                   │
│   ┌──────────────┐    └──────────────────┘                   │
│   │ Matplotlib   │             │                             │
│   │ (Plotting)   │             ▼                             │
│   └──────────────┘    ┌─────────────────────────┐            │
│                       │ Subprocess/Thread: Bleak│            │
│                       │ (GATT Notify/Write)     │            │
│                       └─────────────────────────┘            │
└──────────────────────────────────────────────────────────────┘
          │                              ▲
          ▼                              │
     data/*.csv                    XIAO-IMU Device
     data/*.png                    (BLE GATT Server)
```

---

## 4. Data Structures

### 4.1 DeviceSession (in-memory)
| Field | Type | Notes |
|-------|------|-------|
| `name` | str | BLE Device name. |
| `address` | str | BLE MAC/UUID address. |
| `header` | dict | Captured from `0xA0` packet (ODR, range, etc). |
| `samples` | list[int] | Buffer for raw 16-bit samples. |
| `packet_indices` | set[int] | Tracks received `0xA1` packets for missing packet detection. |

### 4.2 CSV Output Format
| Column | Description |
|--------|-------------|
| `sample_idx` | Sequential index. |
| `time_s` | Calculated timestamp based on ODR and elapsed time. |
| `*_raw` | Raw 16-bit signed integer. |
| `*_g` | Value converted to Gs. |
| `*_m_s2` | Value converted to m/s². |

---

## 5. Functional Requirements

### 5.1 Device Discovery
- **FR-DISC-1**: The system must scan for BLE devices filtering by name prefix (`XIAO-IMU`) or Service UUID.
- **FR-DISC-2**: Discovery timeout is configurable (default 10s).

### 5.2 Data Acquisition
- **FR-ACQ-1**: User can select Axis (X, Y, Z), Range (±2g, 4g, 8g, 16g), and Duration.
- **FR-ACQ-2**: Commands are sent via the CMD characteristic as UTF-8 strings (e.g., `STARTAT,X,8,1000,3000`).
- **FR-ACQ-3**: System must acknowledge every data packet (`0xA1`) to prevent buffer overflow on the device.
- **FR-ACQ-4**: Missing packets must be detected and reported in the session summary.
- **FR-ACQ-5**: Supports ML campaign metadata injection (Failure Mode as integer and Excitation Frequency in Hz) into both filenames and CSV headers for traceability.


### 5.3 User Interface (GUI)
- **FR-GUI-1**: Display a real-time log of BLE operations and status messages.
- **FR-GUI-2**: Interactive plots using Matplotlib to compare data from multiple sensors.
- **FR-GUI-3**: Status indicators for connection and measurement progress.

### 5.4 Specialized Utilities (CLI)
- **FR-CLI-1**: `ble_only_x_logger` provides minimal overhead for maximum sampling performance on a single axis.
- **FR-CLI-2**: `ble_ping_test` verifies connectivity and GATT characteristic accessibility.

---

## 6. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| **NFR-1** | Reliable data recovery: System must handle BLE disconnections gracefully. |
| **NFR-2** | Concurrency: Asyncio must be used to manage multiple BLE connections without blocking the UI. |
| **NFR-3** | Portability: Paths must be handled using `pathlib` for Windows/Linux compatibility. |

---

## 7. Out of Scope
- Firmware development for the XIAO-IMU devices.
- Real-time frequency analysis (FFT) during capture.
- Cloud database integration.

---

## 8. Security Considerations
- **S1**: No sensitive data is transmitted over BLE (only raw sensor data).
- **S2**: BLE communication is unencrypted (Standard for these DIY IMU protocols).

---

## 9. Tunables
| Constant | Default | Location |
|----------|---------|----------|
| `SERVICE_UUID` | `19B10000-...` | `src/main.py` |
| `TIMEOUT` | 180s | `BleWorker.run_measurement` |
| `OUTPUT_DIR` | `data/` | All scripts |

---

## 10. Future Work
- Real-time streaming mode (low frequency).
- Support for Gyroscope and Magnetometer data.
- Automated calibration routines.
