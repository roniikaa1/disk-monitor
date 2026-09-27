# Disk Monitor

A lightweight Python script to monitor local disk space usage, calculate available storage in Gigabytes, and trigger warnings when storage crosses defined thresholds.

## Features
* **Cross-Platform Storage Check:** Automatically queries total, used, and free disk space.
* **Readable Output:** Converts raw byte sizes into clean Gigabyte (GB) values with percentage calculations.
* **Threshold Alerts:** Notifies you if your storage utilization exceeds a specific limit (e.g., 80%).

## Requirements

- **Operating System**: Windows 10 / 11 (uses Windows-specific C-types APIs for Recycle Bin cleanup and file system paths).
- **Python Version**: Python 3.8 or higher.
- **Dependencies**: None! Uses only Python's standard built-in libraries:
  - `ctypes` (Interfacing with Windows Shell APIs)
  - `shutil` (Disk usage & folder operations)
  - `os` & `pathlib` (Environment variables & path management)
- **Permissions**: Administrator privileges recommended (required for cleaning system caches like `Prefetch` and `SoftwareDistribution`).


## Usage
Run the script directly from your terminal:
```bash
python monitor.py
