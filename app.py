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
AGENT_NAME = "Riley"

# ─────────────────────────────────────────────
# BRAIN
# ─────────────────────────────────────────────
BRAIN = f"""
You are {AGENT_NAME}, a member of the Wishlist Vacations hospitality team in North Carolina.
You manage guest communication overnight (10pm to 8am) across 34 lake and waterfront properties.

YOUR VOICE:
- Warm and personal, like texting a friend
- Use the guest's first name
- Short paragraphs, get to the point fast
- Emoji okay when natural
- Never say "per our policy" — sound like a real person
- Sign off naturally as yourself when appropriate, not every message
- Always leave the door open to help further

URGENT — respond immediately:
- Lockout / can't get in
- Smoke or carbon monoxide alarm
- Water leak or flooding
- No heat (cold weather, under 45F outside)
- No AC (extreme heat, over 90F outside)
- Neighbor noise / party preventing sleep
- Power outage affecting safety

NOT URGENT — do not respond, leave for morning team:
- Late checkout / early check-in requests
- General questions with no time pressure
- Positive feedback / thank yous
- Anything that can reasonably wait until 8am
- Pricing or discount negotiations
- Multi-night extension requests
- Group size / booking detail questions with no urgency

WHAT YOU NEVER DO:
- Promise a refund, discount, or rate change
- Make up information you don't have
- Respond to non-urgent messages at night
- Send a message just to send one
- Negotiate pricing or checkout times — always defer to morning team

ESCALATION:
If self-help steps don't resolve an urgent issue within 10 minutes,
let the guest know a team member will reach out shortly.

When in doubt — ask: would a reasonable guest be upset if this waited until 8am?
If yes, respond. If no, leave it for the morning team.
"""

recent_events = []
raw_payloads = []

def log_event(event):
    event["timestamp"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    recent_events.insert(0, event)
    if len(recent_events) > 50:
        recent_events.pop()
    print(f"[{event['timestamp']}] {json.dumps(event)[:150]}")

def save_raw_payload(payload):
    raw_payloads.insert(0, {"timestamp": datetime.now().isoformat(), "payload": payload})
    if len(raw_payloads) > 30:
        raw_payloads.pop()

def is_night_shift():
    hour = datetime.now().hour
    return hour >= NIGHT_SHIFT_START or hour < NIGHT_SHIFT_END

# ─────────────────────────────────────────────
# OWNERREZ HELPERS
# ─────────────────────────────────────────────

def ownerrez_get(path, params=None):
    import requests
    try:
        r = requests.get(
            f"https://api.ownerrez.com/v2{path}",
            headers={
                "Authorization": f"Bearer {OWNERREZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": f"WishlistRiley/1.0 ({OWNERREZ_CLIENT_ID})"
            },
            params=params or {}
        )
        if r.status_code == 200:
            return r.json()
        print(f"GET {path} returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"Error GET {path}: {e}")
    return None

def ownerrez_post(path, body):
    import requests
    try:
        r = requests.post(
            f"https://api.ownerrez.com/v2{path}",
            headers={
                "Authorization": f"Bearer {OWNERREZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": f"WishlistRiley/1.0 ({OWNERREZ_CLIENT_ID})"
            },
            json=body
        )
        return r.status_code in [200, 201]
    except Exception as e:
        print(f"Error POST {path}: {e}")
    return False

def get_contact_info(contact_id):
    if not contact_id:
        return {}
    data = ownerrez_get(f"/contacts/{contact_id}")
    return data or {}

def get_booking_info(booking_id):
    if not booking_id:
        return {}
    data = ownerrez_get(f"/bookings/{booking_id}")
    return data or {}

def get_thread_messages(thread_id):
    if not thread_id:
        return []
    data = ownerrez_get("/messages", {"thread_id": thread_id})
    if data:
        return data.get("items", [])
    return []

def send_reply(thread_id, message_body):
    return ownerrez_post("/messages", {"thread_id": thread_id, "body": message_body})

# ─────────────────────────────────────────────
# AI EVALUATION
# ─────────────────────────────────────────────

def evaluate_message(guest_message, guest_name, property_name, thread_history):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    history_text = ""
    if thread_history:
        history_text = "\n\nPREVIOUS MESSAGES IN THIS THREAD:\n"
        for msg in thread_history[-6:]:
            role = msg.get("from_role", msg.get("direction", "unknown"))
            history_text += f"{role}: {msg.get('body', '')[:200]}\n"

    prompt = f"""
{BRAIN}

CURRENT SITUATION:
Guest name: {guest_name or "the guest"}
Property: {property_name or "unknown property"}
{history_text}

NEW MESSAGE FROM GUEST:
"{guest_message}"

Evaluate this message and respond in JSON only with this exact format:
{{
  "urgency": "urgent" or "wait",
  "reasoning": "one sentence explaining your decision",
  "response": "your full message to the guest if urgent, or null if not urgent"
}}

If urgent, write the response as Riley, a real Wishlist Vacations team member.
If not urgent, set response to null.
Return JSON only, no other text, no markdown formatting.
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def dashboard():
    mode_colors = {"off": "#A32D2D", "draft": "#854F0B", "live": "#0F6E56"}
    mode_color = mode_colors.get(AI_MODE, "#666")
    mode_labels = {
        "off": "OFF — logging webhooks only, no AI evaluation",
        "draft": "DRAFT — Riley evaluates and logs but never sends",
        "live": "LIVE — Riley is actively responding to urgent messages"
    }

    rows = ""
    for e in recent_events[:25]:
        urgency = e.get("urgency", "")
        urgency_badge = ""
        if urgency == "urgent":
            urgency_badge = '<span style="background:#FCEBEB;color:#A32D2D;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">URGENT</span>'
        elif urgency == "wait":
            urgency_badge = '<span style="background:#EAF3DE;color:#3B6D11;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">WAIT</span>'

        action = e.get("action", "")
        action_badge = ""
        if action == "sent":
            action_badge = '<span style="background:#E6F1FB;color:#185FA5;padding:2px 8px;border-radius:4px;font-size:11px;">SENT</span>'
        elif action == "draft_only":
            action_badge = '<span style="background:#FAEEDA;color:#854F0B;padding:2px 8px;border-radius:4px;font-size:11px;">DRAFT</span>'
        elif action == "logged_only":
            action_badge = '<span style="background:#F1EFE8;color:#5F5E5A;padding:2px 8px;border-radius:4px;font-size:11px;">LOGGED</span>'

        draft_resp = e.get("draft_response") or e.get("response") or ""

        rows += f"""
        <tr style="border-bottom:1px solid #eee;vertical-align:top;">
            <td style="padding:10px;font-size:12px;color:#666;white-space:nowrap;">{e.get('timestamp','')}</td>
            <td style="padding:10px;font-size:13px;">{e.get('guest','—')}</td>
            <td style="padding:10px;font-size:13px;">{e.get('property','—')}</td>
            <td style="padding:10px;font-size:13px;max-width:280px;">{(e.get('message','—') or '—')[:120]}</td>
            <td style="padding:10px;">{urgency_badge}</td>
            <td style="padding:10px;">{action_badge}</td>
            <td style="padding:10px;font-size:12px;color:#666;max-width:200px;">{e.get('reasoning','')}</td>
            <td style="padding:10px;font-size:12px;color:#185FA5;max-width:250px;">{draft_resp[:150] if draft_resp else ''}</td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="8" style="padding:40px;text-align:center;color:#999;">No messages yet — waiting for guest activity</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Wishlist Vacations — Riley (Night Shift AI)</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; background: #f8f8f6; }}
        .header {{ background: #0F6E56; color: white; padding: 20px 30px; }}
        .header h1 {{ margin: 0; font-size: 22px; }}
        .header p {{ margin: 4px 0 0; font-size: 14px; opacity: 0.8; }}
        .status-bar {{ background: white; padding: 16px 30px; border-bottom: 1px solid #eee; display: flex; gap: 30px; align-items: center; flex-wrap: wrap; }}
        .status-chip {{ padding: 6px 16px; border-radius: 20px; font-size: 13px; font-weight: bold; color: white; background: {mode_color}; }}
        .stat {{ font-size: 13px; color: #666; }}
        .stat span {{ font-weight: bold; color: #333; }}
        .container {{ padding: 24px 30px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; border: 1px solid #eee; overflow-x: auto; }}
        .card h2 {{ margin: 0 0 16px; font-size: 16px; color: #333; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ text-align: left; padding: 10px; font-size: 12px; color: #999; border-bottom: 2px solid #eee; white-space: nowrap; }}
        .instructions {{ background: #E6F1FB; border-radius: 8px; padding: 16px 20px; font-size: 13px; color: #185FA5; line-height: 1.6; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Wishlist Vacations — Riley</h1>
        <p>Night Shift AI · wishlistnc.com · 34 lake properties · Active 10pm–8am</p>
    </div>
    <div class="status-bar">
        <div class="status-chip">{mode_labels.get(AI_MODE, AI_MODE).upper()}</div>
        <div class="stat">Night shift: <span>{'ACTIVE' if is_night_shift() else 'INACTIVE'}</span></div>
        <div class="stat">Current time: <span>{datetime.now().strftime('%I:%M %p')}</span></div>
        <div class="stat">Events logged: <span>{len(recent_events)}</span></div>
        <div class="stat" style="margin-left:auto;font-size:11px;color:#aaa;">Auto-refreshes every 30s · <a href="/debug/raw?pretty=1">raw payloads</a></div>
    </div>
    <div class="container">
        <div class="instructions">
            <strong>Change Riley's mode:</strong> Railway → project → Variables → <code>AI_MODE</code> =
            <strong>off</strong> (log only) | <strong>draft</strong> (evaluate, don't send) | <strong>live</strong> (fully active)
        </div>
        <br>
        <div class="card">
            <h2>Recent Activity</h2>
            <table>
                <thead><tr>
                    <th>Time</th><th>Guest</th><th>Property</th><th>Message</th>
                    <th>Urgency</th><th>Action</th><th>Reasoning</th><th>Riley's Response</th>
                </tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
</body>
</html>"""

@app.route("/debug/raw", methods=["GET"])
def debug_raw():
    pretty = request.args.get("pretty")
    if pretty:
        output = ""
        for item in raw_payloads:
            output += f"=== {item['timestamp']} ===\n{json.dumps(item['payload'], indent=2)}\n\n"
        return Response(output, mimetype="text/plain")
    return jsonify(raw_payloads)

# ─────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────

@app.route("/webhook/ownerrez", methods=["POST"])
def ownerrez_webhook():
    auth = request.authorization
    if not auth or auth.password != OWNERREZ_WEBHOOK_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "No payload"}), 400

    save_raw_payload(payload)

    action = payload.get("action", "")
    entity = payload.get("entity", {})
    if not isinstance(entity, dict):
        entity = {}

    # Only process new message entities
    if action != "entity_create" or "body" not in entity:
        log_event({"type": "webhook_received", "action": action, "guest": "—", "property": "—", "message": "—"})
        return jsonify({"status": "ignored", "reason": "not a new message"}), 200

    from_role = entity.get("from_role", "")
    message_body = entity.get("body", "")
    thread_id = entity.get("thread_id") or entity.get("id")
    from_contact_id = entity.get("from_contact_id")
    is_draft = entity.get("is_draft", False)

    # Only process actual guest messages, not host/system/cotraveler messages
    if from_role not in ("guest",):
        log_event({
            "type": "message_received",
            "action": "logged_only",
            "guest": f"(role: {from_role})",
            "property": "—",
            "message": message_body[:150],
            "reasoning": f"Skipped — from_role is '{from_role}', not 'guest'"
        })
        return jsonify({"status": "ignored", "reason": f"from_role={from_role}"}), 200

    if is_draft:
        return jsonify({"status": "ignored", "reason": "draft message"}), 200

    # Get guest/contact info
    contact = get_contact_info(from_contact_id)
    guest_name = contact.get("first_name", "") if contact else ""
    property_name = entity.get("property_name", "")

    log_event({
        "type": "message_received",
        "guest": guest_name or "Guest",
        "property": property_name or "—",
        "message": message_body
    })

    # MODE: OFF
    if AI_MODE == "off":
        log_event({
            "type": "message_received", "action": "logged_only",
            "guest": guest_name or "Guest", "property": property_name or "—",
            "message": message_body, "reasoning": "AI_MODE is off"
        })
        return jsonify({"status": "logged", "mode": "off"}), 200

    # Not night shift
    if not is_night_shift():
        log_event({
            "type": "message_received", "action": "logged_only",
            "guest": guest_name or "Guest", "property": property_name or "—",
            "message": message_body, "reasoning": "Not night shift hours"
        })
        return jsonify({"status": "skipped", "reason": "not night shift"}), 200

    thread_history = get_thread_messages(thread_id)

    try:
        result = evaluate_message(message_body, guest_name, property_name, thread_history)
    except Exception as e:
        log_event({"type": "error", "guest": guest_name, "message": str(e)})
        return jsonify({"status": "error", "reason": str(e)}), 500

    urgency = result.get("urgency", "wait")
    reasoning = result.get("reasoning", "")
    response_text = result.get("response")

    if AI_MODE == "draft":
        log_event({
            "type": "message_evaluated", "action": "draft_only",
            "guest": guest_name or "Guest", "property": property_name or "—",
            "message": message_body, "urgency": urgency, "reasoning": reasoning,
            "draft_response": response_text
        })
        return jsonify({"status": "draft", "urgency": urgency, "would_send": response_text}), 200

    # LIVE mode
    if urgency == "urgent" and response_text and thread_id:
        sent = send_reply(thread_id, response_text)
        log_event({
            "type": "message_responded", "action": "sent" if sent else "send_failed",
            "guest": guest_name or "Guest", "property": property_name or "—",
            "message": message_body, "urgency": urgency, "reasoning": reasoning,
            "response": response_text
        })
        return jsonify({"status": "responded", "sent": sent}), 200
    else:
        log_event({
            "type": "message_left_unread", "action": "logged_only",
            "guest": guest_name or "Guest", "property": property_name or "—",
            "message": message_body, "urgency": urgency, "reasoning": reasoning
        })
        return jsonify({"status": "left_unread", "urgency": urgency}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
