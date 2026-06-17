from flask import Flask, request, jsonify
import json
import os
from datetime import datetime
import anthropic

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIGURATION — set these in Railway
# ─────────────────────────────────────────────
OWNERREZ_WEBHOOK_PASSWORD = os.environ.get("OWNERREZ_WEBHOOK_PASSWORD", "nightshift2024")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OWNERREZ_TOKEN = os.environ.get("OWNERREZ_TOKEN", "")
OWNERREZ_CLIENT_ID = os.environ.get("OWNERREZ_CLIENT_ID", "")
NIGHT_SHIFT_START = 22  # 10pm
NIGHT_SHIFT_END = 8     # 8am

# ─────────────────────────────────────────────
# MASTER SWITCHES — control from Railway env vars
# AI_MODE options:
#   "off"   — receive webhooks, log only, do nothing
#   "draft" — evaluate messages, log decisions, never send
#   "live"  — fully active, sends real responses to guests
# ─────────────────────────────────────────────
AI_MODE = os.environ.get("AI_MODE", "off")

# ─────────────────────────────────────────────
# BRAIN DOCUMENT
# ─────────────────────────────────────────────
BRAIN = """
You are a member of the Wishlist Vacations hospitality team based in North Carolina.
You manage 34 lake and waterfront properties.
You work the night shift (10pm to 8am).

YOUR VOICE:
- Warm and personal, like texting a friend
- Use the guest's first name
- Short paragraphs, get to the point fast
- Emoji are okay when natural
- Never say "per our policy" — sound like a person
- Always end with an offer to help or reassurance

URGENT — respond immediately:
- Lockout / can't get in
- Smoke or carbon monoxide alarm
- Water leak or flooding
- No heat in cold weather (under 45F outside)
- No AC in extreme heat (over 90F)
- Neighbor noise / party preventing sleep
- Power outage affecting safety

NOT URGENT — do not respond, leave for morning team:
- Late checkout requests
- Early check-in requests
- General questions with no time sensitivity
- Positive feedback or thank yous
- Requests that can wait until 8am

WHAT YOU NEVER DO:
- Promise a refund or discount
- Make up information you don't have
- Respond to non-urgent messages at night
- Send a response just to send one

ESCALATION:
If self-help steps don't resolve an urgent issue within 10 minutes,
tell the guest a team member will call them shortly.

When in doubt — ask yourself: would a reasonable guest be upset
if this waited until 8am? If yes, respond. If no, leave it.
"""

# ─────────────────────────────────────────────
# LOG — in-memory for dashboard display
# ─────────────────────────────────────────────
recent_events = []

def log_event(event):
    event["timestamp"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    recent_events.insert(0, event)
    if len(recent_events) > 50:
        recent_events.pop()
    print(f"[{event['timestamp']}] {json.dumps(event)[:120]}")
    # Also write to file
    with open("message_log.jsonl", "a") as f:
        f.write(json.dumps(event) + "\n")

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def is_night_shift():
    hour = datetime.now().hour
    return hour >= NIGHT_SHIFT_START or hour < NIGHT_SHIFT_END

def get_thread_messages(thread_id):
    import requests
    try:
        r = requests.get(
            "https://api.ownerrez.com/v2/messages",
            headers={
                "Authorization": f"Bearer {OWNERREZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": f"WishlistNightShift/1.0 ({OWNERREZ_CLIENT_ID})"
            },
            params={"thread_id": thread_id}
        )
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception as e:
        print(f"Error fetching thread: {e}")
    return []

def get_booking_details(booking_id):
    import requests
    try:
        r = requests.get(
            f"https://api.ownerrez.com/v2/bookings/{booking_id}",
            headers={
                "Authorization": f"Bearer {OWNERREZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": f"WishlistNightShift/1.0 ({OWNERREZ_CLIENT_ID})"
            }
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"Error fetching booking: {e}")
    return {}

def send_ownerrez_message(thread_id, message_body):
    import requests
    try:
        r = requests.post(
            "https://api.ownerrez.com/v2/messages",
            headers={
                "Authorization": f"Bearer {OWNERREZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": f"WishlistNightShift/1.0 ({OWNERREZ_CLIENT_ID})"
            },
            json={"thread_id": thread_id, "body": message_body}
        )
        return r.status_code in [200, 201]
    except Exception as e:
        print(f"Error sending message: {e}")
    return False

def evaluate_message(guest_message, guest_name, property_name, thread_history):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    history_text = ""
    if thread_history:
        history_text = "\n\nPREVIOUS MESSAGES IN THIS THREAD:\n"
        for msg in thread_history[-6:]:
            direction = "Guest" if msg.get("direction") == "inbound" else "Host"
            history_text += f"{direction}: {msg.get('body', '')[:200]}\n"

    prompt = f"""
{BRAIN}

CURRENT SITUATION:
Guest name: {guest_name}
Property: {property_name}
{history_text}

NEW MESSAGE FROM GUEST:
"{guest_message}"

Evaluate this message and respond in JSON only with this exact format:
{{
  "urgency": "urgent" or "wait",
  "reasoning": "one sentence explaining your decision",
  "response": "your full message to the guest if urgent, or null if not urgent"
}}

If urgent, write the response as if you are the Wishlist Vacations team member.
If not urgent, set response to null.
Return JSON only, no other text.
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
# ROUTES
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def dashboard():
    mode_colors = {"off": "#A32D2D", "draft": "#854F0B", "live": "#0F6E56"}
    mode_color = mode_colors.get(AI_MODE, "#666")
    mode_descriptions = {
        "off": "OFF — logging webhooks only, no AI evaluation",
        "draft": "DRAFT — AI evaluates and logs but never sends to guests",
        "live": "LIVE — AI is actively responding to urgent guest messages"
    }

    rows = ""
    for e in recent_events[:20]:
        urgency_badge = ""
        if e.get("urgency") == "urgent":
            urgency_badge = '<span style="background:#FCEBEB;color:#A32D2D;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">URGENT</span>'
        elif e.get("urgency") == "wait":
            urgency_badge = '<span style="background:#EAF3DE;color:#3B6D11;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">WAIT</span>'

        action_badge = ""
        if e.get("action") == "sent":
            action_badge = '<span style="background:#E6F1FB;color:#185FA5;padding:2px 8px;border-radius:4px;font-size:11px;">SENT</span>'
        elif e.get("action") == "draft_only":
            action_badge = '<span style="background:#FAEEDA;color:#854F0B;padding:2px 8px;border-radius:4px;font-size:11px;">DRAFT</span>'
        elif e.get("action") == "logged_only":
            action_badge = '<span style="background:#F1EFE8;color:#5F5E5A;padding:2px 8px;border-radius:4px;font-size:11px;">LOGGED</span>'

        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:10px;font-size:12px;color:#666;">{e.get('timestamp','')}</td>
            <td style="padding:10px;font-size:13px;">{e.get('guest','—')}</td>
            <td style="padding:10px;font-size:13px;">{e.get('property','—')}</td>
            <td style="padding:10px;font-size:13px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{e.get('message','—')[:80]}</td>
            <td style="padding:10px;">{urgency_badge}</td>
            <td style="padding:10px;">{action_badge}</td>
            <td style="padding:10px;font-size:12px;color:#666;max-width:200px;">{e.get('reasoning','')}</td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="7" style="padding:40px;text-align:center;color:#999;">No messages yet — waiting for guest activity</td></tr>'

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Wishlist Night Shift AI</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; background: #f8f8f6; }}
        .header {{ background: #0F6E56; color: white; padding: 20px 30px; }}
        .header h1 {{ margin: 0; font-size: 22px; }}
        .header p {{ margin: 4px 0 0; font-size: 14px; opacity: 0.8; }}
        .status-bar {{ background: white; padding: 16px 30px; border-bottom: 1px solid #eee; display: flex; gap: 30px; align-items: center; }}
        .status-chip {{ padding: 6px 16px; border-radius: 20px; font-size: 13px; font-weight: bold; color: white; background: {mode_color}; }}
        .stat {{ font-size: 13px; color: #666; }}
        .stat span {{ font-weight: bold; color: #333; }}
        .container {{ padding: 24px 30px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; border: 1px solid #eee; }}
        .card h2 {{ margin: 0 0 16px; font-size: 16px; color: #333; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ text-align: left; padding: 10px; font-size: 12px; color: #999; border-bottom: 2px solid #eee; }}
        .instructions {{ background: #E6F1FB; border-radius: 8px; padding: 16px 20px; font-size: 13px; color: #185FA5; line-height: 1.6; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Wishlist Vacations — Night Shift AI</h1>
        <p>wishlistnc.com · 34 lake properties · Active 10pm–8am</p>
    </div>
    <div class="status-bar">
        <div class="status-chip">{mode_descriptions.get(AI_MODE, AI_MODE).upper()}</div>
        <div class="stat">Night shift: <span>{'ACTIVE' if is_night_shift() else 'INACTIVE'}</span></div>
        <div class="stat">Current time: <span>{datetime.now().strftime('%I:%M %p')}</span></div>
        <div class="stat">Events logged: <span>{len(recent_events)}</span></div>
        <div class="stat" style="margin-left:auto;font-size:11px;color:#aaa;">Auto-refreshes every 30 seconds</div>
    </div>
    <div class="container">
        <div class="instructions">
            <strong>How to change the AI mode:</strong> Go to Railway → your project → Variables tab → change <code>AI_MODE</code> to:
            &nbsp;<strong>off</strong> (log only) &nbsp;|&nbsp; <strong>draft</strong> (evaluate but don't send) &nbsp;|&nbsp; <strong>live</strong> (fully active)
        </div>
        <br>
        <div class="card">
            <h2>Recent Activity</h2>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Guest</th>
                        <th>Property</th>
                        <th>Message</th>
                        <th>Urgency</th>
                        <th>Action</th>
                        <th>Reasoning</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
</body>
</html>"""
    return html

@app.route("/webhook/ownerrez", methods=["POST"])
def ownerrez_webhook():
    auth = request.authorization
    if not auth or auth.password != OWNERREZ_WEBHOOK_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "No payload"}), 400

    event_type = payload.get("type", "")

     # Log everything including raw payload so we can see OwnerRez's format
    log_event({
        "type": "webhook_received", 
        "event": event_type,
        "raw_payload": json.dumps(payload)[:500]
    })
    
   # Always log raw webhook including full payload for debugging
    log_event({
        "type": "webhook_received",
        "event": event_type,
        "guest": str(payload)[:80],
        "property": "",
        "message": json.dumps(payload)[:150]
    })

    if event_type != "message.created":
        return jsonify({"status": "ignored", "reason": "not a message event"}), 200

    message_data = payload.get("data", {})
    if message_data.get("direction") != "inbound":
        return jsonify({"status": "ignored", "reason": "outbound message"}), 200

    guest_message = message_data.get("body", "")
    thread_id = message_data.get("thread_id")
    booking_id = message_data.get("booking_id")
    guest_name = message_data.get("guest_name", "Guest")
    property_name = message_data.get("property_name", "the property")

    # MODE: OFF — just log it
    if AI_MODE == "off":
        log_event({
            "type": "message_received",
            "action": "logged_only",
            "guest": guest_name,
            "property": property_name,
            "message": guest_message,
            "mode": "off"
        })
        return jsonify({"status": "logged", "mode": "off"}), 200

    # Not night shift — skip
    if not is_night_shift():
        log_event({
            "type": "message_received",
            "action": "logged_only",
            "guest": guest_name,
            "property": property_name,
            "message": guest_message,
            "reasoning": "Not night shift hours"
        })
        return jsonify({"status": "skipped", "reason": "not night shift hours"}), 200

    # Get context
    thread_history = get_thread_messages(thread_id) if thread_id else []
    booking = get_booking_details(booking_id) if booking_id else {}
    if booking:
        guest_data = booking.get("guest", {})
        guest_name = guest_data.get("first_name", guest_name).strip()
        property_name = booking.get("property_name", property_name)

    # Evaluate with AI
    try:
        result = evaluate_message(guest_message, guest_name, property_name, thread_history)
    except Exception as e:
        log_event({"type": "error", "message": str(e)})
        return jsonify({"status": "error", "reason": str(e)}), 500

    urgency = result.get("urgency", "wait")
    reasoning = result.get("reasoning", "")
    response_text = result.get("response")

    # MODE: DRAFT — evaluate but never send
    if AI_MODE == "draft":
        log_event({
            "type": "message_evaluated",
            "action": "draft_only",
            "guest": guest_name,
            "property": property_name,
            "message": guest_message,
            "urgency": urgency,
            "reasoning": reasoning,
            "draft_response": response_text
        })
        return jsonify({
            "status": "draft",
            "urgency": urgency,
            "reasoning": reasoning,
            "would_have_sent": response_text
        }), 200

    # MODE: LIVE — actually send
    if urgency == "urgent" and response_text and thread_id:
        sent = send_ownerrez_message(thread_id, response_text)
        log_event({
            "type": "message_responded",
            "action": "sent",
            "guest": guest_name,
            "property": property_name,
            "message": guest_message,
            "urgency": urgency,
            "reasoning": reasoning,
            "response": response_text,
            "sent": sent
        })
        return jsonify({"status": "responded", "sent": sent}), 200
    else:
        log_event({
            "type": "message_left_unread",
            "action": "logged_only",
            "guest": guest_name,
            "property": property_name,
            "message": guest_message,
            "urgency": urgency,
            "reasoning": reasoning
        })
        return jsonify({"status": "left_unread", "urgency": urgency}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
