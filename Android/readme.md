# SpaceDrums android app

## Features

* **Real-Time Pose Tracking:** Uses Google's MediaPipe and Android CameraX to track the user's shoulders, elbows, and wrists at 60 FPS.
* **Hardware IMU Synchronization:** Listens on UDP port `5556` for instant physical strike data from custom hardware sticks, effectively bypassing visual processing delays.
* **Dynamic Drum Zones:** The virtual drum kit (Hi-Hat, Snare, Floor Tom, Crash, Ride) anchors dynamically to the user's body position in the camera feed.
* **Low-Light Detection:** Automatically monitors camera frame gaps and alerts the user if the environment is too dim for accurate 60 FPS tracking.
* **In-App Audio Mixer:** Real-time volume control for individual drum samples using Android's `SoundPool`.

---

## The Physics Engine

The hardest problem in mixed reality is **pipeline latency**. When a physical drumstick hits the "air," the hardware IMU fires instantly over Wi-Fi. However, the camera and neural network pipeline (MediaPipe) run 50–150ms behind physical reality. 

If the app just checked the camera when the IMU fired, the sticks would visually appear mid-air, leading to inaccurate "ghost hits" on the wrong drums. 

### The Solution: Instantaneous Velocity Prediction
SpaceDrums uses a custom `DrumLogic` spatial-temporal engine to solve this entirely in software:

1. **The Flight Recorder:** The camera thread continuously logs the user's arm trajectory and timestamps it using the hardware clock (`SystemClock.uptimeMillis()`).
2. **Terminal Velocity:** When the UDP hit arrives, the engine grabs the last 50ms of visual data to calculate the exact terminal velocity and trajectory of the stick.
3. **Latency Extrapolation:** It calculates the exact millisecond delta between the IMU hardware and the camera pipeline.
4. **The Strike:** It pushes the stick's trajectory forward in time by the calculated latency (e.g., +80ms), predicting exactly where the stick *physically* landed, even if the camera hasn't seen it yet.
5. **Axis-Independent Dampening:** To prevent the stick's natural upward "rebound" from accidentally triggering cymbals, the vertical (Y) extrapolation is heavily dampened while the horizontal (X) movement remains 100% responsive for fast cross-kit fills.

---

## Tech Stack

* **Language:** Kotlin
* **UI Framework:** Jetpack Compose
* **Computer Vision:** MediaPipe Pose Landmarker (Tasks Vision)
* **Camera:** AndroidX Camera2Interop (Forced 60 FPS via hardware requests)
* **Networking:** Java `DatagramSocket` (UDP Multicast & Broadcast)
* **Audio:** Android `SoundPool`

---

## Setup & Requirements

### Software
1. Open the project in **Android Studio**.
2. Sync Gradle dependencies (ensure MediaPipe and CameraX are fetched).
3. Run on a physical Android device (Emulators cannot handle the required camera framerates or UDP network syncing reliably).

### Hardware Context
*This application is designed to pair with external IMU-based hardware.*
* The app acts as the **Server**.
* It expects UDP packets formatted as string commands (`"LEFT"`, `"RIGHT"`, `"KICK"`) broadcasted over the local Wi-Fi network to port `5556`.
* Discovery broadcasts are sent aggressively on port `5555` to help the hardware find the phone's IP address.

