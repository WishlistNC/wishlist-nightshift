from flask import Flask, request, jsonify
import json
import os
import hmac
import hashlib
from datetime import datetime
import anthropic

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIGURATION — fill these in on Railway
# ─────────────────────────────────────────────
OWNERREZ_WEBHOOK_PASSWORD = os.environ.get("OWNERREZ_WEBHOOK_PASSWORD", "nightshift2024")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OWNERREZ_TOKEN = os.environ.get("OWNERREZ_TOKEN", "")
OWNERREZ_CLIENT_ID = os.environ.get("OWNERREZ_CLIENT_ID", "")
NIGHT_SHIFT_START = 22  # 10pm
NIGHT_SHIFT_END = 8     # 8am

# ─────────────────────────────────────────────
# BRAIN DOCUMENT — your AI's rules
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
# HELPERS
# ─────────────────────────────────────────────

def is_night_shift():
    hour = datetime.now().hour
    return hour >= NIGHT_SHIFT_START or hour < NIGHT_SHIFT_END

def get_booking_details(booking_id):
    """Pull booking context from OwnerRez API"""
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

def get_thread_messages(thread_id):
    """Pull existing thread messages for context"""
    import requests
    try:
        r = requests.get(
            f"https://api.ownerrez.com/v2/messages",
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

def send_ownerrez_message(thread_id, message_body):
    """Send a message back to the guest via OwnerRez"""
    import requests
    try:
        r = requests.post(
            "https://api.ownerrez.com/v2/messages",
            headers={
                "Authorization": f"Bearer {OWNERREZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": f"WishlistNightShift/1.0 ({OWNERREZ_CLIENT_ID})"
            },
            json={
                "thread_id": thread_id,
                "body": message_body
            }
        )
        return r.status_code == 200 or r.status_code == 201
    except Exception as e:
        print(f"Error sending message: {e}")
    return False

def evaluate_message(guest_message, guest_name, property_name, thread_history):
    """Ask Claude to evaluate urgency and draft a response if needed"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    history_text = ""
    if thread_history:
        history_text = "\n\nPREVIOUS MESSAGES IN THIS THREAD:\n"
        for msg in thread_history[-6:]:  # last 6 messages for context
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
    # Clean up any markdown code blocks if present
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def log_message(data):
    """Simple file logging — replace with database later"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "data": data
    }
    with open("message_log.json", "a") as f:
        f.write(json.dumps(log_entry) + "\n")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Logged: {json.dumps(data)[:100]}")

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "Wishlist Night Shift AI is running",
        "night_shift_active": is_night_shift(),
        "time": datetime.now().strftime("%I:%M %p")
    })

@app.route("/webhook/ownerrez", methods=["POST"])
def ownerrez_webhook():
    # Verify basic auth from OwnerRez
    auth = request.authorization
    if not auth or auth.password != OWNERREZ_WEBHOOK_PASSWORD:
        print("Webhook auth failed")
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "No payload"}), 400

    event_type = payload.get("type", "")
    print(f"\n{'='*40}")
    print(f"Webhook received: {event_type}")
    print(f"Time: {datetime.now().strftime('%I:%M %p')}")

    # Only process inbound guest messages
    if event_type != "message.created":
        return jsonify({"status": "ignored", "reason": "not a message event"}), 200

    message_data = payload.get("data", {})
    direction = message_data.get("direction", "")

    # Only process messages FROM guests (inbound)
    if direction != "inbound":
        return jsonify({"status": "ignored", "reason": "outbound message"}), 200

    # Extract message details
    guest_message = message_data.get("body", "")
    thread_id = message_data.get("thread_id")
    booking_id = message_data.get("booking_id")
    guest_name = message_data.get("guest_name", "Guest")
    property_name = message_data.get("property_name", "the property")

    print(f"Guest: {guest_name}")
    print(f"Property: {property_name}")
    print(f"Message: {guest_message[:100]}")

    # Log everything regardless
    log_message({
        "type": "inbound_message",
        "guest": guest_name,
        "property": property_name,
        "message": guest_message,
        "thread_id": thread_id,
        "booking_id": booking_id,
        "night_shift": is_night_shift()
    })

    # If not night shift, leave it for the morning team
    if not is_night_shift():
        print("Not night shift — leaving for morning team")
        return jsonify({
            "status": "skipped",
            "reason": "not night shift hours",
            "message": "Morning team will handle"
        }), 200

    # Get thread history for context
    thread_history = get_thread_messages(thread_id) if thread_id else []

    # Get booking details for more context
    booking = get_booking_details(booking_id) if booking_id else {}
    if booking:
        guest_data = booking.get("guest", {})
        guest_name = f"{guest_data.get('first_name', guest_name)}".strip()
        property_name = booking.get("property_name", property_name)

    # Ask the AI what to do
    print("Evaluating with AI...")
    try:
        result = evaluate_message(
            guest_message=guest_message,
            guest_name=guest_name,
            property_name=property_name,
            thread_history=thread_history
        )
    except Exception as e:
        print(f"AI evaluation error: {e}")
        return jsonify({"status": "error", "reason": str(e)}), 500

    urgency = result.get("urgency", "wait")
    reasoning = result.get("reasoning", "")
    response_text = result.get("response")

    print(f"Decision: {urgency.upper()}")
    print(f"Reasoning: {reasoning}")

    if urgency == "urgent" and response_text and thread_id:
        print(f"Sending response: {response_text[:100]}...")
        sent = send_ownerrez_message(thread_id, response_text)
        print(f"Sent: {sent}")

        log_message({
            "type": "ai_response_sent",
            "guest": guest_name,
            "property": property_name,
            "urgency": urgency,
            "reasoning": reasoning,
            "response": response_text,
            "sent_successfully": sent
        })

        return jsonify({
            "status": "responded",
            "urgency": urgency,
            "reasoning": reasoning,
            "response_sent": sent
        }), 200
    else:
        log_message({
            "type": "message_left_unread",
            "guest": guest_name,
            "property": property_name,
            "urgency": urgency,
            "reasoning": reasoning
        })

        return jsonify({
            "status": "left_unread",
            "urgency": urgency,
            "reasoning": reasoning
        }), 200

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
