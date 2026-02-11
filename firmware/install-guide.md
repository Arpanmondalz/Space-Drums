# ESP32-S3 Firmware Upload Guide

This guide provides the necessary steps to configure the Arduino IDE and upload firmware to an ESP32-S3 Dev Module.

---

## Prerequisites

*   **Arduino IDE:** Ensure you have the latest version installed.
*   **ESP32 Board Package:** 
    1.  In Arduino IDE, go to **File > Preferences**.
    2.  In "Additional Boards Manager URLs", paste: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
    3.  Go to **Tools > Board > Boards Manager**, search for "esp32" by Espressif Systems, and click install.

---

## Phase 1: Arduino IDE Configuration

To ensure the ESP32-S3 functions correctly, you must select the specific board and hardware settings. Navigate to the **Tools** menu and set the following:

*   **Board:** "ESP32S3 Dev Module"
*   **USB CDC On Boot:** Disabled
*   **Flash Mode:** QIO 80MHz
*   **Flash Size:** 8MB (Or match your specific module's capacity)
*   **Core Debug Level:** None

---

## Phase 2: Connecting the Hardware

1.  Connect your ESP32-S3 to your computer using a high-quality USB data cable.
2.  Go to **Tools > Port** and select the COM port associated with your device (e.g., COM3 or COM5).

---

## Phase 3: Entering Bootloader Mode

If the Arduino IDE fails to connect to the board automatically, you must manually put the ESP32-S3 into "Download Mode" using the physical buttons on the module.

Follow this exact sequence:

1.  **Press and hold** the **BOOT** button.
2.  While still holding BOOT, **press** the **RESET** (or EN) button.
3.  **Release** the **RESET** button.
4.  **Release** the **BOOT** button.

The board is now in firmware upload mode. 

---

## Phase 4: Uploading the Code

1.  In the Arduino IDE, click the **Upload** icon (the right-pointing arrow) or press `Ctrl + U`.
2.  Wait for the console at the bottom to show "Writing at 0x00001000..." and eventually **"Leaving... Hard resetting via RTS pin..."**
3.  Once the upload is complete, you may need to press the **RESET** button once more to start the program.

---

## Troubleshooting

*   **Port not visible:** Ensure you are using a data cable and not a charge-only cable. Try a different USB port or reinstall the CP210x or CH340 drivers if required.
*   **Upload Failed:** Ensure you followed the button sequence in Phase 3 correctly. The timing of releasing the Reset button while holding Boot is critical.
*   **Serial Monitor Blank:** Ensure the Baud Rate in the Serial Monitor matches the `Serial.begin()` value in your code (usually 115200).
