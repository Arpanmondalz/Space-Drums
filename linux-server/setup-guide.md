# Space Drums: Linux Setup Guide

This guide details the setup for the Air Drums server on Linux:

- **Ubuntu 24.04+, Linux Mint 22+, Debian-based**
- **Fedora 39+, RHEL-based**

This version is optimized for **sub-5ms latency** using direct ALSA audio and real-time process scheduling.

---

## Prerequisites

- **OS:** Ubuntu 24.04+, Mint 22+, Fedora 39+, or newer
- **Hardware:** Webcam or smartphone, ESP32 Drumsticks
- **Network:** Computer and smartphone must be on the same WiFi network

---

## Phase 1: System Dependencies

Install required system libraries for audio (ALSA), graphics (OpenGL), and Python virtual environments.

### Debian / Ubuntu / Mint (APT)

```bash
sudo apt update
sudo apt install python3-venv python3-pip libgl1 libglib2.0-0t64 libasound2-dev libcap2-bin
```

### Fedora / RHEL (DNF)

```bash
sudo dnf install python3 python3-pip mesa-libGL glib2 alsa-lib-devel libcap
```

**Fedora Notes:**

| Debian/Ubuntu Name | Fedora Equivalent | Notes |
|---|---|---|
| `python3-venv` | `python3` | venv is included by default |
| `libgl1` | `mesa-libGL` | OpenGL rendering |
| `libglib2.0-0t64` | `glib2` | GLib library |
| `libasound2-dev` | `alsa-lib-devel` | ALSA audio headers |
| `libcap2-bin` | `libcap` | Provides `setcap` |

To verify venv works on Fedora:

```bash
python3 -m venv --help
```

---

## Phase 2: Virtual Environment Setup

To prevent dependency conflicts, install MediaPipe before other libraries.

```bash
# 1. Create project directory
mkdir -p ~/airdrums/sounds
cd ~/airdrums

# 2. Setup environment
python3 -m venv venv
source venv/bin/activate

# 3. Install core dependencies
pip install --upgrade pip setuptools wheel
pip install mediapipe==0.10.14
pip install opencv-python pygame flask flask-socketio eventlet numpy
```

---

## Phase 3: High-Performance Configuration

To achieve low latency, you must grant the Python executable permission to use real-time scheduling ("Nice" values) without running as root. **Do not run the server with sudo**, as it will break the audio connection.

Run this command to grant the necessary permissions:

```bash
sudo setcap 'cap_sys_nice=eip' $(readlink -f venv/bin/python)
```

If `setcap` is not found on Fedora:

```bash
sudo dnf install libcap
```

---

## Phase 4: Firewall and Audio Files

### 1. Open Required Ports

**Debian / Ubuntu / Mint (UFW):**

```bash
sudo ufw allow 5000/tcp   # Smartphone Video Stream
sudo ufw allow 5556/udp   # ESP32 Drum Hits
```

**Fedora / RHEL (firewalld):**

```bash
sudo firewall-cmd --permanent --add-port=5000/tcp
sudo firewall-cmd --permanent --add-port=5556/udp
sudo firewall-cmd --reload
```

### 2. Add Sound Files

Place your `.wav` files (snare, hihat, tom, crash) into the `~/airdrums/sounds/` directory.

---

## Phase 5: Running the System

1. **Navigate to the folder:**
    ```bash
    cd ~/airdrums
    ```

2. **Activate the environment (if not already active):**
    ```bash
    source venv/bin/activate
    ```

3. **Start the server:**
    ```bash
    python drums.py
    ```

4. **Verify Status:**
    The terminal should display: `[LINUX] High Priority Mode: ACTIVE`.

---

## Troubleshooting

- **Audio Error (Host is down):** This usually happens if you ran the script with `sudo`. Run the `setcap` command in Phase 3 and launch as a standard user.

- **ModuleNotFoundError (Mediapipe):** If MediaPipe fails to load, delete the `venv` folder and reinstall, ensuring MediaPipe is installed first.

- **Video Lag:** If the laptop camera struggles, edit the script to lower the `CAP_PROP_FPS` from 60 to 30.

- **Permissions:** If high priority is not active, ensure the `setcap` command was pointed at the correct Python binary inside your virtual environment:
    ```bash
    readlink -f venv/bin/python
    ```
    Re-run the `setcap` command if necessary.
