"""
app_cloud.py — Dashboard Web (Railway)
Tidak ada kamera / cv2 / dlib / tensorflow di sini.
Hanya menerima data dari agent_lokal.py via SocketIO.
"""

from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app    = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'keamanan_rumah_secret')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── State global (in-memory, reset saat deploy ulang) ─────────────
system_running  = False
buzzer_on       = False
detection_stats = {
    "total_detected": 0,
    "known_today":    0,
    "unknown_today":  0,
    "last_detection": None
}
snapshots = []          # list dict: {filename, url, time}
log_items = []          # list dict deteksi terbaru (max 50)

AGENT_SECRET = os.getenv('AGENT_SECRET', 'agent_rahasia_123')   # wajib cocok di agent_lokal


# ── Routes dashboard ──────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


# ── API dipanggil oleh agent_lokal ───────────────────────────────
def _check_agent(req):
    """Validasi secret header dari agent lokal."""
    return req.headers.get('X-Agent-Secret') == AGENT_SECRET


@app.route('/agent/status', methods=['POST'])
def agent_status():
    """Agent melaporkan status sistem (nyala/mati)."""
    global system_running
    if not _check_agent(request):
        return jsonify({'error': 'unauthorized'}), 403
    data = request.get_json()
    system_running = data.get('running', False)
    socketio.emit('system_status', {'running': system_running})
    return jsonify({'ok': True})


@app.route('/agent/detection', methods=['POST'])
def agent_detection():
    """Agent mengirim hasil deteksi wajah."""
    global detection_stats, log_items
    if not _check_agent(request):
        return jsonify({'error': 'unauthorized'}), 403

    data       = request.get_json()
    detections = data.get('detections', [])
    stats      = data.get('stats', {})
    snapshot   = data.get('snapshot')   # dict {filename, url, time} atau None

    detection_stats.update(stats)

    for det in detections:
        log_items.insert(0, det)
    if len(log_items) > 50:
        log_items = log_items[:50]

    if snapshot:
        snapshots.insert(0, snapshot)
        if len(snapshots) > 30:
            snapshots.pop()

    # Broadcast ke semua browser yang terbuka
    socketio.emit('detection', {
        'detections': detections,
        'stats':      detection_stats
    })
    socketio.emit('stats_update', detection_stats)

    return jsonify({'ok': True})


@app.route('/agent/snapshot', methods=['POST'])
def agent_snapshot():
    """Agent upload foto snapshot orang asing."""
    if not _check_agent(request):
        return jsonify({'error': 'unauthorized'}), 403

    if 'photo' not in request.files:
        return jsonify({'error': 'no photo'}), 400

    import base64, io
    file  = request.files['photo']
    fname = file.filename
    data  = base64.b64encode(file.read()).decode('utf-8')
    url   = f"data:image/jpeg;base64,{data}"
    ts    = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    snap = {'filename': fname, 'url': url, 'time': ts}
    snapshots.insert(0, snap)
    if len(snapshots) > 30:
        snapshots.pop()

    socketio.emit('new_snapshot', snap)
    return jsonify({'ok': True, 'url': url})


# ── API untuk dashboard browser ───────────────────────────────────
@app.route('/api/system/status')
def system_status():
    return jsonify({
        'running': system_running,
        'stats':   detection_stats
    })


@app.route('/api/snapshots')
def get_snapshots():
    return jsonify(snapshots[:20])


@app.route('/api/buzzer/on', methods=['POST'])
def buzzer_on_route():
    global buzzer_on
    buzzer_on = True
    socketio.emit('buzzer_status', {'on': True})
    # Perintah buzzer diteruskan ke agent via event (agent harus listen)
    socketio.emit('buzzer_command', {'state': True})
    return jsonify({'status': 'on'})


@app.route('/api/buzzer/off', methods=['POST'])
def buzzer_off_route():
    global buzzer_on
    buzzer_on = False
    socketio.emit('buzzer_status', {'on': False})
    socketio.emit('buzzer_command', {'state': False})
    return jsonify({'status': 'off'})


@app.route('/api/buzzer/status')
def buzzer_status():
    return jsonify({'on': buzzer_on})


@app.route('/api/telegram/test', methods=['POST'])
def test_telegram():
    # Delegasikan ke agent
    socketio.emit('telegram_test', {})
    return jsonify({'status': 'sent_to_agent'})


# ── SocketIO ──────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    emit('stats_update',  detection_stats)
    emit('system_status', {'running': system_running})
    emit('buzzer_status', {'on': buzzer_on})
    logger.info(f"Browser terhubung: {request.sid}")


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"Cloud dashboard berjalan di port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
