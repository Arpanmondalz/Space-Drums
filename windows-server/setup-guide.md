# Space Drums: Windows Setup Guide

This guide details the procedure for setting up the Air Drums server on Windows 10 or 11.

---

## Prerequisites

*   **Operating System:** Windows 10 or 11.
*   **Python:** Version 3.10, 3.11, or 3.12 installed.
    *   **Important:** During installation, ensure the box **"Add Python to PATH"** is checked.
*   **Hardware:** Webcam or smartphone, and ESP32 Drumsticks.

---

## Phase 1: Project Setup

Follow these steps using PowerShell to initialize the project directory and virtual environment.

1.  **Create the directory structure:**
    ```powershell
    New-Item -ItemType Directory -Force -Path "$HOME\Documents\airdrums"
    cd "$HOME\Documents\airdrums"
    New-Item -ItemType Directory -Force -Path "sounds"
    ```

2.  **Create the Virtual Environment:**
    ```powershell
    python -m venv venv
    ```

3.  **Activate the Environment:**
    If you receive a security error, run: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` first.
    ```powershell
    .\venv\Scripts\Activate
    ```
    Once activated, your command prompt will be prefixed with `(venv)`.

---

## Phase 2: Install Libraries

Run the following command to install the required dependencies:

```bash
pip install opencv-python mediapipe pygame flask flask-socketio eventlet numpy
```

---

## Phase 3: Network and Firewall Configuration

The Windows Defender Firewall frequently blocks the server's network communication.

### Handling the Firewall Prompt
When you run the script for the first time, a Windows Defender Firewall dialog will appear.
1.  **Check both boxes:**
    *   Private networks (home or work)
    *   Public networks (airport, coffee shop)
2.  **Select:** Allow Access.

### Manual Configuration
If you did not see the popup or the connection is blocked:
1.  Search the Start Menu for **"Allow an app through Windows Firewall"**.
2.  Click **Change settings**.
3.  Locate `python.exe` in the list.
4.  Ensure both **Private** and **Public** checkboxes are selected for all Python entries.

---

## Phase 4: Finding your IP Address

To connect your phone to the server, you must identify your computer's local IP address.

1.  Open PowerShell.
2.  Type the following command:
    ```powershell
    ipconfig
    ```
3.  Locate the **IPv4 Address** under your active network adapter (typically *Wireless LAN adapter Wi-Fi*).
    *   Example: `192.168.1.15`
4.  Enter the address into your phone's browser using port 5000:
    `http://192.168.1.15:5000`

---

## Troubleshooting

### Error: ImportError: DLL load failed (OpenCV)
*   **Cause:** Missing Windows Media Feature Pack, common on Windows "N" editions.
*   **Fix:** Navigate to `Windows Settings > Apps > Optional Features` and install the **Media Feature Pack**.

### Error: Audio Crackling
*   **Cause:** The audio buffer size is too low for standard Windows drivers.
*   **Fix:** Modify the buffer setting in your Python script from `64` to `512`.
    ```python
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
    ```

### Error: Phone connects but the screen remains black
*   **Cause:** The firewall is blocking the video stream on port 5000.
*   **Fix:** Temporarily disable the Windows Firewall to confirm the cause. If the stream works, re-configure the firewall rules as detailed in Phase 3.
