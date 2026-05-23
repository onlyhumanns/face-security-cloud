"""
app_cloud.py — Dashboard Web (Railway)
Menerima frame video + data deteksi dari agent_lokal via SocketIO.
"""

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app      = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'keamanan_rumah_secret')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading',
                    max_http_buffer_size=5 * 1024 * 1024)  # 5MB untuk frame

# ── State global ──────────────────────────────────────────────────
system_running  = False
buzzer_on       = False
detection_stats = {
    "total_detected": 0,
    "known_today":    0,
    "unknown_today":  0,
    "last_detection": None
}
snapshots  = []
log_items  = []

AGENT_SECRET = os.getenv('AGENT_SECRET', 'agent_rahasia_123')

# Set SID agent agar frame tidak di-relay ke dirinya sendiri
agent_sid = None

def _check_agent(req):
    return req.headers.get('X-Agent-Secret') == AGENT_SECRET


# ── Routes ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/agent/status', methods=['POST'])
def agent_status():
    global system_running
    if not _check_agent(request):
        return jsonify({'error': 'unauthorized'}), 403
    data = request.get_json()
    system_running = data.get('running', False)
    socketio.emit('system_status', {'running': system_running})
    return jsonify({'ok': True})

@app.route('/agent/detection', methods=['POST'])
def agent_detection():
    global detection_stats, log_items
    if not _check_agent(request):
        return jsonify({'error': 'unauthorized'}), 403

    data       = request.get_json()
    detections = data.get('detections', [])
    stats      = data.get('stats', {})

    detection_stats.update(stats)
    for det in detections:
        log_items.insert(0, det)
    if len(log_items) > 50:
        log_items = log_items[:50]

    socketio.emit('detection', {'detections': detections, 'stats': detection_stats})
    socketio.emit('stats_update', detection_stats)
    return jsonify({'ok': True})

@app.route('/agent/snapshot', methods=['POST'])
def agent_snapshot():
    if not _check_agent(request):
        return jsonify({'error': 'unauthorized'}), 403
    if 'photo' not in request.files:
        return jsonify({'error': 'no photo'}), 400

    import base64
    file  = request.files['photo']
    data  = base64.b64encode(file.read()).decode('utf-8')
    url   = f"data:image/jpeg;base64,{data}"
    ts    = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    snap  = {'filename': file.filename, 'url': url, 'time': ts}

    snapshots.insert(0, snap)
    if len(snapshots) > 30:
        snapshots.pop()

    socketio.emit('new_snapshot', snap)
    return jsonify({'ok': True})

@app.route('/api/system/status')
def system_status():
    return jsonify({'running': system_running, 'stats': detection_stats})

@app.route('/api/snapshots')
def get_snapshots():
    return jsonify(snapshots[:20])

@app.route('/api/buzzer/on', methods=['POST'])
def buzzer_on_route():
    global buzzer_on
    buzzer_on = True
    socketio.emit('buzzer_status',  {'on': True})
    socketio.emit('buzzer_command', {'state': True})
    return jsonify({'status': 'on'})

@app.route('/api/buzzer/off', methods=['POST'])
def buzzer_off_route():
    global buzzer_on
    buzzer_on = False
    socketio.emit('buzzer_status',  {'on': False})
    socketio.emit('buzzer_command', {'state': False})
    return jsonify({'status': 'off'})

@app.route('/api/buzzer/status')
def buzzer_status():
    return jsonify({'on': buzzer_on})

@app.route('/api/telegram/test', methods=['POST'])
def test_telegram():
    socketio.emit('telegram_test', {})
    return jsonify({'status': 'sent_to_agent'})


# ── SocketIO events ───────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    emit('stats_update',  detection_stats)
    emit('system_status', {'running': system_running})
    emit('buzzer_status', {'on': buzzer_on})
    logger.info(f"Client terhubung: {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    global agent_sid
    if request.sid == agent_sid:
        agent_sid = None
        logger.info("Agent lokal terputus")

@socketio.on('frame')
def on_frame(data):
    """Terima frame dari agent, broadcast ke semua browser."""
    global agent_sid
    agent_sid = request.sid
    # Relay frame ke semua browser (skip pengirim)
    socketio.emit('frame', data, skip_sid=request.sid)


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"Cloud dashboard berjalan di port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
