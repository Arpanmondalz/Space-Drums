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
from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO

# ================= CONFIGURATION =================
UDP_DISCOVERY_PORT = 5555
UDP_HIT_PORT = 5556
WEB_PORT = 5000
BROADCAST_INTERVAL = 1.0

# Drum Zone Layout (0.0 - 1.0)
CYMBAL_HEIGHT = 0.4
DIVIDER_1 = 0.35  # Left vs Center for Bottom (35%)
DIVIDER_2 = 0.65  # Center vs Right for Top & Bottom (65%)

# Stick Physics
STICK_EXTENSION = 1.2 

# --- PERFORMANCE & TRACKING ---
HEADLESS_MODE = False  # Set to True to disable video rendering for max FPS

# --- DEBOUNCE SETTINGS ---
DEBOUNCE_TIME = 0.04  # 40 milliseconds cooldown per stick
last_hit_time = {"LEFT": 0.0, "RIGHT": 0.0, "KICK": 0.0}

# Lightweight Kalman Filter (Alpha-Beta)
KALMAN_ALPHA = 0.6     # Trust in raw position
KALMAN_BETA = 0.2      # Trust in velocity momentum
PREDICTION_FRAMES = 4  # How many frames to project into the future

# Global State
current_zone_left = "SNARE"
current_zone_right = "SNARE"
kalman_state = {"Left": None, "Right": None} # Stores [x, y, vx, vy]
latest_frame_from_phone = None
frame_lock = threading.Lock()

# ================= AUDIO ENGINE =================
pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=64)
pygame.mixer.init()
pygame.init()
pygame.mixer.set_num_channels(16)

volumes = {
    "SNARE": 1.0, "HI-HAT": 1.0, "FLOOR TOM": 1.0, 
    "CRASH": 1.0, "RIDE": 1.0, "KICK": 1.0
}

def load_sound(path):
    if os.path.exists(path): return pygame.mixer.Sound(path)
    print(f" [WARNING] Sound missing: {path}")
    return None

sounds = {
    "SNARE": load_sound("sounds/snare.wav"),
    "HI-HAT": load_sound("sounds/hihat.wav"),
    "FLOOR TOM": load_sound("sounds/tom.wav"),
    "CRASH": load_sound("sounds/crash.wav"),
    "RIDE": load_sound("sounds/ride.wav"), 
    "KICK": load_sound("sounds/kick.wav")  
}

def play_sound(zone):
    if zone in sounds and sounds[zone]:
        sounds[zone].play()
        print(f" > {zone}")

# ================= THREADED CAMERA CLASS =================
class WebcamStream:
    def __init__(self, src=0, width=320, height=240):
        # Standard VideoCapture for Windows
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
    global current_zone_left, current_zone_right, kalman_state
    
    h, w, _ = frame.shape
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = pose.process(rgb_frame)

    if not HEADLESS_MODE:
        # Draw Zones
        c = (100, 100, 100)
        cymbal_y = int(h * CYMBAL_HEIGHT)
        div_1_x = int(w * DIVIDER_1)
        div_2_x = int(w * DIVIDER_2)

        cv2.line(frame, (0, cymbal_y), (w, cymbal_y), c, 1)
        cv2.line(frame, (div_2_x, 0), (div_2_x, cymbal_y), c, 1)
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
                raw_tx, raw_ty = extend_line(ex, ey, wx, wy, STICK_EXTENSION)
                
                # 2. Alpha-Beta Kalman Filter
                if kalman_state[name] is None:
                    kalman_state[name] = [raw_tx, raw_ty, 0.0, 0.0]
                    kx, ky = raw_tx, raw_ty
                    kvx, kvy = 0.0, 0.0
                else:
                    kx, ky, kvx, kvy = kalman_state[name]
                    
                    pred_x = kx + kvx
                    pred_y = ky + kvy
                    
                    res_x = raw_tx - pred_x
                    res_y = raw_ty - pred_y
                    
                    kx = pred_x + (KALMAN_ALPHA * res_x)
                    ky = pred_y + (KALMAN_ALPHA * res_y)
                    kvx = kvx + (KALMAN_BETA * res_x)
                    kvy = kvy + (KALMAN_BETA * res_y)
                    
                    kalman_state[name] = [kx, ky, kvx, kvy]

                # 3. Time Travel Prediction
                tx = int(kx + (kvx * PREDICTION_FRAMES))
                ty = int(ky + (kvy * PREDICTION_FRAMES))

                tx = max(0, min(w, tx))
                ty = max(0, min(h, ty))

                detected_zone = get_drum_zone(tx/w, ty/h)
                
                if name == "Left": current_zone_left = detected_zone
                else: current_zone_right = detected_zone

                # Visuals
                if not HEADLESS_MODE:
                    color = (0, 255, 0)
                    cv2.line(frame, (wx, wy), (tx, ty), color, 2) 
                    cv2.circle(frame, (tx, ty), 6, (0, 0, 255), -1) 
                    cv2.putText(frame, detected_zone[:3], (tx, ty-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
                    cv2.circle(frame, (int(kx), int(ky)), 2, (255, 255, 255), -1)

    return frame

# ================= PWA SERVER =================
app = Flask(__name__)
# Keep threading for Windows environment stability
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_interval=5)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Air Drums</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#000000">
    <style>
        * { box-sizing: border-box; }
        body { 
            margin: 0; background-color: #000; display: flex; flex-direction: column;
            justify-content: center; align-items: center; height: 100vh; width: 100vw; 
            overflow: hidden; font-family: sans-serif; color: white; user-select: none;
        }
        #ui-layer { text-align: center; z-index: 10; }
        #start-btn {
            padding: 18px 40px; font-size: 1.1rem; font-weight: 600; color: #fff; 
            background: #ff8000; border: none; border-radius: 50px; cursor: pointer; 
            text-transform: uppercase; letter-spacing: 1px; box-shadow: 0 4px 15px rgba(255, 128, 0, 0.4);
        }
        #start-btn:active { transform: scale(0.95); }
        #status { display: none; animation: fadeIn 0.5s ease-out; }
        h3 { margin: 10px 0 5px 0; letter-spacing: 2px; }
        p { margin: 0; font-size: 0.9rem; opacity: 0.7; }
        .pulsing-circle {
            width: 60px; height: 60px; background-color: #ff751a; border-radius: 50%;
            margin: 0 auto 20px auto; position: relative;
        }
        .pulsing-circle::before, .pulsing-circle::after {
            content: ''; position: absolute; left: 0; top: 0; right: 0; bottom: 0;
            border-radius: 50%; border: 2px solid #ff751a; animation: pulse 2s infinite;
        }
        .pulsing-circle::after { animation-delay: 0.5s; }
        @keyframes pulse { 0% { transform: scale(1); opacity: 1; } 100% { transform: scale(2.5); opacity: 0; } }
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
                const stream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: "environment", width: { ideal: 640 }, height: { ideal: 360 }, frameRate: { ideal: 30 } }
                });
                v.srcObject = stream; 
                await v.play();
                btn.style.display = 'none'; 
                status.style.display = 'block';
                c.width = 320; 
                c.height = 180; 

                setInterval(() => {
                    ctx.drawImage(v, 0, 0, 320, 180);
                    c.toBlob(blob => { if(blob) s.emit('frame', blob); }, 'image/jpeg', 0.5); 
                }, 33); 

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

@socketio.on('frame')
def h(data):
    global latest_frame_from_phone
    try:
        n = np.frombuffer(data, np.uint8)
        frame = cv2.imdecode(n, cv2.IMREAD_COLOR)
        with frame_lock: 
            latest_frame_from_phone = frame
    except Exception as e: 
        print(f"Frame Error: {e}")

def run_web(): 
    # allow_unsafe_werkzeug ensures compatibility when using threading mode
    socketio.run(app, host="0.0.0.0", port=WEB_PORT, allow_unsafe_werkzeug=True)


# ================= UDP NETWORK (WITH DEBOUNCE) =================
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
        
        if current_time - last_broadcast > 1.0:
            try: t_disc.sendto(b"AIRDRUM_SERVER", ("255.255.255.255", UDP_DISCOVERY_PORT))
            except: pass
            last_broadcast = current_time
        
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
                        
            except BlockingIOError: break 
            except Exception as e: 
                print(f" [DEBUG] UDP Error: {e}") 
                break
                
        time.sleep(0.001)

# ================= PYGAME UI & MAIN LOOP =================
def main():
    global HEADLESS_MODE, volumes, sounds

    # Get local IP for display
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close()
    except: ip="127.0.0.1"

    # Start network thread
    threading.Thread(target=udp_loops, daemon=True).start()

    # Pygame UI Setup
    screen = pygame.display.set_mode((700, 450))
    pygame.display.set_caption("Air Drums Control Panel (Windows)")
    font = pygame.font.SysFont("arial", 20, bold=True)
    title_font = pygame.font.SysFont("arial", 28, bold=True)
    small_font = pygame.font.SysFont("arial", 14)

    # UI State
    app_state = "STARTUP" 
    camera_mode = None
    vs = None
    lid = None

    # UI Elements Layout
    btn_pc = pygame.Rect(150, 180, 180, 60)
    btn_mobile = pygame.Rect(370, 180, 180, 60)
    btn_ip = pygame.Rect(20, 380, 110, 40)
    btn_headless = pygame.Rect(275, 380, 150, 40)
    
    show_ip = False
    
    drum_names = ["SNARE", "HI-HAT", "CRASH", "RIDE", "FLOOR TOM", "KICK"]
    sliders = {}
    spacing = 700 / 6
    for i, name in enumerate(drum_names):
        x_center = (i * spacing) + (spacing / 2)
        sliders[name] = pygame.Rect(x_center - 15, 120, 30, 200)

    dragging_slider = None
    clock = pygame.time.Clock()
    running = True

    while running:
        screen.fill((15, 15, 20)) 

        # --- EVENT HANDLING ---
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1: 
                    if app_state == "STARTUP":
                        if btn_pc.collidepoint(event.pos):
                            camera_mode = "PC"
                            vs = WebcamStream(src=0).start()
                            app_state = "MIXER"
                        elif btn_mobile.collidepoint(event.pos):
                            camera_mode = "MOBILE"
                            threading.Thread(target=run_web, daemon=True).start()
                            app_state = "MIXER"
                        elif btn_ip.collidepoint(event.pos):
                            show_ip = not show_ip
                    
                    elif app_state == "MIXER":
                        if btn_headless.collidepoint(event.pos):
                            HEADLESS_MODE = not HEADLESS_MODE
                            if HEADLESS_MODE: 
                                cv2.destroyAllWindows()
                        elif camera_mode == "MOBILE" and btn_ip.collidepoint(event.pos):
                            show_ip = not show_ip
                        
                        for name, rect in sliders.items():
                            if rect.collidepoint(event.pos):
                                dragging_slider = name
            
            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1:
                    dragging_slider = None
            
            elif event.type == pygame.MOUSEMOTION:
                if dragging_slider:
                    rect = sliders[dragging_slider]
                    rel_y = max(0, min(rect.height, event.pos[1] - rect.y))
                    new_vol = 1.0 - (rel_y / rect.height)
                    volumes[dragging_slider] = new_vol
                    if sounds.get(dragging_slider):
                        sounds[dragging_slider].set_volume(new_vol)

        # --- DRAWING UI ---
        if app_state == "STARTUP":
            title = title_font.render("AIR DRUMS", True, (255, 128, 0)) 
            screen.blit(title, (270, 80))

            pygame.draw.rect(screen, (50, 150, 255), btn_pc, border_radius=10)
            pygame.draw.rect(screen, (255, 120, 50), btn_mobile, border_radius=10)

            screen.blit(font.render("PC Camera", True, (255, 255, 255)), (btn_pc.x + 35, btn_pc.y + 18))
            screen.blit(font.render("Mobile App", True, (255, 255, 255)), (btn_mobile.x + 35, btn_mobile.y + 18))
            
            # Show/Hide IP Toggle Button
            pygame.draw.rect(screen, (70, 70, 90), btn_ip, border_radius=5)
            ip_btn_txt = font.render("Hide IP" if show_ip else "Show IP", True, (255, 255, 255))
            screen.blit(ip_btn_txt, (btn_ip.x + 10, btn_ip.y + 8))
            
            if show_ip:
                ip_txt = small_font.render(f"Phone URL: http://{ip}:{WEB_PORT}", True, (50, 255, 100))
                screen.blit(ip_txt, (btn_ip.x, btn_ip.y - 20))

        elif app_state == "MIXER":
            # Draw Sliders
            for name, rect in sliders.items():
                pygame.draw.rect(screen, (40, 40, 50), rect) 
                
                vol = volumes[name]
                fill_h = int(vol * rect.height)
                fill_y = rect.y + (rect.height - fill_h)
                
                r = 255
                g = int(vol * 200)
                b = max(0, int(138 - (vol * 138)))
                pygame.draw.rect(screen, (r, g, b), (rect.x, fill_y, rect.width, fill_h)) 
                pygame.draw.rect(screen, (220, 220, 220), (rect.x-5, fill_y-5, rect.width+10, 10)) 
                
                lbl = small_font.render(name, True, (200, 200, 200))
                screen.blit(lbl, (rect.x + 15 - (lbl.get_width()//2), rect.y - 25))
                val_lbl = small_font.render(f"{int(vol*100)}%", True, (255, 255, 255))
                screen.blit(val_lbl, (rect.x + 15 - (val_lbl.get_width()//2), rect.y + rect.height + 10))

            # Draw Headless Toggle
            h_color = (255, 128, 0) if HEADLESS_MODE else (100, 100, 100)
            pygame.draw.rect(screen, h_color, btn_headless, border_radius=5)
            h_text = font.render(f"Headless: {'ON' if HEADLESS_MODE else 'OFF'}", True, (255, 255, 255))
            screen.blit(h_text, (btn_headless.x + 15, btn_headless.y + 8))
            
            # IP Toggle Button (Visible only in Mobile Mode)
            if camera_mode == "MOBILE":
                pygame.draw.rect(screen, (70, 70, 90), btn_ip, border_radius=5)
                ip_btn_txt = font.render("Hide IP" if show_ip else "Show IP", True, (255, 255, 255))
                screen.blit(ip_btn_txt, (btn_ip.x + 10, btn_ip.y + 8))
                
                if show_ip:
                    ip_txt = small_font.render(f"Phone URL: http://{ip}:{WEB_PORT}", True, (50, 255, 100))
                    screen.blit(ip_txt, (btn_ip.x, btn_ip.y - 20))
            
            mode_txt = small_font.render(f"[{camera_mode} MODE]", True, (150, 150, 150))
            screen.blit(mode_txt, (580, 20))

            # --- CAMERA PROCESSING ---
            if camera_mode == "PC":
                f = vs.read()
                if f is not None:
                    frame = process_pose_frame(cv2.flip(f, 1), True)
                    if not HEADLESS_MODE:
                        cv2.imshow('Air Drums - PC Feed', frame)
                        cv2.waitKey(1)
            
            elif camera_mode == "MOBILE":
                with frame_lock:
                    if latest_frame_from_phone is not None:
                        cid = id(latest_frame_from_phone)
                        if cid != lid:
                            frame = process_pose_frame(cv2.flip(latest_frame_from_phone, 1), True)
                            lid = cid
                            if not HEADLESS_MODE:
                                cv2.imshow('Air Drums - Mobile Feed', frame)
                                cv2.waitKey(1)

        pygame.display.flip()
        clock.tick(60)

    # Cleanup
    if vs: vs.stop()
    cv2.destroyAllWindows()
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()
