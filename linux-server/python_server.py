import socket
import threading
import time
import pygame
import os
import cv2
import mediapipe as mp
import numpy as np
import logging
import sys
from flask import Flask, render_template_string, jsonify, send_file
from flask_socketio import SocketIO

# --- PERFORMANCE & TRACKING ---
HEADLESS_MODE = True  # Set to True to disable all video rendering for maximum FPS!

# ================= LINUX PRIORITY =================
try:
    os.nice(-10) 
    print(" [LINUX] High Priority Mode: ACTIVE")
except:
    print(" [LINUX] Normal Priority (Run with sudo for boost)")

# ================= CONFIGURATION =================
UDP_DISCOVERY_PORT = 5555
UDP_HIT_PORT = 5556
WEB_PORT = 5000

# Drum Zones
CYMBAL_HEIGHT = 0.4
DIVIDER_1 = 0.35  
DIVIDER_2 = 0.65  
STICK_EXTENSION = 1.2 

# --- DEBOUNCE SETTINGS ---
DEBOUNCE_TIME = 0.04  # 40 milliseconds cooldown per stick
last_hit_time = {"LEFT": 0.0, "RIGHT": 0.0, "KICK": 0.0}

# Lightweight Kalman Filter (Alpha-Beta)
KALMAN_ALPHA = 0.6     # Trust in raw position (0.0 = frozen, 1.0 = raw/jittery)
KALMAN_BETA = 0.2      # Trust in velocity momentum (0.0 = no prediction, 1.0 = overshoot)
PREDICTION_FRAMES = 4  # How many frames to project into the future

# Global State
current_zone_left = "SNARE"
current_zone_right = "SNARE"
kalman_state = {"Left": None, "Right": None} # Stores [x, y, vx, vy]
latest_frame_from_phone = None
frame_lock = threading.Lock()

# ================= LOW-LATENCY AUDIO (ALSA) =================
# Buffer 64 = ~1.5ms latency. If crackling occurs, try 128.
pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=64)
pygame.mixer.init()
pygame.mixer.set_num_channels(16)

def load_sound(path):
    if os.path.exists(path): return pygame.mixer.Sound(path)
    print(f" [WARNING] Sound not found: {path}")
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

# ================= V4L2 CAMERA =================
class WebcamStream:
    def __init__(self, src=0, width=640, height=360):
        # Linux V4L2 Optimized Backend
        self.stream = cv2.VideoCapture(src, cv2.CAP_V4L2)
        self.stream.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.stream.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.stream.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.stream.set(cv2.CAP_PROP_FPS, 60)
        self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1)
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
    def stop(self): self.stopped = True; self.stream.release()

# ================= VISION LOGIC =================
mp_pose = mp.solutions.pose
pose = mp_pose.Pose(
    model_complexity=0, # <--- 0 FOR MAX SPEED (LINUX OPTIMIZED)
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
    global current_zone_left, current_zone_right, kalman_state 
    h, w, _ = frame.shape
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = pose.process(rgb_frame)

    if not HEADLESS_MODE:
        # Draw Zones only if we are rendering the video
        c = (80,80,80)
        cymbal_y = int(h * CYMBAL_HEIGHT)
        div_1_x = int(w * DIVIDER_1)
        div_2_x = int(w * DIVIDER_2)

        cv2.line(frame, (0, cymbal_y), (w, cymbal_y), c, 1)
        cv2.line(frame, (div_2_x, 0), (div_2_x, cymbal_y), c, 1)
        cv2.line(frame, (div_1_x, cymbal_y), (div_1_x, h), c, 1)
        cv2.line(frame, (div_2_x, cymbal_y), (div_2_x, h), c, 1)

    if results.pose_landmarks:
        lm = results.pose_landmarks.landmark
        arms = [("Right", 13, 15), ("Left", 14, 16)] if mirror_mode else [("Left", 13, 15), ("Right", 14, 16)]

        for name, idx_elb, idx_wri in arms:
            elbow = lm[idx_elb]
            wrist = lm[idx_wri]
            if wrist.visibility > 0.3:
                ex, ey = int(elbow.x * w), int(elbow.y * h)
                wx, wy = int(wrist.x * w), int(wrist.y * h)
                
                # 1. Base Physics
                raw_tx, raw_ty = extend_line(ex, ey, wx, wy, STICK_EXTENSION)
                
                # 2. NEW: Alpha-Beta Kalman Filter
                if kalman_state[name] is None:
                    # First frame: Initialize state [x, y, velocity_x, velocity_y]
                    kalman_state[name] = [raw_tx, raw_ty, 0.0, 0.0]
                    kx, ky = raw_tx, raw_ty
                    kvx, kvy = 0.0, 0.0
                else:
                    kx, ky, kvx, kvy = kalman_state[name]
                    
                    # Predict next state using momentum
                    pred_x = kx + kvx
                    pred_y = ky + kvy
                    
                    # Calculate residual (difference between raw camera measurement and prediction)
                    res_x = raw_tx - pred_x
                    res_y = raw_ty - pred_y
                    
                    # Update state with Alpha (position gain) and Beta (velocity gain)
                    kx = pred_x + (KALMAN_ALPHA * res_x)
                    ky = pred_y + (KALMAN_ALPHA * res_y)
                    kvx = kvx + (KALMAN_BETA * res_x)
                    kvy = kvy + (KALMAN_BETA * res_y)
                    
                    kalman_state[name] = [kx, ky, kvx, kvy]

                # 3. Time Travel Prediction
                tx = int(kx + (kvx * PREDICTION_FRAMES))
                ty = int(ky + (kvy * PREDICTION_FRAMES))

                # Keep coordinates safely on-screen
                tx = max(0, min(w, tx))
                ty = max(0, min(h, ty))

                detected_zone = get_drum_zone(tx/w, ty/h)
                if name == "Left": current_zone_left = detected_zone
                else: current_zone_right = detected_zone

                if not HEADLESS_MODE:
                    col = (0, 255, 0)
                    cv2.line(frame, (wx, wy), (tx, ty), col, 2) 
                    cv2.circle(frame, (tx, ty), 6, (0, 0, 255), -1) 
                    cv2.putText(frame, detected_zone[:3], (tx, ty-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
                    cv2.circle(frame, (int(kx), int(ky)), 2, (255, 255, 255), -1)

    return frame
    
# ================= WEB SERVER =================
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', ping_interval=5) # Uses eventlet!
log = logging.getLogger('werkzeug'); log.setLevel(logging.ERROR)

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>AirDrums Linux</title>
    
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" type="image/png" href="/icon.png">
    <link rel="apple-touch-icon" href="/icon.png">
    <meta name="theme-color" content="#000000">

    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        body { margin:0; background:#000; display:flex; flex-direction:column; justify-content:center; align-items:center; height:100vh; overflow:hidden; font-family:sans-serif; color:white; }
        #start-btn { padding:15px 40px; font-size:1.2rem; background:#ff8000; color:#fff; border:none; border-radius:30px; }
        #status { display:none; text-align:center; }
        .pulsing-circle { width:50px; height:50px; background:#ff751a; border-radius:50%; margin:0 auto 20px auto; animation:pulse 2s infinite; }
        @keyframes pulse { 0% { transform:scale(0.95); opacity:0.7; } 100% { transform:scale(0.95); opacity:0; } }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
</head>
<body>
    <button id="start-btn" onclick="start()">Connect (Linux Mode)</button>
    <div id="status"><div class="pulsing-circle"></div><h3>LIVE</h3></div>
    <video id="v" autoplay playsinline muted style="display:none"></video>
    <canvas id="c" style="display:none"></canvas>
    <script>
        const s = io(); const v = document.getElementById('v'); const c = document.getElementById('c'); const ctx = c.getContext('2d');
        async function start(){
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 640 }, height: { ideal: 360 } } });
                v.srcObject = stream; await v.play();
                document.getElementById('start-btn').style.display='none'; document.getElementById('status').style.display='block';
                c.width=320; c.height=180;
                setInterval(() => {
                    ctx.drawImage(v,0,0,320,180);
                    c.toBlob(b => { if(b) s.emit('frame', b); }, 'image/jpeg', 0.5);
                }, 33);
                if(document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
            } catch(e) { alert(e); }
        }
    </script>
</body>
</html>
"""

@app.route('/') 
def index(): return render_template_string(HTML_PAGE)

# --- ICON ROUTE ---
@app.route('/icon.png')
def icon():
    if os.path.exists('icon.png'): return send_file('icon.png', mimetype='image/png')
    return "No Icon Found", 404

# --- MANIFEST ROUTE (WITH LANDSCAPE FORCE) ---
@app.route('/manifest.json') 
def m(): 
    return jsonify({
        "name": "AirDrums",
        "short_name": "AirDrums",
        "display": "standalone",
        "orientation": "landscape", 
        "start_url": "/",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [{
            "src": "/icon.png",
            "sizes": "192x192",
            "type": "image/png"
        }]
    })

@socketio.on('frame')
def h(data):
    global latest_frame_from_phone
    try:
        n = np.frombuffer(data, np.uint8)
        with frame_lock: latest_frame_from_phone = cv2.imdecode(n, cv2.IMREAD_COLOR)
    except: pass

def run_web(): socketio.run(app, host="0.0.0.0", port=WEB_PORT)


# ================= UDP NETWORK (UPDATED WITH DEBOUNCE) =================
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
        
        # 1. Discovery Broadcast (CRITICAL: Hardware needs this to connect!)
        if current_time - last_broadcast > 1.0:
            try: 
                t_disc.sendto(b"AIRDRUM_SERVER", ("255.255.255.255", UDP_DISCOVERY_PORT))
            except: 
                pass
            last_broadcast = current_time
        
        # 2. Receive Hits & Debounce
        while True:
            try:
                data, _ = t_list.recvfrom(32)
                msg = data.decode("utf-8").upper().strip()
                now = time.time()
                
                if "KICK" in msg:
                    if now - last_hit_time["KICK"] > DEBOUNCE_TIME:
                        play_sound("KICK")
                        last_hit_time["KICK"] = now
                        
                elif "LEFT" in msg: 
                    if now - last_hit_time["LEFT"] > DEBOUNCE_TIME:
                        play_sound(current_zone_left)
                        last_hit_time["LEFT"] = now
                        
                elif "RIGHT" in msg: 
                    if now - last_hit_time["RIGHT"] > DEBOUNCE_TIME:
                        play_sound(current_zone_right)
                        last_hit_time["RIGHT"] = now
                        
            except BlockingIOError: 
                break 
            except Exception as e: 
                # I added a print here just in case something else is failing!
                print(f" [DEBUG] UDP Error: {e}") 
                break
                
        time.sleep(0.001)

if __name__ == "__main__":
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close()
    except: ip="127.0.0.1"
    
    print(f"AIR DRUMS LINUX | Web: http://{ip}:{WEB_PORT}")
    threading.Thread(target=udp_loops, daemon=True).start()

    mode = input(" Mode (1=Laptop, 2=Phone): ").strip()
    print(f" [INFO] Headless Mode is {'ON (Max FPS)' if HEADLESS_MODE else 'OFF'}")
    
    if mode == "2":
        threading.Thread(target=run_web, daemon=True).start()
        lid = None
        while True:
            with frame_lock:
                if latest_frame_from_phone is not None:
                    cid = id(latest_frame_from_phone)
                    if cid != lid:
                        frame = process_pose_frame(cv2.flip(latest_frame_from_phone,1), True)
                        lid = cid
                        if not HEADLESS_MODE:
                            cv2.imshow('Phone', frame)
            
            if not HEADLESS_MODE:
                if cv2.waitKey(1) & 0xFF == ord('q'): break
            else:
                time.sleep(0.001) # Keeps the headless loop from pegging the CPU
    else:
        vs = WebcamStream(src=0).start()
        while not vs.stopped:
            f = vs.read()
            if f is not None: 
                frame = process_pose_frame(cv2.flip(f,1), True)
                if not HEADLESS_MODE:
                    cv2.imshow('Laptop', frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'): break
                else:
                    time.sleep(0.001)
        vs.stop()
        if not HEADLESS_MODE: cv2.destroyAllWindows()