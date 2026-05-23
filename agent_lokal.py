"""
agent_lokal.py — Agen Lokal (jalankan di komputer kamu)
Tugasnya: deteksi wajah + kirim frame video ke Railway via SocketIO.
"""

import cv2
import dlib
import numpy as np
import tensorflow as tf
import threading
import requests
import socketio
import base64
import os
import time
import logging
from datetime import datetime
from pathlib import Path
from config import Config
from database import FaceDatabase
from notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ── Konfigurasi ───────────────────────────────────────────────────
RAILWAY_URL  = os.getenv('RAILWAY_URL',  'https://NAMA-PROYEK.up.railway.app')
AGENT_SECRET = os.getenv('AGENT_SECRET', 'agent_rahasia_123')
HEADERS      = {'X-Agent-Secret': AGENT_SECRET}

# Resolusi frame yang dikirim (lebih kecil = lebih ringan)
STREAM_WIDTH   = 640
STREAM_HEIGHT  = 360
STREAM_QUALITY = 50   # JPEG quality 1-100
STREAM_FPS     = 10   # frame per detik yang dikirim ke cloud

config   = Config()
db       = FaceDatabase()
notifier = TelegramNotifier(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)

# ── SocketIO client ───────────────────────────────────────────────
sio = socketio.Client(reconnection=True, reconnection_attempts=0,
                      reconnection_delay=2, logger=False, engineio_logger=False)

@sio.event
def connect():
    logger.info("SocketIO terhubung ke Railway")

@sio.event
def disconnect():
    logger.warning("SocketIO terputus dari Railway")

def connect_socketio():
    while True:
        try:
            sio.connect(RAILWAY_URL, headers=HEADERS,
                        transports=['websocket'])
            break
        except Exception as e:
            logger.warning(f"SocketIO gagal konek: {e}, coba lagi 3 detik...")
            time.sleep(3)

# ── Global state ──────────────────────────────────────────────────
system_running = False
detector       = None
cnn_model      = None
class_names    = None
models_loaded  = False

detection_stats = {
    "total_detected": 0,
    "known_today":    0,
    "unknown_today":  0,
    "last_detection": None
}

ESP32_IP = os.getenv('ESP32_IP', '')


# ── Load model ────────────────────────────────────────────────────
def load_models():
    global detector, cnn_model, class_names, models_loaded
    try:
        logger.info("Memuat dlib HOG detector...")
        detector = dlib.get_frontal_face_detector()

        logger.info("Memuat CNN model...")
        if not Path(config.CNN_MODEL_PATH).exists():
            logger.warning("CNN model belum ada!")
            return

        cnn_model   = tf.keras.models.load_model(config.CNN_MODEL_PATH)
        class_names = np.load(config.CLASS_NAMES_PATH, allow_pickle=True)
        logger.info(f"Model siap! Kelas: {list(class_names)}")
        models_loaded = True
    except Exception as e:
        logger.error(f"Gagal memuat model: {e}")


# ── Face recognition ──────────────────────────────────────────────
def preprocess_face(img_rgb, rect):
    x1 = max(0, rect.left());  y1 = max(0, rect.top())
    x2 = min(img_rgb.shape[1], rect.right())
    y2 = min(img_rgb.shape[0], rect.bottom())
    crop = img_rgb[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    resized = cv2.resize(crop, (config.IMG_SIZE, config.IMG_SIZE))
    return np.expand_dims(resized / 255.0, axis=0)

def recognize_face(img_rgb, rect):
    face_in = preprocess_face(img_rgb, rect)
    if face_in is None:
        return "Orang Asing", 0.0
    preds      = cnn_model.predict(face_in, verbose=0)
    confidence = float(np.max(preds))
    class_idx  = int(np.argmax(preds))
    if confidence < config.CNN_CONFIDENCE:
        return "Orang Asing", confidence
    name = str(class_names[class_idx])
    return ("Orang Asing", confidence) if name == "unknown" else (name, confidence)


# ── Cooldown helpers ──────────────────────────────────────────────
last_alert_time = {}
last_stats_time = {}

def can_alert(key, cooldown=None):
    cd  = cooldown or config.ALERT_COOLDOWN
    now = time.time()
    if now - last_alert_time.get(key, 0) > cd:
        last_alert_time[key] = now
        return True
    return False

def can_count(key, cd=10):
    now = time.time()
    if now - last_stats_time.get(key, 0) > cd:
        last_stats_time[key] = now
        return True
    return False


# ── Kirim ke Railway ──────────────────────────────────────────────
def post_json(endpoint, payload):
    try:
        r = requests.post(f"{RAILWAY_URL}{endpoint}", json=payload,
                          headers=HEADERS, timeout=10)
        if r.status_code != 200:
            logger.warning(f"POST {endpoint} → {r.status_code}")
    except Exception as e:
        logger.warning(f"Gagal POST {endpoint}: {e}")

def upload_snapshot(path: str):
    try:
        with open(path, 'rb') as f:
            r = requests.post(
                f"{RAILWAY_URL}/agent/snapshot",
                files={'photo': (Path(path).name, f, 'image/jpeg')},
                headers=HEADERS, timeout=15
            )
        if r.status_code == 200:
            logger.info(f"Snapshot terkirim: {Path(path).name}")
        else:
            logger.warning(f"Upload snapshot gagal: {r.status_code}")
    except Exception as e:
        logger.warning(f"Gagal upload snapshot: {e}")

def save_snapshot_local(frame):
    snap_dir = Path("logs/snapshots")
    snap_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
    path = snap_dir / f"snap_{ts}.jpg"
    cv2.imwrite(str(path), frame)
    return str(path)

def trigger_buzzer():
    if not ESP32_IP:
        return
    try:
        requests.get(f"http://{ESP32_IP}/buzz", timeout=3)
        logger.info("Buzzer ESP32 dibunyikan")
    except Exception as e:
        logger.warning(f"Buzzer gagal: {e}")

def encode_frame(frame):
    """Resize + encode frame ke base64 JPEG untuk dikirim via SocketIO."""
    small = cv2.resize(frame, (STREAM_WIDTH, STREAM_HEIGHT))
    ret, buf = cv2.imencode('.jpg', small,
                            [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
    if not ret:
        return None
    return base64.b64encode(buf).decode('utf-8')


# ── Camera loop ───────────────────────────────────────────────────
def camera_loop():
    global system_running, detection_stats

    source = config.CAMERA_SOURCE
    try:
        source = int(source)
    except ValueError:
        pass

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        logger.error(f"Kamera tidak bisa dibuka: {source}")
        post_json('/agent/status', {'running': False})
        system_running = False
        return

    logger.info("Kamera aktif, mulai deteksi + streaming...")
    post_json('/agent/status', {'running': True})

    frame_count    = 0
    last_stream_t  = 0
    stream_interval = 1.0 / STREAM_FPS
    last_boxes     = []

    while system_running:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        frame_count += 1
        detections  = []

        # ── Deteksi wajah ──────────────────────────────────────────
        if models_loaded and frame_count % config.PROCESS_EVERY_N_FRAMES == 0:
            small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
            rgb   = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            dets  = detector(rgb, 1)
            last_boxes = []

            for det in dets:
                try:
                    name, conf = recognize_face(rgb, det)
                    is_stranger = (name == "Orang Asing")
                    color = (0, 0, 255) if is_stranger else (0, 220, 100)
                    x1, y1 = det.left()*2, det.top()*2
                    x2, y2 = det.right()*2, det.bottom()*2
                    last_boxes.append({"box": (x1,y1,x2,y2), "color": color,
                                       "label": f"{name} ({conf*100:.0f}%)"})

                    if can_count(f"stat_{name}"):
                        detection_stats["total_detected"] += 1
                        detection_stats["last_detection"]  = datetime.now().strftime("%H:%M:%S")

                    if is_stranger:
                        if can_count("stat_unknown"):
                            detection_stats["unknown_today"] += 1
                        if can_alert("unknown"):
                            snap_path = save_snapshot_local(frame)
                            threading.Thread(target=trigger_buzzer, daemon=True).start()
                            threading.Thread(target=notifier.send_alert,
                                             args=(snap_path,), daemon=True).start()
                            threading.Thread(target=upload_snapshot,
                                             args=(snap_path,), daemon=True).start()
                            detections.append({"type": "stranger", "name": "Orang Asing",
                                               "conf": round(conf*100,1),
                                               "time": datetime.now().strftime("%H:%M:%S")})
                    else:
                        if can_count(f"stat_known_{name}"):
                            detection_stats["known_today"] += 1
                        detections.append({"type": "known", "name": name,
                                           "conf": round(conf*100,1),
                                           "time": datetime.now().strftime("%H:%M:%S")})
                except Exception as e:
                    logger.error(f"Error deteksi: {e}")

        # ── Gambar kotak wajah di frame ────────────────────────────
        display = frame.copy()
        for item in last_boxes:
            x1, y1, x2, y2 = item["box"]
            cv2.rectangle(display, (x1,y1), (x2,y2), item["color"], 2)
            cv2.rectangle(display, (x1, y2-28), (x2, y2), item["color"], cv2.FILLED)
            cv2.putText(display, item["label"], (x1+4, y2-8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 1)

        # Timestamp di frame
        ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        cv2.putText(display, ts, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 1)

        # ── Kirim frame ke Railway via SocketIO ────────────────────
        now = time.time()
        if sio.connected and (now - last_stream_t) >= stream_interval:
            b64 = encode_frame(display)
            if b64:
                try:
                    sio.emit('frame', {'data': b64})
                    last_stream_t = now
                except Exception:
                    pass

        # ── Kirim deteksi ke Railway ───────────────────────────────
        if detections:
            threading.Thread(
                target=post_json,
                args=('/agent/detection', {
                    'detections': detections,
                    'stats':      detection_stats
                }), daemon=True
            ).start()

    cap.release()
    post_json('/agent/status', {'running': False})
    logger.info("Kamera dimatikan.")


# ── Main ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    logger.info(f"Agent lokal → Railway: {RAILWAY_URL}")
    logger.info("Memuat model...")
    load_models()

    logger.info("Menghubungkan SocketIO ke Railway...")
    threading.Thread(target=connect_socketio, daemon=True).start()
    time.sleep(2)

    system_running = True
    try:
        camera_loop()
    except KeyboardInterrupt:
        logger.info("Dihentikan oleh user.")
        system_running = False
    finally:
        if sio.connected:
            sio.disconnect()
