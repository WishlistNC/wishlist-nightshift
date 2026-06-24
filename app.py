from flask import Flask, request, jsonify, Response
import json
import os
from datetime import datetime
import anthropic

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
OWNERREZ_WEBHOOK_PASSWORD = os.environ.get("OWNERREZ_WEBHOOK_PASSWORD", "nightshift2024")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OWNERREZ_TOKEN = os.environ.get("OWNERREZ_TOKEN", "")
OWNERREZ_CLIENT_ID = os.environ.get("OWNERREZ_CLIENT_ID", "")
NIGHT_SHIFT_START = 22
NIGHT_SHIFT_END = 8
AI_MODE = os.environ.get("AI_MODE", "off")

recent_events = []
raw_payloads = []  # store full untruncated payloads for download

def log_event(event):
    event["timestamp"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    recent_events.insert(0, event)
    if len(recent_events) > 50:
        recent_events.pop()
    print(f"[{event['timestamp']}] {json.dumps(event)[:120]}")

def save_raw_payload(payload):
    raw_payloads.insert(0, {
        "timestamp": datetime.now().isoformat(),
        "payload": payload
    })
    if len(raw_payloads) > 30:
        raw_payloads.pop()

def is_night_shift():
    hour = datetime.now().hour
    return hour >= NIGHT_SHIFT_START or hour < NIGHT_SHIFT_END

@app.route("/", methods=["GET"])
def dashboard():
    rows = ""
    for e in recent_events[:25]:
        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:8px;font-size:12px;color:#666;">{e.get('timestamp','')}</td>
            <td style="padding:8px;font-size:13px;">{str(e)[:150]}</td>
        </tr>
        """
    if not rows:
        rows = '<tr><td colspan="2" style="padding:20px;text-align:center;color:#999;">No events yet</td></tr>'

    return f"""<!DOCTYPE html>
<html><head><title>Wishlist Night Shift AI - Debug</title>
<style>body{{font-family:Arial;margin:20px;}} table{{width:100%;border-collapse:collapse;}}</style>
</head><body>
<h2>Wishlist Night Shift AI — Debug Mode</h2>
<p>Mode: {AI_MODE} | Night shift active: {is_night_shift()} | Events: {len(recent_events)} | Raw payloads saved: {len(raw_payloads)}</p>
<p><a href="/debug/raw">View full raw payloads (JSON)</a> | <a href="/debug/raw?pretty=1">Pretty printed</a></p>
<table>{rows}</table>
</body></html>"""

@app.route("/debug/raw", methods=["GET"])
def debug_raw():
    pretty = request.args.get("pretty")
    if pretty:
        output = ""
        for item in raw_payloads:
            output += f"=== {item['timestamp']} ===\n"
            output += json.dumps(item['payload'], indent=2)
            output += "\n\n"
        return Response(output, mimetype="text/plain")
    return jsonify(raw_payloads)

@app.route("/webhook/ownerrez", methods=["POST"])
def ownerrez_webhook():
    auth = request.authorization
    if not auth or auth.password != OWNERREZ_WEBHOOK_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "No payload"}), 400

    # Save the FULL raw payload, untruncated, for debugging
    save_raw_payload(payload)

    log_event({
        "type": "webhook_received",
        "action": payload.get("action", ""),
        "entity_type": payload.get("entity", {}).get("type", "") if isinstance(payload.get("entity"), dict) else ""
    })

    return jsonify({"status": "logged"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
