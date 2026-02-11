/*
 * Space Drums - ESP32-S3 Firmware
 * Compatible with Python "Master Server"
 * * INSTRUCTIONS:
 * 1. Flash this to your LEFT stick first.
 * 2. Change STICK_ID to "RIGHT" and flash your second stick.
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <WiFiUdp.h>
#include <Adafruit_LSM6DS3TRC.h>
#include <Wire.h>

// ==================== STICK IDENTITY ====================
#define STICK_ID "Right"  // Options: "LEFT" or "RIGHT"

// ==================== PIN CONFIGURATION ====================
const int PIN_SDA  = 7;
const int PIN_SCL  = 8;
const int PIN_INT1 = 10;

// ==================== NETWORK CONFIGURATION ====================
const uint16_t UDP_DISCOVERY_PORT = 5555; // Listening for Server Broadcast
const uint16_t UDP_HIT_PORT       = 5556; // Sending Hits to Server

// ==================== HIT DETECTION TUNING ====================
// (Your tuned values - DO NOT TOUCH)
const float    MIN_DOWN_VELOCITY  = -0.6f;
const float    IMPACT_ACCEL_MS2   = -14.0f;
const uint32_t HIT_COOLDOWN_MS    = 80;
const float    VELOCITY_DECAY     = 0.94f;
const float    GRAVITY_MS2        = 9.81f;

// ==================== CALIBRATION ====================
const uint32_t CALIBRATION_DELAY_MS = 500;
const uint32_t CALIBRATION_TIME_MS  = 2000;

// ==================== OBJECTS ====================
Adafruit_LSM6DS3TRC imu;
WiFiUDP udpRx; // For receiving broadcast
WiFiUDP udpTx; // For sending hits

// ==================== STATE ====================
volatile bool isCalibrated = false;
float gravityZ_g = 1.0f;

float velocityZ = 0.0f;
unsigned long lastSampleMicros = 0;
unsigned long lastHitMs = 0;

IPAddress serverIP;
volatile bool serverFound = false;
unsigned long lastServerSeen = 0;

QueueHandle_t hitQueue;

// ==================== CALIBRATION ROUTINE ====================
void calibrateGravity() {
    Serial.println("\n[CAL] Hold still...");
    delay(CALIBRATION_DELAY_MS);

    float sumZ = 0.0f;
    int samples = 0;
    unsigned long start = millis();
    sensors_event_t accel, gyro, temp;

    while (millis() - start < CALIBRATION_TIME_MS) {
        if (imu.getEvent(&accel, &gyro, &temp)) {
            sumZ += accel.acceleration.z;
            samples++;
        }
        delayMicroseconds(500);
    }

    if (samples > 100) {
        gravityZ_g = (sumZ / samples) / GRAVITY_MS2;
        gravityZ_g = constrain(gravityZ_g, -1.1f, 1.1f);
        isCalibrated = true;
        Serial.printf("[CAL] Gravity Z = %.3f g (%d samples)\n", gravityZ_g, samples);
    } else {
        Serial.println("[CAL] Failed, using default gravity");
        gravityZ_g = 1.0f;
        isCalibrated = true;
    }
}

// ==================== SENSOR TASK ====================
void imuTask(void* param) {
    sensors_event_t accel, gyro, temp;
    while (!isCalibrated) vTaskDelay(10);

    lastSampleMicros = micros();

    while (true) {
        if (imu.getEvent(&accel, &gyro, &temp)) {
            unsigned long now = micros();
            float dt = (now - lastSampleMicros) * 1e-6f;
            lastSampleMicros = now;
            
            // Filter crazy time jumps
            if (dt <= 0 || dt > 0.05f) continue;

            // Remove Gravity
            float accelZ_g = accel.acceleration.z / GRAVITY_MS2;
            float accelZ_linear_g = accelZ_g - gravityZ_g;
            float accelZ_ms2 = accelZ_linear_g * GRAVITY_MS2;

            // Velocity Integration
            velocityZ += accelZ_ms2 * dt;
            velocityZ *= VELOCITY_DECAY;

            // Hit Logic
            unsigned long nowMs = millis();
            bool strongImpact = accelZ_ms2 < IMPACT_ACCEL_MS2;
            bool movingDownFast = velocityZ < MIN_DOWN_VELOCITY;
            bool notSwingUp = accelZ_ms2 < 0.0f;   
            bool cooldownOK = (nowMs - lastHitMs) > HIT_COOLDOWN_MS;

            if (strongImpact && movingDownFast && notSwingUp && cooldownOK) {
                lastHitMs = nowMs;
                velocityZ = 0.0f;
                
                // Signal the Network Task
                uint8_t hit = 1;
                xQueueSend(hitQueue, &hit, 0);
                
                // Debug Print
                Serial.printf(">>> HIT (%s) <<<\n", STICK_ID);
            }
        }
        vTaskDelay(1); // Yield to prevent watchdog crash
    }
}

// ==================== NETWORK TASK ====================
void networkTask(void* param) {
    udpRx.begin(UDP_DISCOVERY_PORT);
    udpTx.begin(UDP_HIT_PORT);

    Serial.printf("[NET] Listening for Python Server on Port %d...\n", UDP_DISCOVERY_PORT);
    char rxBuf[64];

    while (true) {
        // 1. LISTEN FOR SERVER BROADCAST
        int size = udpRx.parsePacket();
        if (size > 0) {
            int len = udpRx.read(rxBuf, sizeof(rxBuf) - 1);
            if (len > 0) {
                rxBuf[len] = '\0';
                // Check if the packet is from our Python script
                if (strncmp(rxBuf, "AIRDRUM_SERVER", 14) == 0) {
                    serverIP = udpRx.remoteIP();
                    if (!serverFound) {
                        Serial.printf("[NET] FOUND SERVER AT: %s\n", serverIP.toString().c_str());
                    }
                    serverFound = true;
                    lastServerSeen = millis();
                }
            }
        }

        // 2. CHECK CONNECTION HEALTH
        if (serverFound && millis() - lastServerSeen > 5000) {
            serverFound = false;
            Serial.println("[NET] Lost connection to server");
        }

        // 3. SEND HITS FROM QUEUE
        uint8_t hit;
        while (xQueueReceive(hitQueue, &hit, 0) == pdTRUE) {
            if (serverFound) {
                udpTx.beginPacket(serverIP, UDP_HIT_PORT);
                
                // --- THE CRITICAL UPDATE ---
                // Sends "HIT:LEFT" or "HIT:RIGHT"
                udpTx.print("HIT:");
                udpTx.print(STICK_ID); 
                
                udpTx.endPacket();
            } else {
                Serial.println("[NET] Hit ignored - No Server Found");
            }
        }
        vTaskDelay(1);
    }
}

// ==================== SETUP ====================
void setup() {
    Serial.begin(115200);
    delay(1000);

    // I2C Init
    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setClock(400000);

    // IMU Init
    if (!imu.begin_I2C()) {
        Serial.println("[IMU] HARDWARE ERROR - CHECK WIRING");
        while (1) delay(1000);
    }
    
    // IMU Settings (High Speed)
    imu.setAccelRange(LSM6DS_ACCEL_RANGE_8_G);
    imu.setAccelDataRate(LSM6DS_RATE_833_HZ);
    imu.setGyroRange(LSM6DS_GYRO_RANGE_1000_DPS);
    imu.setGyroDataRate(LSM6DS_RATE_833_HZ);

    // WiFi Init (Auto-Connect Portal)
    WiFiManager wm;
    // wm.resetSettings(); // Uncomment if you need to reset WiFi credentials
    if (!wm.autoConnect("AirDrums-Stick", "airdrums123")) {
        ESP.restart();
    }

    // Task & Queue Init
    hitQueue = xQueueCreate(16, sizeof(uint8_t));
    calibrateGravity(); // Get initial orientation

    // Multithreading: 
    // Core 1 = Sensor Fusion (Fast)
    // Core 0 = WiFi/Network (Slow)
    xTaskCreatePinnedToCore(imuTask, "IMU", 4096, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(networkTask, "NET", 4096, NULL, 1, NULL, 0);

    Serial.printf("[SYSTEM] %s Stick Ready!\n", STICK_ID);
}

void loop() {
    vTaskDelay(pdMS_TO_TICKS(1000)); // Nothing to do here
}