# XIAO-IMU BLE Datalogger

A professional datalogging solution for capturing high-frequency accelerometer data from XIAO-IMU (ESP32-S3/C3 based) devices over Bluetooth Low Energy (BLE).

This repository contains a GUI-based multi-device logger and several CLI utility scripts for specialized data capture tasks.

## Repository Structure

```text
dimitri_ble_datalogger/
├── data/               # Output directory for CSV captures and PNG plots
├── docs/               # Technical documentation and protocol details
│   └── FSD.md          # Functional Specification Document
├── src/                # Main application source code
│   └── main.py         # GUI Application (Tkinter) for multi-device capture
├── scripts/            # CLI Utility scripts
│   ├── campaign_logger.py    # NEW: Automated ML data collection
│   ├── ble_multiple_imu.py   # CLI version of the multi-device logger
│   ├── ble_capture_to_csv.py # Simple single-device XYZ capture
│   ├── ble_only_x_logger.py  # Optimized single-axis capture (X only)
│   ├── ble_capture_preview.py # Real-time preview utility
│   └── ble_ping_test.py      # BLE connection test script
├── requirements.txt    # Project dependencies
└── README.md           # This file
```

## Setup Instructions

### Prerequisites
- Python 3.9 or higher.
- BLE-capable computer (Internal or USB Dongle).

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/PabloCastilloFCR/dimitri_ble_datalogger.git
   cd dimitri_ble_datalogger
   ```

2. **Create a virtual environment (recommended)**:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### GUI Application (Recommended)
The main GUI application allows you to discover up to 3 XIAO-IMU devices, configure capture parameters (axis, range, duration, delay), and visualize results in real-time. 

It includes **ML Campaign** fields to tag measurements with a Failure Mode (integer) and Excitation Frequency (Hz) directly from the interface.

```bash
python src/main.py
```

### CLI Scripts
For automated or headless environments, you can use the scripts in the `scripts/` folder.

- **ML Campaign Logger**: `python scripts/campaign_logger.py` (Automated, interactive collection)
- **Multi-IMU CLI**: `python scripts/ble_multiple_imu.py`
- **Single Axis (X) Optimized**: `python scripts/ble_only_x_logger.py`

## Data Format
All captures are saved in the `data/` directory as CSV files with a machine-parseable naming convention:
`{Timestamp}_{DeviceName}_{Axis}_mode{FailureMode}_{Freq}Hz.csv`

Each file includes a **Metadata Header** (as `#` comments) containing:
- Device ID
- Capture Axis
- Failure Mode (integer)
- Excitation Frequency (Hz)
- Real-world timestamp


**CSV Columns**:
- `sample_idx`: Sequential index of the sample.
- `time_s`: Relative time in seconds.
- `*_raw`: Raw ADC value from the IMU.
- `*_g`: Acceleration in Gs.
- `*_m_s2`: Acceleration in m/s².
- `device_name`: Source device identifier.
- `odr_hz`: Output Data Rate configured.
- `capture_elapsed_us`: Actual duration of the hardware capture.

## BLE Protocol Details
The application communicates with the XIAO-IMU devices using a custom notification-based protocol:
- **Service UUID**: `19B10000-E8F2-537E-4F6C-D104768A1214`
- **Command Characteristic**: `19B10001-...` (Write)
- **Status Characteristic**: `19B10002-...` (Notify)
- **Data Characteristic**: `19B10003-...` (Notify)

**Packet Types**:
- `0xA0`: Header Packet (Configuration info).
- `0xA1`: Data Packet (Chunk of samples).
- `0xAF`: End of Session.

---
Developed by Pablo Castillo for Fraunhofer Chile Research.
