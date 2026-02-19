import socket
import threading
import time
import pygame
import os
import cv2
import mediapipe as mp
import numpy as np
import logging
from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO

# ================= CONFIGURATION =================
UDP_DISCOVERY_PORT = 5555
UDP_HIT_PORT = 5556
WEB_PORT = 5000
BROADCAST_INTERVAL = 1.0

# Drum Zone Layout (0.0 - 1.0)
CYMBAL_HEIGHT = 0.35
DIVIDER_1 = 0.35  # Left vs Center for Bottom (35%)
DIVIDER_2 = 0.65  # Center vs Right for Top & Bottom (65%)

# Stick Physics
STICK_EXTENSION = 1.2 

# Prediction Settings 
PREDICTION_STRENGTH = 5.0 

# Global State
current_zone_left = "SNARE"
current_zone_right = "SNARE"
previous_wrist_x = {"Left": 0, "Right": 0} 
latest_frame_from_phone = None
frame_lock = threading.Lock()

# ================= AUDIO ENGINE =================
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=64)
pygame.mixer.set_num_channels(16)

def load_sound(path):
    if os.path.exists(path): return pygame.mixer.Sound(path)
    print(f" [WARNING] Sound missing: {path}")
    return None

sounds = {
    "SNARE": load_sound("sounds/snare.wav"),
    "HI-HAT": load_sound("sounds/hihat.wav"),
    "FLOOR TOM": load_sound("sounds/tom.wav"),
    "CRASH": load_sound("sounds/crash.wav"),
    "RIDE": load_sound("sounds/ride.wav"), # <--- NEW RIDE SOUND
    "KICK": load_sound("sounds/kick.wav")  
}

def play_sound(zone):
    if zone in sounds and sounds[zone]:
        sounds[zone].play()
        print(f" > {zone}")

# ================= THREADED CAMERA CLASS =================
class WebcamStream:
    def __init__(self, src=0, width=320, height=240):
        self.stream = cv2.VideoCapture(src)
        self.stream.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.stream.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
        self.stream.set(cv2.CAP_PROP_FPS, 60)
        (self.grabbed, self.frame) = self.stream.read()
        self.stopped = False

    def start(self):
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self

    def update(self):
        while not self.stopped:
            grabbed, frame = self.stream.read()
            if not grabbed: self.stopped = True
            else:
                self.grabbed = True
                self.frame = frame

    def read(self): return self.frame
    def stop(self):
        self.stopped = True
        self.stream.release()

# ================= VISION LOGIC =================
mp_pose = mp.solutions.pose
pose = mp_pose.Pose(
    model_complexity=1, 
    min_detection_confidence=0.5, 
    min_tracking_confidence=0.5
)

def get_drum_zone(x, y):
    if y < CYMBAL_HEIGHT:
        # Top Row: Left & Center = CRASH, Right = RIDE
        if x < DIVIDER_2: return "CRASH"
        return "RIDE"
    else:
        # Bottom Row: Left = HI-HAT, Center = SNARE, Right = FLOOR TOM
        if x < DIVIDER_1: return "HI-HAT"
        if x < DIVIDER_2: return "SNARE"
        return "FLOOR TOM"

def extend_line(x1, y1, x2, y2, scale=1.0):
    return int(x2 + (x2-x1)*scale), int(y2 + (y2-y1)*scale)

def process_pose_frame(frame, mirror_mode=False):
    global current_zone_left, current_zone_right, previous_wrist_x
    
    h, w, _ = frame.shape
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = pose.process(rgb_frame)

    # Draw Zones (Updated Layout)
    c = (100, 100, 100)
    cymbal_y = int(h * CYMBAL_HEIGHT)
    div_1_x = int(w * DIVIDER_1)
    div_2_x = int(w * DIVIDER_2)

    # Horizontal divider for top/bottom
    cv2.line(frame, (0, cymbal_y), (w, cymbal_y), c, 1)
    
    # Top vertical divider (Crash vs Ride)
    cv2.line(frame, (div_2_x, 0), (div_2_x, cymbal_y), c, 1)
    
    # Bottom vertical dividers (Hi-Hat vs Snare vs Floor Tom)
    cv2.line(frame, (div_1_x, cymbal_y), (div_1_x, h), c, 1)
    cv2.line(frame, (div_2_x, cymbal_y), (div_2_x, h), c, 1)

    if results.pose_landmarks:
        lm = results.pose_landmarks.landmark
        if mirror_mode: arms = [("Right", 13, 15), ("Left", 14, 16)] 
        else: arms = [("Left", 13, 15), ("Right", 14, 16)]

        for name, idx_elb, idx_wri in arms:
            elbow = lm[idx_elb]
            wrist = lm[idx_wri]

            if wrist.visibility > 0.3:
                ex, ey = int(elbow.x * w), int(elbow.y * h)
                wx, wy = int(wrist.x * w), int(wrist.y * h)
                
                # 1. Base Tip
                tx, ty = extend_line(ex, ey, wx, wy, STICK_EXTENSION)
                
                # 2. Predictive Warp
                current_x = wrist.x
                dx = current_x - previous_wrist_x[name]
                pred_offset = int(dx * w * PREDICTION_STRENGTH)
                tx += pred_offset

                previous_wrist_x[name] = current_x

                tx = max(0, min(w, tx))
                ty = max(0, min(h, ty))

                detected_zone = get_drum_zone(tx/w, ty/h)
                
                if name == "Left": current_zone_left = detected_zone
                else: current_zone_right = detected_zone

                # Visuals
                color = (0, 255, 0)
                cv2.line(frame, (wx, wy), (tx, ty), color, 2) 
                cv2.circle(frame, (tx, ty), 6, (0, 0, 255), -1) 
                cv2.putText(frame, detected_zone[:3], (tx, ty-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    return frame

# ================= PWA SERVER =================
app = Flask(__name__)
# IMPORTANT: ping_interval set lower to keep connection snappy
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_interval=5)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Air Drums (Binary Mode)</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#000000">
    <style>
        * { box-sizing: border-box; }
        body { 
            margin: 0; 
            background-color: #000; 
            display: flex; 
            flex-direction: column;
            justify-content: center; 
            align-items: center; 
            height: 100vh; 
            width: 100vw; 
            overflow: hidden; 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            color: white; 
            user-select: none;
            -webkit-font-smoothing: antialiased;
        }
        #ui-layer {
            text-align: center;
            z-index: 10;
        }
        #start-btn {
            padding: 18px 40px; 
            font-size: 1.1rem; 
            font-weight: 600;
            color: #fff; 
            background: #ff8000;
            border: none; 
            border-radius: 50px; 
            cursor: pointer; 
            text-transform: uppercase;
            letter-spacing: 1px;
            box-shadow: 0 4px 15px rgba(255, 128, 0, 0.4);
            transition: transform 0.2s, box-shadow 0.2s;
        }
        #start-btn:active {
            transform: scale(0.95);
        }
        #status { 
            display: none; 
            animation: fadeIn 0.5s ease-out;
        }
        h3 { margin: 10px 0 5px 0; letter-spacing: 2px; }
        p { margin: 0; font-size: 0.9rem; opacity: 0.7; }

        .pulsing-circle {
            width: 60px; 
            height: 60px; 
            background-color: #ff751a; 
            border-radius: 50%;
            margin: 0 auto 20px auto; 
            position: relative;
        }
        .pulsing-circle::before, .pulsing-circle::after {
            content: '';
            position: absolute;
            left: 0; top: 0; right: 0; bottom: 0;
            border-radius: 50%;
            border: 2px solid #ff751a;
            animation: pulse 2s infinite;
        }
        .pulsing-circle::after { animation-delay: 0.5s; }

        @keyframes pulse {
            0% { transform: scale(1); opacity: 1; }
            100% { transform: scale(2.5); opacity: 0; }
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
</head>
<body>
    
    <div id="ui-layer">
        <button id="start-btn" onclick="start()">Start Stream</button>
        
        <div id="status">
            <div class="pulsing-circle"></div>
            <h3>LIVE</h3>
            <p>Camera Stream Active</p>
        </div>
    </div>

    <video id="v" autoplay playsinline muted style="display:none"></video>
    <canvas id="c" style="display:none"></canvas>

    <script>
        const s = io(); 
        const v = document.getElementById('v'); 
        const c = document.getElementById('c');
        const ctx = c.getContext('2d');
        const btn = document.getElementById('start-btn'); 
        const status = document.getElementById('status');
        
        async function start(){
            try {
                // Request Camera
                const stream = await navigator.mediaDevices.getUserMedia({
                    video: { 
                        facingMode: "environment", 
                        width: { ideal: 640 }, 
                        height: { ideal: 360 },
                        frameRate: { ideal: 30 }
                    }
                });
                
                v.srcObject = stream; 
                await v.play();
                
                // Update UI
                btn.style.display = 'none'; 
                status.style.display = 'block';
                
                // Optimize Canvas size for transmission speed
                c.width = 320; 
                c.height = 180; 

                // Start Transmission Loop (Target ~30 FPS)
                setInterval(() => {
                    ctx.drawImage(v, 0, 0, 320, 180);
                    
                    // Send as Binary Blob (Lower CPU usage than Base64)
                    c.toBlob(blob => {
                        if(blob) s.emit('frame', blob);
                    }, 'image/jpeg', 0.5); 
                    
                }, 33); 

                // Fullscreen (Mobile experience)
                if (document.documentElement.requestFullscreen) {
                    document.documentElement.requestFullscreen().catch(e => {});
                }
            } catch(e) { 
                alert("Camera Error: " + e); 
                btn.innerText = "Error: Access Denied";
                btn.style.backgroundColor = "#cc0000";
            }
        }
    </script>
</body>
</html>
"""

@app.route('/') 
def index(): return render_template_string(HTML_PAGE)

@app.route('/manifest.json') 
def m(): 
    return jsonify({
        "name": "AirDrums", "short_name": "Drums", "display": "standalone",
        "orientation": "landscape", "background_color": "#000000", "theme_color": "#000000",
        "start_url": "/", "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/2907/2907253.png", "sizes": "192x192", "type": "image/png"}]
    })

@socketio.on('frame')
def h(data):
    # CONCEPT 4: Receive Binary Data directly
    global latest_frame_from_phone
    try:
        # No Base64 decode needed. 'data' is already bytes.
        n = np.frombuffer(data, np.uint8)
        frame = cv2.imdecode(n, cv2.IMREAD_COLOR)
        
        with frame_lock: 
            latest_frame_from_phone = frame
    except Exception as e: 
        print(f"Frame Error: {e}")

def run_web(): socketio.run(app, host="0.0.0.0", port=WEB_PORT)

# ================= UDP NETWORK (UPDATED FOR KICK) =================
def udp_loops():
    t_disc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t_disc.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    t_list = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t_list.bind(("0.0.0.0", UDP_HIT_PORT))
    t_list.setblocking(False) 
    print(f" [NET] Listening on {UDP_HIT_PORT}")
    last_broadcast = 0
    while True:
        current_time = time.time()
        # 1. Discovery Broadcast
        if current_time - last_broadcast > 1.0:
            try: t_disc.sendto(b"AIRDRUM_SERVER", ("255.255.255.255", UDP_DISCOVERY_PORT))
            except: pass
            last_broadcast = current_time
        
        # 2. Receive Hits
        while True:
            try:
                data, _ = t_list.recvfrom(32)
                msg = data.decode("utf-8").upper().strip()
                
                if "KICK" in msg:
                    play_sound("KICK")
                elif "LEFT" in msg: 
                    play_sound(current_zone_left)
                elif "RIGHT" in msg: 
                    play_sound(current_zone_right)
                    
            except BlockingIOError: break 
            except Exception as e: break
        time.sleep(0.001)

# ================= MAIN =================
if __name__ == "__main__":
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close()
    except: ip="127.0.0.1"

    print("="*30)
    print(" AIR DRUMS: BINARY MODE + KICK")
    print("="*30)
    
    threading.Thread(target=udp_loops, daemon=True).start()

    mode = input(" Mode (1=Laptop, 2=Phone): ").strip()

    if mode == "2":
        print(f" Go to: http://{ip}:{WEB_PORT}")
        threading.Thread(target=run_web, daemon=True).start()
        
        # TRACKER FOR THE LAST PROCESSED FRAME
        last_processed_id = None 

        while True:
            with frame_lock:
                if latest_frame_from_phone is not None:
                    
                    # 1. UNIQUE ID CHECK (The Magic Fix)
                    current_id = id(latest_frame_from_phone)
                    
                    if current_id != last_processed_id:
                        frame = cv2.flip(latest_frame_from_phone, 1)
                        processed_frame = process_pose_frame(frame, mirror_mode=True)
                        cv2.imshow('Phone Feed', processed_frame)
                        last_processed_id = current_id
                    
            if cv2.waitKey(1) & 0xFF == ord('q'): break
    else:
        vs = WebcamStream(src=0).start()
        print(" [CAM] Threaded Capture Started")
        while not vs.stopped:
            frame = vs.read()
            if frame is None: continue
            frame = cv2.flip(frame, 1)
            frame = process_pose_frame(frame, mirror_mode=True)
            cv2.imshow('Laptop Feed', frame)
            if cv2.waitKey(1) & 0xFF == ord('q'): break
        vs.stop()
        cv2.destroyAllWindows()
