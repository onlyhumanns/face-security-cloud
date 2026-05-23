"""
agent_lokal.py — Agen Lokal (jalankan di komputer kamu)
Tugasnya: deteksi wajah via kamera → kirim hasil ke Railway dashboard.

Cara pakai:
  1. Isi RAILWAY_URL dan AGENT_SECRET di bawah (atau di .env)
  2. python agent_lokal.py
"""

import cv2
import dlib
import numpy as np
import tensorflow as tf
import threading
import requests
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

# ── Konfigurasi koneksi ke Railway ────────────────────────────────
RAILWAY_URL  = os.getenv('RAILWAY_URL',   'https://NAMA-PROYEK.up.railway.app')
AGENT_SECRET = os.getenv('AGENT_SECRET',  'agent_rahasia_123')   # harus sama dengan di Railway

HEADERS = {'X-Agent-Secret': AGENT_SECRET}

config   = Config()
db       = FaceDatabase()
notifier = TelegramNotifier(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)

# ── Global state ──────────────────────────────────────────────────
system_running  = False
detector        = None
shape_predictor = None
cnn_model       = None
class_names     = None
models_loaded   = False

detection_stats = {
    "total_detected": 0,
    "known_today":    0,
    "unknown_today":  0,
    "last_detection": None
}

ESP32_IP = os.getenv('ESP32_IP', config.__class__.__dict__.get('ESP32_IP', ''))


# ── Load model ────────────────────────────────────────────────────
def load_models():
    global detector, shape_predictor, cnn_model, class_names, models_loaded
    try:
        logger.info("Memuat dlib HOG detector...")
        detector        = dlib.get_frontal_face_detector()
        shape_predictor = dlib.shape_predictor(config.SHAPE_PREDICTOR_PATH)

        logger.info("Memuat CNN model...")
        if not Path(config.CNN_MODEL_PATH).exists():
            logger.warning("CNN model belum ada! Jalankan: python face_training/train.py")
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
            logger.warning(f"POST {endpoint} → {r.status_code}: {r.text[:100]}")
    except Exception as e:
        logger.warning(f"Gagal POST {endpoint}: {e}")


def upload_snapshot(path: str):
    try:
        with open(path, 'rb') as f:
            r = requests.post(
                f"{RAILWAY_URL}/agent/snapshot",
                files={'photo': (Path(path).name, f, 'image/jpeg')},
                headers=HEADERS,
                timeout=15
            )
        if r.status_code == 200:
            logger.info(f"Snapshot terkirim ke Railway: {Path(path).name}")
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

    logger.info("Kamera aktif, mulai deteksi...")
    post_json('/agent/status', {'running': True})

    frame_count      = 0
    last_drawn_boxes = []

    while system_running:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        frame_count += 1
        detections   = []

        if models_loaded and frame_count % config.PROCESS_EVERY_N_FRAMES == 0:
            small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
            rgb   = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            dets  = detector(rgb, 1)

            for det in dets:
                try:
                    name, conf = recognize_face(rgb, det)
                    is_stranger = (name == "Orang Asing")

                    if can_count(f"stat_{name}"):
                        detection_stats["total_detected"] += 1
                        detection_stats["last_detection"]  = datetime.now().strftime("%H:%M:%S")

                    if is_stranger:
                        if can_count("stat_unknown"):
                            detection_stats["unknown_today"] += 1

                        if can_alert("unknown"):
                            snap_path = save_snapshot_local(frame)

                            # Buzzer ESP32
                            threading.Thread(target=trigger_buzzer, daemon=True).start()

                            # Telegram
                            threading.Thread(
                                target=notifier.send_alert,
                                args=(snap_path,), daemon=True
                            ).start()

                            # Upload snapshot ke Railway
                            threading.Thread(
                                target=upload_snapshot,
                                args=(snap_path,), daemon=True
                            ).start()

                            detections.append({
                                "type": "stranger",
                                "name": "Orang Asing",
                                "conf": round(conf * 100, 1),
                                "time": datetime.now().strftime("%H:%M:%S")
                            })
                    else:
                        if can_count(f"stat_known_{name}"):
                            detection_stats["known_today"] += 1
                        detections.append({
                            "type": "known",
                            "name": name,
                            "conf": round(conf * 100, 1),
                            "time": datetime.now().strftime("%H:%M:%S")
                        })

                except Exception as e:
                    logger.error(f"Error deteksi: {e}")

        if detections:
            threading.Thread(
                target=post_json,
                args=('/agent/detection', {
                    'detections': detections,
                    'stats':      detection_stats
                }),
                daemon=True
            ).start()

        # Preview lokal (opsional, bisa di-comment kalau tidak perlu)
        for item in last_drawn_boxes:
            x1, y1, x2, y2 = item["box"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), item["color"], 2)

        cv2.imshow("Face Security - Agent Lokal (tekan Q untuk keluar)", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
    post_json('/agent/status', {'running': False})
    logger.info("Kamera dimatikan.")


# ── Main ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    logger.info(f"Agent lokal → Railway: {RAILWAY_URL}")
    logger.info("Memuat model...")
    load_models()

    system_running = True
    try:
        camera_loop()
    except KeyboardInterrupt:
        logger.info("Dihentikan oleh user.")
        system_running = False
