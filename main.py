"""
Pareeksha Gurukul v2.3 - STABLE
================================
Root cause fixes:
1. NO file I/O at all - Railway filesystem is ephemeral, files vanish on restart
2. Pure in-memory store (single worker = single process = consistent memory)  
3. Webhook always returns 200 instantly - zero blocking
4. Broadcast runs in separate daemon thread - never blocks webhook
5. Zero Markdown in bot messages - no parse errors possible
6. Webhook registered on startup via background thread (non-blocking)
"""

import os
import json
import logging
import threading
import time
from flask import Flask, request, jsonify, send_from_directory
import requests
from dotenv import load_dotenv

load_dotenv()

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
EVAL_GROUP_ID = os.getenv("EVAL_GROUP_ID", "")
WEBAPP_URL    = os.getenv("WEBAPP_URL", "").rstrip("/")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
PORT          = int(os.getenv("PORT", 8080))

ADMIN_IDS = set(a.strip() for a in ADMIN_IDS_RAW.split(",") if a.strip())
TG_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── IN-MEMORY STORE (no file I/O) ────────────────────────────────────────────
_lock        = threading.Lock()
_question    = ""          # current active question
_submissions = {}          # {str(uid): {name, answer_type, msg_id}}
_students    = {}          # {str(uid): name} - everyone who ever /start-ed

def store_get_question():
    with _lock:
        return _question

def store_set_question(q):
    global _question, _submissions
    with _lock:
        _question    = q
        _submissions = {}   # reset when new question is set

def store_clear_question():
    global _question
    with _lock:
        _question = ""

def store_get_students():
    with _lock:
        return dict(_students)

def store_register_student(uid, name):
    with _lock:
        _students[str(uid)] = name

def store_has_submitted(uid):
    with _lock:
        return str(uid) in _submissions

def store_record_submission(uid, name, answer_type, msg_id=None):
    with _lock:
        _submissions[str(uid)] = {
            "name":        name,
            "answer_type": answer_type,
            "msg_id":      msg_id,
        }

def store_get_stats():
    with _lock:
        q    = _question
        subs = dict(_submissions)
        stus = dict(_students)
    by_type = {"text": 0, "audio": 0, "video": 0}
    for s in subs.values():
        t = s.get("answer_type", "text")
        by_type[t] = by_type.get(t, 0) + 1
    return q, stus, subs, by_type

def store_reset_submissions():
    global _submissions
    with _lock:
        count        = len(_submissions)
        _submissions = {}
    return count

# ── TELEGRAM API ──────────────────────────────────────────────────────────────
def tg(method, payload=None, files=None, timeout=20):
    url = f"{TG_API}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=timeout)
        else:
            r = requests.post(url, json=payload, timeout=timeout)
        res = r.json()
        if not res.get("ok"):
            log.warning(f"TG {method}: {res.get('description', '?')}")
        return res
    except Exception as e:
        log.error(f"TG {method} error: {e}")
        return {"ok": False}

def send(chat_id, text, markup=None):
    p = {"chat_id": str(chat_id), "text": text}
    if markup:
        p["reply_markup"] = markup
    return tg("sendMessage", p)

def is_admin(uid):
    return str(uid) in ADMIN_IDS

def bg(fn, *args, **kwargs):
    """Fire and forget in a daemon thread."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

# ── FLASK ─────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    return jsonify({
        "ok":       True,
        "question": bool(store_get_question()),
        "students": len(store_get_students()),
    })

@app.route("/question")
def api_question():
    q = store_get_question()
    return jsonify({"ok": True, "question": q, "active": bool(q)})

# ── SUBMIT ────────────────────────────────────────────────────────────────────
@app.route("/submit", methods=["POST"])
def submit():
    uid   = request.form.get("student_id",   "").strip()
    name  = request.form.get("student_name", "Student").strip()
    atype = request.form.get("answer_type",  "text").strip()
    text  = request.form.get("text_answer",  "").strip()

    if not uid:
        return jsonify({"ok": False, "error": "Missing student ID"}), 400
    if not BOT_TOKEN or not EVAL_GROUP_ID:
        return jsonify({"ok": False, "error": "Server misconfigured"}), 500

    q = store_get_question()
    if not q:
        return jsonify({"ok": False, "error": "No active question"}), 400
    if store_has_submitted(uid):
        return jsonify({"ok": False, "error": "Already submitted"}), 400

    # Read file bytes NOW (before thread — request context won't exist in thread)
    fbytes, mime, fname = None, None, None
    if atype in ("audio", "video"):
        f = request.files.get("file")
        if not f:
            return jsonify({"ok": False, "error": "No file attached"}), 400
        fbytes = f.read()
        mime   = f.content_type or "application/octet-stream"
        fname  = f.filename or f"answer.{atype}"

    # Block duplicate immediately, forward in background
    store_record_submission(uid, name, atype)
    bg(_forward, uid, name, q, atype, text, fbytes, mime, fname)

    return jsonify({"ok": True, "message": "Submitted successfully!"})


def _forward(uid, name, question, atype, text, fbytes, mime, fname):
    caption = (
        f"NEW SUBMISSION\n"
        f"Student: {name} (ID: {uid})\n"
        f"Question: {question}\n"
        f"Type: {atype.upper()}"
    )
    if atype == "text":
        caption += f"\n\nAnswer:\n{text[:3500]}"

    kb = {"inline_keyboard": [[
        {"text": "1 star",  "callback_data": f"rate|{uid}|1"},
        {"text": "2 stars", "callback_data": f"rate|{uid}|2"},
        {"text": "3 stars", "callback_data": f"rate|{uid}|3"},
        {"text": "4 stars", "callback_data": f"rate|{uid}|4"},
        {"text": "5 stars", "callback_data": f"rate|{uid}|5"},
    ]]}

    if atype == "text":
        res = tg("sendMessage", {
            "chat_id":      EVAL_GROUP_ID,
            "text":         caption,
            "reply_markup": kb,
        })
    else:
        cap = caption[:1024] if len(caption) > 1024 else caption
        method = "sendAudio" if atype == "audio" else "sendVideo"
        key    = "audio"     if atype == "audio" else "video"
        res = tg(method,
            payload={
                "chat_id":      EVAL_GROUP_ID,
                "caption":      cap,
                "reply_markup": json.dumps(kb),
            },
            files={key: (fname, fbytes, mime)},
            timeout=60,
        )

    if res.get("result"):
        store_record_submission(uid, name, atype, res["result"].get("message_id"))
        log.info(f"Forwarded submission: {name} ({uid}) [{atype}]")
    else:
        log.error(f"Forward failed for {uid}: {res}")


# ── WEBHOOK ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """Return 200 IMMEDIATELY. Process in background."""
    update = request.get_json(silent=True)
    if update:
        bg(_process, update)
    return "ok", 200


def _process(update):
    try:
        cb  = update.get("callback_query")
        msg = update.get("message", {})
        if cb:
            _on_callback(cb)
        elif msg:
            _on_message(msg)
    except Exception as e:
        log.error(f"_process error: {e}", exc_info=True)


def _on_callback(cb):
    data       = cb.get("data", "")
    evaluator  = cb.get("from", {}).get("first_name", "Evaluator")
    msg        = cb.get("message", {})
    chat_id    = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    cb_id      = cb["id"]

    if data.startswith("rate|"):
        parts = data.split("|")
        if len(parts) != 3:
            return
        _, uid, s = parts
        stars = int(s)
        star_str = "⭐" * stars

        send(uid,
            f"Your answer has been evaluated!\n\n"
            f"Rating: {star_str} ({stars} out of 5)\n"
            f"Evaluated by: {evaluator}\n\n"
            f"Well done! Use /start to submit for the next question."
        )
        tg("editMessageReplyMarkup", {
            "chat_id":    chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": [[
                {"text": f"Rated {stars}/5 by {evaluator}", "callback_data": "done"}
            ]]}
        })
        tg("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"Done! Rated {stars}/5. Student notified."
        })
        log.info(f"Rated {uid}: {stars}/5 by {evaluator}")

    elif data == "done":
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Already rated."})


def _on_message(msg):
    text    = (msg.get("text") or "").strip()
    user    = msg.get("from", {})
    uid     = str(user.get("id", ""))
    name    = (user.get("first_name") or "Student").strip()
    chat_id = msg.get("chat", {}).get("id")

    if not text or not uid:
        return

    # Always register student
    store_register_student(uid, name)

    cmd = text.split()[0].split("@")[0].lower()

    if cmd == "/start":
        _cmd_start(uid, name, chat_id)

    elif cmd == "/setquestion":
        _cmd_setquestion(uid, name, chat_id, text)

    elif cmd == "/clearquestion":
        if not is_admin(uid):
            send(chat_id, "Not authorized.")
            return
        store_clear_question()
        send(chat_id, "Question cleared. Submissions are now disabled.")

    elif cmd == "/viewquestion":
        if not is_admin(uid):
            send(chat_id, "Not authorized.")
            return
        q = store_get_question()
        send(chat_id, f"Current question:\n\n{q}" if q else "No active question set.")

    elif cmd == "/stats":
        if not is_admin(uid):
            send(chat_id, "Not authorized.")
            return
        _cmd_stats(chat_id)

    elif cmd == "/resetsubmissions":
        if not is_admin(uid):
            send(chat_id, "Not authorized.")
            return
        count = store_reset_submissions()
        send(chat_id, f"Reset {count} submission(s). Students can submit again.")

    elif cmd == "/broadcast":
        if not is_admin(uid):
            send(chat_id, "Not authorized.")
            return
        _cmd_broadcast(uid, chat_id)

    elif cmd == "/help":
        _cmd_help(uid, chat_id)

    else:
        if msg.get("chat", {}).get("type") == "private":
            send(chat_id, "Use /start to begin.")


def _cmd_start(uid, name, chat_id):
    q = store_get_question()
    if is_admin(uid):
        q_prev = (q[:100] + "...") if len(q) > 100 else (q or "None")
        send(chat_id,
            f"Admin Panel - {name}\n\n"
            f"Commands:\n"
            f"/setquestion [text] - Set today's question\n"
            f"/clearquestion - Disable submissions\n"
            f"/viewquestion - View current question\n"
            f"/stats - View submission stats\n"
            f"/resetsubmissions - Let students resubmit\n"
            f"/broadcast - Notify all students\n\n"
            f"Current question: {q_prev}"
        )
    else:
        url = f"{WEBAPP_URL}?uid={uid}&name={name}"
        markup = {"inline_keyboard": [[
            {"text": "Submit Your Answer", "web_app": {"url": url}}
        ]]} if q else None

        send(chat_id,
            f"Pareeksha Gurukul - Mock Interview Platform\n\n"
            f"Namaste {name}!\n\n"
            + ("Tap the button below to submit your answer.\nYour score will be sent here after evaluation."
               if q else
               "No active question right now. You will be notified when the admin posts a new question. Stay ready!"),
            markup=markup
        )


def _cmd_setquestion(uid, name, chat_id, text):
    if not is_admin(uid):
        send(chat_id, "Not authorized.")
        return
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        send(chat_id,
            "Usage: /setquestion Your question text here\n\n"
            "Example:\n/setquestion Tell us about yourself in 45-60 seconds"
        )
        return
    new_q = parts[1].strip()
    store_set_question(new_q)
    send(chat_id,
        f"Question set successfully!\n\n"
        f"{new_q}\n\n"
        f"Use /broadcast to notify all students."
    )
    log.info(f"Admin {name} ({uid}) set question: {new_q[:80]}")


def _cmd_stats(chat_id):
    q, students, subs, by_type = store_get_stats()
    q_prev = (q[:80] + "...") if len(q) > 80 else (q or "Not set")
    recent = "\n".join([
        f"- {v.get('name','?')} [{v.get('answer_type','?').upper()}]"
        for v in list(subs.values())[-10:]
    ]) or "None yet"
    send(chat_id,
        f"Submission Stats\n\n"
        f"Question: {q_prev}\n\n"
        f"Registered students: {len(students)}\n"
        f"Total submissions: {len(subs)}\n"
        f"  Text: {by_type['text']}\n"
        f"  Audio: {by_type['audio']}\n"
        f"  Video: {by_type['video']}\n\n"
        f"Recent:\n{recent}"
    )


def _cmd_broadcast(uid, chat_id):
    q        = store_get_question()
    students = store_get_students()
    if not q:
        send(chat_id, "No active question. Use /setquestion first.")
        return
    if not students:
        send(chat_id, "No students registered yet. They need to /start the bot first.")
        return
    send(chat_id, f"Broadcasting to {len(students)} student(s)...")
    bg(_do_broadcast, students, q)


def _do_broadcast(students, question):
    ok, fail = 0, 0
    for sid, sname in students.items():
        url = f"{WEBAPP_URL}?uid={sid}&name={sname}"
        res = send(sid,
            f"New question available!\n\n{question}\n\nTap below to submit your answer.",
            markup={"inline_keyboard": [[
                {"text": "Submit Your Answer", "web_app": {"url": url}}
            ]]}
        )
        if res.get("ok"):
            ok += 1
        else:
            fail += 1
        time.sleep(0.05)   # avoid hitting Telegram rate limit
    log.info(f"Broadcast complete: {ok} ok, {fail} failed")


def _cmd_help(uid, chat_id):
    if is_admin(uid):
        send(chat_id,
            "Admin Commands:\n\n"
            "/setquestion [text] - Set today's question\n"
            "/clearquestion - Disable submissions\n"
            "/viewquestion - See active question\n"
            "/stats - Submission breakdown\n"
            "/resetsubmissions - Allow re-submissions\n"
            "/broadcast - Notify all students\n"
            "/start - Admin panel"
        )
    else:
        send(chat_id,
            "How to submit:\n\n"
            "1. Tap Submit Your Answer\n"
            "2. Choose Text, Audio, or Video\n"
            "3. Submit your answer\n"
            "4. Receive your star rating in DM\n\n"
            "Use /start to open the form."
        )


# ── WEBHOOK REGISTRATION ──────────────────────────────────────────────────────
def _register_webhook():
    """Called in background after server starts."""
    time.sleep(2)   # wait for server to be ready
    if not BOT_TOKEN or not WEBAPP_URL:
        log.warning("BOT_TOKEN or WEBAPP_URL missing - webhook not registered")
        return
    hook = f"{WEBAPP_URL}/webhook"
    res  = tg("setWebhook", {"url": hook, "drop_pending_updates": True, "max_connections": 10})
    if res.get("ok"):
        log.info(f"Webhook registered: {hook}")
    else:
        log.error(f"Webhook registration failed: {res}")


# ── START ─────────────────────────────────────────────────────────────────────
bg(_register_webhook)
log.info(f"Starting Pareeksha Gurukul on port {PORT}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
