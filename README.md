# Space Drums

Space Drums is an open source computer vision-based air drumming system. It utilizes a smartphone as a wireless camera to track hand positions via MediaPipe and ESP32-based drumsticks to trigger hits via UDP.

This project enables a virtual drum kit experience in mid-air with real-time audio feedback.

---

## Project Structure

The repository is organized into three primary components:

*   **linux-server/**: The optimized Python server for Linux systems. Recommended for lowest latency.
*   **windows-server/**: The standard Python server for Windows 10 and 11.
*   **firmware/**: C++ firmware for the ESP32-based drumsticks.

---

## Hardware Requirements

*   **Computer**: A laptop or desktop running Linux (Ubuntu/Mint) or Windows 10/11.
*   **Smartphone**: Any modern smartphone with a camera and web browser (used as the video source).
*   **Space Drum sticks**
*   **Network**: Both the computer and smartphone must be connected to the same WiFi network.

---

## Version Comparison

### Linux Version (Recommended)
Optimized for high performance. It utilizes direct kernel access for video (V4L2) and audio (ALSA), achieving sub-10ms audio response and superior frame rate stability. It supports high-priority process scheduling.

### Windows Version
Easier to set up using standard Windows drivers (DirectShow). While latency is slightly higher due to the Windows audio stack, it is fully functional for standard use.

---

## Installation and Setup

### 1. ESP32 Firmware
Flash the code located in the `firmware/` folder to your ESP32 boards.

1.  Open the project in the Arduino IDE or PlatformIO.
2.  Update the WiFi credentials (`SSID` and `PASSWORD`) in the source code.
3.  Flash one stick configured as **LEFT** and the other as **RIGHT** (refer to the definitions at the top of the firmware file).

### 2. Python Server Setup
Ensure Python 3.10 or newer is installed.

#### Linux Setup
Navigate to the `linux-server/` directory and run:
```bash
sudo apt-get install libgl1 libasound2-dev
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
*Note: Granting high-priority permissions to the Python executable is recommended for optimal performance.*

#### Windows Setup
Navigate to the `windows-server/` directory in PowerShell:
```powershell
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
```

### 3. Audio Files

All audio files can be downloaded from the sounds/ folder

---

## Running the System

1.  **Start the Server:**
    *   **Linux:** `venv/bin/python main.py`
    *   **Windows:** `python main.py`

2.  **Connect the Camera:**
    *   The terminal will display a local IP address (e.g., `http://192.168.1.15:5000`).
    *   Open this address in your smartphone's web browser.
    *   Select **Connect** and grant camera permissions.
    *   Position the phone camera to capture your movements.

3.  **Power on the Sticks:**
    *   Turn on the ESP32 drumsticks.
    *   Only the first time: You will need to connect to the "AirDrums-Stick" wifi network and add your wifi SSID and password for each stick
    *   They will automatically discover the server over UDP.
    *   A confirmation sound will play once both sticks are synchronized.

---

## Troubleshooting

*   **Firewall:** Ensure the computer allows incoming connections on **TCP port 5000** (Video) and **UDP port 5556** (Drum Hits).
*   **Audio Latency:**
    *   **Linux:** Ensure you are using the ALSA configuration provided in the code.
    *   **Windows:** If jitter occurs, set the Python process priority to "Realtime" in the Task Manager.
