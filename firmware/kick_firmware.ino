/*
 * Space Drums - KICK MODULE (Foot)
 * Hardware: ESP32-S3 + LSM6DS3TR-C
 * Mounting: Securely to the top of the shoe (laces)
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <WiFiUdp.h>
#include <Adafruit_LSM6DS3TRC.h>
#include <Wire.h>

// ==================== IDENTITY ====================
#define STICK_ID "KICK" 

// ==================== PIN CONFIGURATION ====================
const int PIN_SDA  = 7;
const int PIN_SCL  = 8;

// ==================== NETWORK CONFIGURATION ====================
const uint16_t UDP_DISCOVERY_PORT = 5555; 
const uint16_t UDP_HIT_PORT       = 5556; 

// ==================== KICK TUNING ====================
// The threshold for a "Stomp". 
// Gravity is ~9.8m/s^2. A stomp usually exceeds 20-30m/s^2.
// We look for a sudden DELTA (change) in acceleration.
const float    STOMP_THRESHOLD_G  = 1.8f;  // Sensitivity (Lower = easier to trigger)
const uint32_t HIT_COOLDOWN_MS    = 90;    // Prevent double-triggering on one stomp

// ==================== OBJECTS ====================
Adafruit_LSM6DS3TRC imu;
WiFiUDP udpRx;
WiFiUDP udpTx;

// ==================== STATE ====================
IPAddress serverIP;
volatile bool serverFound = false;
unsigned long lastServerSeen = 0;

QueueHandle_t hitQueue;

// ==================== SENSOR TASK (CORE 1) ====================
void imuTask(void* param) {
    sensors_event_t accel, gyro, temp;
    float lastAccelZ = 0;
    unsigned long lastHitTime = 0;

    // Initialize initial reading
    if(imu.getEvent(&accel, &gyro, &temp)) {
        lastAccelZ = accel.acceleration.z;
    }

    while (true) {
        if (imu.getEvent(&accel, &gyro, &temp)) {
            float currentZ = accel.acceleration.z / 9.81f; // Convert to Gs
            
            // Calculate the "Shock" (High Pass Filter)
            // This ignores gravity and only sees sudden changes
            float shock = abs(currentZ - lastAccelZ);
            lastAccelZ = currentZ;

            unsigned long now = millis();

            // HIT DETECTION LOGIC
            // 1. Shock must be strong enough
            // 2. Must be outside the cooldown window
            if (shock > STOMP_THRESHOLD_G && (now - lastHitTime > HIT_COOLDOWN_MS)) {
                
                // Register Hit
                lastHitTime = now;
                uint8_t hit = 1;
                xQueueSend(hitQueue, &hit, 0);
                
                Serial.printf(">>> KICK (Shock: %.2fg) <<<\n", shock);
            }
        }
        // Run slightly slower than sticks, precise integration isn't needed for kicks
        vTaskDelay(2); 
    }
}

// ==================== NETWORK TASK (CORE 0) ====================
void networkTask(void* param) {
    udpRx.begin(UDP_DISCOVERY_PORT);
    udpTx.begin(UDP_HIT_PORT);

    Serial.printf("[NET] Listening for Python Server...\n");
    char rxBuf[64];

    while (true) {
        // 1. Discovery
        int size = udpRx.parsePacket();
        if (size > 0) {
            int len = udpRx.read(rxBuf, sizeof(rxBuf) - 1);
            if (len > 0) {
                rxBuf[len] = '\0';
                if (strncmp(rxBuf, "AIRDRUM_SERVER", 14) == 0) {
                    serverIP = udpRx.remoteIP();
                    if (!serverFound) Serial.printf("[NET] CONNECTED: %s\n", serverIP.toString().c_str());
                    serverFound = true;
                    lastServerSeen = millis();
                }
            }
        }

        // 2. Watchdog
        if (serverFound && millis() - lastServerSeen > 5000) {
            serverFound = false;
            Serial.println("[NET] Lost connection...");
        }

        // 3. Send Hits
        uint8_t hit;
        while (xQueueReceive(hitQueue, &hit, 0) == pdTRUE) {
            if (serverFound) {
                udpTx.beginPacket(serverIP, UDP_HIT_PORT);
                udpTx.print("HIT:");
                udpTx.print(STICK_ID); 
                udpTx.endPacket();
            }
        }
        vTaskDelay(1);
    }
}

// ==================== SETUP ====================
void setup() {
    Serial.begin(115200);
    delay(1000);

    Wire.begin(PIN_SDA, PIN_SCL);
    
    if (!imu.begin_I2C()) {
        Serial.println("IMU NOT FOUND!");
        while (1) delay(100);
    }

    // High Speed Settings
    imu.setAccelRange(LSM6DS_ACCEL_RANGE_8_G); // Higher range for foot impacts
    imu.setAccelDataRate(LSM6DS_RATE_416_HZ);  // Plenty fast for kicks

    WiFiManager wm;
    if (!wm.autoConnect("AirDrums-Kick", "airdrums123")) {
        ESP.restart();
    }

    hitQueue = xQueueCreate(10, sizeof(uint8_t));

    // Launch Tasks
    xTaskCreatePinnedToCore(imuTask, "IMU", 4096, NULL, 2, NULL, 1);
    xTaskCreatePinnedToCore(networkTask, "NET", 4096, NULL, 1, NULL, 0);
}

void loop() {
    vTaskDelay(1000);
}
