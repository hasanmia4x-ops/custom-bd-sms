from datetime import datetime
import logging
import os
import re
import sqlite3
import threading
from html import escape

import requests
import telebot
from PIL import Image, ImageDraw, ImageFilter
from telebot import types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = 6339678222
ADMIN_PASSWORD = "hasanbro"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")

OCS_BASE_URL = os.getenv("OCS_BASE_URL", "https://sms.ocs-api.top/api").rstrip("/")
OCS_ENDPOINT = os.getenv("OCS_ENDPOINT", f"{OCS_BASE_URL}/v3/sandbox/send-sms")
OCS_API_KEY = os.environ.get("OCS_API_KEY", "")
DEFAULT_SENDER_ID = "SENDER_ID"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNER_PATH = os.path.join(BASE_DIR, "apk_converter_banner.png")
DB_PATH = os.path.join(BASE_DIR, "custom_sms_bot.db")

WELCOME_BONUS = 3
REFERRAL_BONUS = 1
SMS_COST = 1
MIN_MESSAGE_LENGTH = 1
MAX_MESSAGE_LENGTH = 500
DAILY_LIMIT = 20
RATE_LIMIT_SECONDS = 30
REQUIRE_RECIPIENT_CONSENT = True
API_TIMEOUT = 20

BAD_WORDS = [
    "xxx", "sex", "porn", "nude", "xvideo", "xvideos", "hentai", "blowjob", "handjob",
    "বাল", "খানকি", "মাগি", "চোদা", "চুদ", "চুদা", "বোকাচোদা", "বোকচোদা", "চোদনা",
    "হারামী", "হারামি", "শালা", "শালার", "কুত্তা", "kuttar", "madarchod", "bhenchod",
    "mc", "bc", "fuck", "bitch", "bastard", "slut", "whore", "dick", "pussy", "cock"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("custom_sms_bot")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit("Set a valid BOT_TOKEN environment variable before running.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
sessions = {}
db_lock = threading.Lock()


def ensure_banner():
    if os.path.exists(BANNER_PATH):
        return
    w, h = 1200, 675
    im = Image.new("RGB", (w, h), "#10162d")
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = (x / w + y / h) / 2
            px[x, y] = (int(15 + 18 * t), int(22 + 26 * t), int(53 + 55 * t))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((760, -160, 1320, 400), fill=(101, 88, 255, 95))
    gd.ellipse((-180, 410, 430, 1030), fill=(0, 207, 191, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    im = Image.alpha_composite(im.convert("RGBA"), glow)
    d = ImageDraw.Draw(im)
    phone = (435, 85, 765, 590)
    d.rounded_rectangle(phone, radius=42, fill="#202a4b", outline="#6976b6", width=7)
    d.rounded_rectangle((458, 132, 742, 535), radius=14, fill="#0c1329")
    d.rounded_rectangle((555, 101, 645, 119), radius=9, fill="#6976b6")
    d.ellipse((579, 551, 621, 593), fill="#6976b6")
    d.rounded_rectangle((535, 230, 665, 390), radius=22, fill="#665cff")
    d.polygon([(600, 198), (646, 240), (554, 240)], fill="#8d86ff")
    d.line((574, 219, 560, 200), fill="#8d86ff", width=8)
    d.line((626, 219, 640, 200), fill="#8d86ff", width=8)
    d.ellipse((568, 278, 584, 294), fill="white")
    d.ellipse((616, 278, 632, 294), fill="white")
    d.rounded_rectangle((570, 330, 630, 347), radius=8, fill="#b9b5ff")
    im.convert("RGB").save(BANNER_PATH, "PNG", optimize=True)


def connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    con.execute("PRAGMA journal_mode=WAL;")
    return con


def setup_db():
    with db_lock:
        con = connect()
        cur = con.cursor()
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY,
            full_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            balance INTEGER DEFAULT {WELCOME_BONUS},
            referrals INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            last_sms_time INTEGER DEFAULT 0,
            daily_count INTEGER DEFAULT 0,
            daily_date TEXT DEFAULT ''
        )
        """)
        cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
        for col, ddl in [
            ("full_name", "ALTER TABLE users ADD COLUMN full_name TEXT DEFAULT ''"),
            ("username", "ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''"),
            ("banned", "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0"),
            ("ban_reason", "ALTER TABLE users ADD COLUMN ban_reason TEXT DEFAULT ''"),
            ("last_sms_time", "ALTER TABLE users ADD COLUMN last_sms_time INTEGER DEFAULT 0"),
            ("daily_count", "ALTER TABLE users ADD COLUMN daily_count INTEGER DEFAULT 0"),
            ("daily_date", "ALTER TABLE users ADD COLUMN daily_date TEXT DEFAULT ''"),
        ]:
            if col not in cols:
                cur.execute(ddl)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            name TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid INTEGER,
            number TEXT,
            message TEXT,
            status TEXT,
            cost INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ''
        )
        """)
        log_cols = [r[1] for r in cur.execute("PRAGMA table_info(logs)").fetchall()]
        if "created_at" not in log_cols:
            cur.execute("ALTER TABLE logs ADD COLUMN created_at TEXT DEFAULT ''")

        cur.execute("INSERT OR IGNORE INTO settings(name, value) VALUES('sender_id', ?)", (DEFAULT_SENDER_ID,))
        cur.execute("INSERT OR IGNORE INTO settings(name, value) VALUES('notice', '')")
        cur.execute("INSERT OR IGNORE INTO settings(name, value) VALUES('api_key', ?)", (OCS_API_KEY,))
        cur.execute("INSERT OR IGNORE INTO settings(name, value) VALUES('endpoint', ?)", (OCS_ENDPOINT,))
        con.commit()
        con.close()


def get_setting(name: str, default: str = "") -> str:
    with db_lock:
        con = connect()
        row = con.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        con.close()
    return row[0] if row and row[0] is not None else default


def set_setting(name: str, value: str):
    with db_lock:
        con = connect()
        con.execute("INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)", (name, value))
        con.commit()
        con.close()


def sync_user_profile(user, ref_id: int | None = None) -> bool:
    if not user:
        return False
    uid = user.id
    full_name = (user.full_name or "").strip()
    username = f"@{user.username}" if user.username else "No Username"
    today = datetime.now().strftime("%Y-%m-%d")
    is_new = False

    with db_lock:
        con = connect()
        cur = con.cursor()
        exists = cur.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone()
        if not exists:
            is_new = True
            cur.execute(
                """
                INSERT INTO users(uid, full_name, username, balance, referrals, banned, ban_reason, last_sms_time, daily_count, daily_date)
                VALUES(?, ?, ?, ?, 0, 0, '', 0, 0, ?)
                """,
                (uid, full_name, username, WELCOME_BONUS, today),
            )
            if ref_id and ref_id != uid:
                ref_row = cur.execute("SELECT uid, banned FROM users WHERE uid=?", (ref_id,)).fetchone()
                if ref_row and ref_row[1] == 0:
                    cur.execute(
                        "UPDATE users SET balance = balance + ?, referrals = referrals + 1 WHERE uid=?",
                        (REFERRAL_BONUS, ref_id),
                    )
        else:
            cur.execute("UPDATE users SET full_name=?, username=? WHERE uid=?", (full_name, username, uid))
        con.commit()
        con.close()
    return is_new


def get_user_info(uid: int) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    with db_lock:
        con = connect()
        row = con.execute(
            """
            SELECT uid, full_name, username, balance, referrals, banned, ban_reason, last_sms_time, daily_count, daily_date
            FROM users WHERE uid=?
            """,
            (uid,),
        ).fetchone()
        con.close()

    if not row:
        return {
            "uid": uid,
            "full_name": "Unknown",
            "username": "No Username",
            "balance": 0,
            "referrals": 0,
            "banned": 0,
            "ban_reason": "",
            "last_sms_time": 0,
            "daily_count": 0,
            "daily_date": today,
        }

    daily_cnt = row[8] if row[9] == today else 0
    return {
        "uid": row[0],
        "full_name": row[1] or "Unknown",
        "username": row[2] or "No Username",
        "balance": row[3],
        "referrals": row[4],
        "banned": row[5],
        "ban_reason": row[6] or "",
        "last_sms_time": row[7] or 0,
        "daily_count": daily_cnt,
        "daily_date": row[9] or today,
    }


def get_ban_info(uid: int) -> tuple[bool, str]:
    if uid == ADMIN_ID:
        return False, ""
    info = get_user_info(uid)
    if info["banned"] == 1:
        return True, (info["ban_reason"] or "নিয়ম ভঙ্গ করার কারণে ব্যান করা হয়েছে।")
    return False, ""


def get_all_active_user_ids() -> list[int]:
    with db_lock:
        con = connect()
        rows = con.execute("SELECT uid FROM users WHERE banned=0").fetchall()
        con.close()
    return [r[0] for r in rows]


def get_admin_contact() -> str:
    if ADMIN_USERNAME:
        return f"@{ADMIN_USERNAME.lstrip('@')}"
    try:
        chat = bot.get_chat(ADMIN_ID)
        if chat.username:
            return f"@{chat.username}"
    except Exception:
        pass
    return f"ID: {ADMIN_ID}"


def block_if_banned_chat(chat_id: int, user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    banned, reason = get_ban_info(user_id)
    if not banned:
        return False
    admin_tag = escape(get_admin_contact())
    bot.send_message(
        chat_id,
        f"🚫 <b>You are banned!</b>\n\n"
        f"📝 <b>Reason:</b> {escape(reason)}\n"
        f"👑 <b>Admin:</b> {admin_tag}",
    )
    return True


def has_bad_message(text: str) -> tuple[bool, str]:
    lower = text.lower()
    compact = re.sub(r"[\W_]+", "", lower)
    for word in BAD_WORDS:
        w_lower = word.lower()
        w_compact = re.sub(r"[\W_]+", "", w_lower)
        if re.fullmatch(r"[a-z]{1,3}", w_lower):
            if re.search(rf"\b{re.escape(w_lower)}\b", lower):
                return True, word
        else:
            if w_lower in lower or (w_compact and w_compact in compact):
                return True, word
    return False, ""


def normalize_bd_number(raw: str) -> str | None:
    cleaned = re.sub(r"[\s\-]", "", (raw or "").strip())
    if not re.fullmatch(r"(?:\+?88)?01[3-9][0-9]{8}", cleaned):
        return None
    if cleaned.startswith("+88"):
        return cleaned[1:]
    if cleaned.startswith("88"):
        return cleaned
    return "88" + cleaned


def send_sms_via_ocs_api(number: str, message: str) -> tuple[bool, str]:
    api_key = get_setting("api_key", OCS_API_KEY).strip()
    endpoint = get_setting("endpoint", OCS_ENDPOINT).strip() or OCS_ENDPOINT
    if not api_key:
        return False, "API Key সেট করা নেই। /setapikey দিয়ে সেট করুন।"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "number": number,
        "message": message,
    }

    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=API_TIMEOUT)
        raw_text = (resp.text or "").strip()
        parsed = None
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None

        if resp.status_code in (200, 201) and isinstance(parsed, dict):
            status_val = str(parsed.get("status", "")).lower().strip()
            msg_val = str(parsed.get("message", "")).strip()
            data_obj = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            mode_val = data_obj.get("mode", "unknown")
            sender_val = data_obj.get("senderid", get_setting("sender_id", DEFAULT_SENDER_ID))
            if status_val in ("success", "ok", "queued", "sent"):
                return True, f"Mode: {mode_val} | SenderID: {sender_val} | {msg_val[:140]}"
            return False, f"API Error: {msg_val or raw_text[:200]}"

        if resp.status_code in (200, 201) and raw_text:
            return True, f"Response: {raw_text[:200]}"

        detail = parsed.get("message") or parsed.get("error") if isinstance(parsed, dict) else raw_text
        return False, f"HTTP {resp.status_code}: {str(detail)[:200] or 'Unknown error'}"
    except Exception as exc:
        logger.exception("OCS SMS API error")
        return False, f"Exception: {str(exc)}"


def kb_main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚀 Send Custom SMS", callback_data="menu:new"))
    kb.add(
        types.InlineKeyboardButton("👤 My Account", callback_data="menu:account"),
        types.InlineKeyboardButton("🔗 Refer & Earn", callback_data="menu:refer"),
    )
    kb.add(
        types.InlineKeyboardButton("📘 How it works", callback_data="menu:help"),
        types.InlineKeyboardButton("ℹ️ About", callback_data="menu:about"),
    )
    return kb


def kb_cancel():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data="flow:cancel"))
    return kb


def kb_back():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Back to menu", callback_data="menu:home"))
    return kb


def kb_confirm():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Send SMS Now", callback_data="flow:send"),
        types.InlineKeyboardButton("✏️ Edit Number", callback_data="flow:editnum"),
    )
    kb.add(
        types.InlineKeyboardButton("💬 Edit Message", callback_data="flow:editmsg"),
        types.InlineKeyboardButton("✖️ Cancel", callback_data="flow:cancel"),
    )
    return kb


def progress(step, label):
    cells = ["✅" if i < step else ("🔵" if i == step else "⚪") for i in range(5)]
    return f"<b>Step {step + 1}/5 — {escape(label)}</b>\n{'  '.join(cells)}\n"


def reset(chat_id):
    sessions.pop(chat_id, None)


def safe_text(value):
    return escape(value or "")


def flow_screen(chat_id, text, markup=None, source=None):
    session = sessions.get(chat_id)
    if session is None:
        return None
    msg = source if source is not None else session.get("ui_message")
    if msg is None:
        msg = bot.send_message(chat_id, text, reply_markup=markup)
    elif msg.content_type == "photo":
        try:
            bot.edit_message_caption(text, chat_id, msg.message_id, reply_markup=markup)
        except Exception:
            msg = bot.send_message(chat_id, text, reply_markup=markup)
    else:
        try:
            bot.edit_message_text(text, chat_id, msg.message_id, reply_markup=markup)
        except Exception:
            msg = bot.send_message(chat_id, text, reply_markup=markup)
    session["ui_message"] = msg
    return msg


def flow_next(chat_id, text, handler, *args, source=None, markup=None):
    msg = flow_screen(chat_id, text, markup or kb_cancel(), source=source)
    bot.register_next_step_handler(msg, handler, *args)
    return msg


def edit_ui_message(message, text, markup=None):
    if not message:
        return
    try:
        if message.content_type == "photo":
            bot.edit_message_caption(text, message.chat.id, message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)
    except Exception as exc:
        logger.debug("UI update failed: %s", exc)


def animate_sms_dispatch(message, stop_event):
    frames = ["◐", "◓", "◑", "◒"]
    stages = [
        ("Validating recipient & quota", "📱", 25),
        ("Checking content safety", "🛡️", 45),
        ("Connecting to OCS API", "🌐", 70),
        ("Dispatching custom SMS", "📡", 88),
        ("Finalizing receipt", "📦", 96),
    ]
    index = 0
    while not stop_event.is_set():
        label, icon, percent = stages[index % len(stages)]
        filled = max(1, percent // 10)
        bar = "🟦" * filled + "▫️" * (10 - filled)
        text = (
            f"<b>{frames[index % len(frames)]} Custom SMS Studio</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>DISPATCH IN PROGRESS</b>\n\n"
            f"{icon} <b>{label}</b>\n"
            f"{bar} <b>{percent}%</b>\n\n"
            "☕ Please wait while the SMS service works…\n"
            "<i>Your request is being processed securely.</i>"
        )
        edit_ui_message(message, text)
        index += 1
        stop_event.wait(1.3)


def send_home(chat_id, intro=True):
    notice = get_setting("notice", "").strip()
    notice_line = f"\n📢 <b>Notice:</b> {safe_text(notice)}\n" if notice else "\n"
    text = (
        "<b>Custom SMS Studio</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💬 Custom SMS → 📱 Instant Dispatch\n\n"
        f"🎁 <b>Welcome Bonus:</b> <code>{WELCOME_BONUS}</code> coins  •  "
        f"👥 <b>Referral:</b> <code>{REFERRAL_BONUS}</code> coin  •  "
        f"💸 <b>Cost:</b> <code>{SMS_COST}</code> coin/SMS"
        f"{notice_line}\n"
        "<i>Fast • Filtered • OCS API Ready</i>"
        if intro else
        "<b>Custom SMS Studio</b>\n\nChoose an action below to continue:"
    )
    if os.path.exists(BANNER_PATH):
        with open(BANNER_PATH, "rb") as photo:
            bot.send_photo(chat_id, photo, caption=text, reply_markup=kb_main_menu())
    else:
        bot.send_message(chat_id, text, reply_markup=kb_main_menu())


HELP_TEXT = (
    "<b>📘 How it works</b>\n\n"
    "<b>1.</b> Choose <b>Send Custom SMS</b>\n"
    "<b>2.</b> Enter recipient number (<code>017XXXXXXXX</code> or <code>88017XXXXXXXX</code>)\n"
    f"<b>3.</b> Enter your message ({MIN_MESSAGE_LENGTH}–{MAX_MESSAGE_LENGTH} characters)\n"
    "<b>4.</b> Review details and confirm\n"
    "<b>5.</b> Dispatch via OCS API\n\n"
    f"⚠️ Only send messages to consenting recipients. Cooldown: <code>{RATE_LIMIT_SECONDS}s</code>, Daily limit: <code>{DAILY_LIMIT}</code>."
)

ABOUT_TEXT = (
    "<b>ℹ️ About Custom SMS Studio</b>\n\n"
    "A step-by-step Telegram interface for custom SMS dispatch via OCS API.\n\n"
    "<b>Features:</b> Coins, referrals, daily &amp; rate limits, bad-word filter, admin controls.\n"
    f"<b>Endpoint:</b> <code>{safe_text(OCS_ENDPOINT)}</code>"
)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    user = message.from_user
    chat_id = message.chat.id
    if not user or block_if_banned_chat(chat_id, user.id):
        return
    reset(chat_id)
    ref_id = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        try:
            ref_id = int(parts[1].strip())
        except ValueError:
            ref_id = None
    is_new = sync_user_profile(user, ref_id=ref_id)
    if is_new and ref_id and ref_id != user.id:
        try:
            bot.send_message(ref_id, f"🎉 নতুন Referral সফল হয়েছে! আপনি +{REFERRAL_BONUS} Coin পেয়েছেন।")
        except Exception:
            pass
    send_home(chat_id, intro=True)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    if not message.from_user or block_if_banned_chat(message.chat.id, message.from_user.id):
        return
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=kb_back())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if not message.from_user or block_if_banned_chat(message.chat.id, message.from_user.id):
        return
    reset(message.chat.id)
    bot.send_message(message.chat.id, "✖️ <b>Cancelled</b>\n\nNothing was changed.", reply_markup=kb_main_menu())


@bot.message_handler(commands=["sendsms"])
def cmd_sendsms(message):
    if not message.from_user or block_if_banned_chat(message.chat.id, message.from_user.id):
        return
    start_flow(message.chat.id)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if not message.from_user or block_if_banned_chat(message.chat.id, message.from_user.id):
        return
    sync_user_profile(message.from_user)
    uinfo = get_user_info(message.from_user.id)
    api_set = "Configured ✅" if get_setting("api_key", OCS_API_KEY).strip() else "Not Set ❌"
    bot.send_message(
        message.chat.id,
        "<b>⚙️ System Status</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>OCS API:</b> {api_set}\n"
        f"📊 <b>Today's SMS:</b> <code>{uinfo['daily_count']}/{DAILY_LIMIT}</code>\n"
        f"⏱️ <b>Cooldown:</b> <code>{RATE_LIMIT_SECONDS}s</code>\n"
        f"💰 <b>Balance:</b> <code>{uinfo['balance']}</code> coins",
        reply_markup=kb_back(),
    )


@bot.message_handler(commands=["notice"])
def cmd_notice(message):
    if not message.from_user or block_if_banned_chat(message.chat.id, message.from_user.id):
        return
    notice = get_setting("notice", "").strip()
    txt = "<b>📢 Notice Board</b>\n\nℹ️ No active notice right now." if not notice else f"<b>📢 Notice Board</b>\n\n{safe_text(notice)}"
    bot.send_message(message.chat.id, txt, reply_markup=kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:home")
def cb_home(call):
    if not call.from_user or block_if_banned_chat(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id)
    edit_ui_message(call.message, "<b>Custom SMS Studio</b>\n\nChoose an action below to continue:", kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "menu:help")
def cb_help(call):
    bot.answer_callback_query(call.id)
    edit_ui_message(call.message, HELP_TEXT, kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:about")
def cb_about(call):
    bot.answer_callback_query(call.id)
    edit_ui_message(call.message, ABOUT_TEXT, kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:account")
def cb_account(call):
    if not call.from_user or block_if_banned_chat(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    sync_user_profile(call.from_user)
    info = get_user_info(call.from_user.id)
    bot.answer_callback_query(call.id)
    txt = (
        "<b>👤 My Account</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🙋 <b>Name:</b> {safe_text(info['full_name'])}\n"
        f"🔗 <b>Username:</b> {safe_text(info['username'])}\n"
        f"🆔 <b>User ID:</b> <code>{info['uid']}</code>\n"
        f"💰 <b>Balance:</b> <code>{info['balance']}</code> coins\n"
        f"👥 <b>Referrals:</b> <code>{info['referrals']}</code>\n"
        f"📨 <b>Today's SMS:</b> <code>{info['daily_count']}/{DAILY_LIMIT}</code>"
    )
    edit_ui_message(call.message, txt, kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:refer")
def cb_refer(call):
    if not call.from_user or block_if_banned_chat(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    me = bot.get_me()
    link = f"https://t.me/{me.username}?start={call.from_user.id}"
    txt = (
        "<b>🔗 Refer &amp; Earn</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<code>{safe_text(link)}</code>\n\n"
        f"🎁 Earn <b>{REFERRAL_BONUS}</b> coin per valid referral!"
    )
    edit_ui_message(call.message, txt, kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:new")
def cb_new(call):
    if not call.from_user or block_if_banned_chat(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    start_flow(call.message.chat.id, edit=call.message)


@bot.callback_query_handler(func=lambda c: c.data == "flow:cancel")
def cb_cancel(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id, "Cancelled")
    edit_ui_message(call.message, "✖️ <b>Cancelled</b>\n\nChoose an action below:", kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "flow:editnum")
def cb_editnum(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id)
    sessions[chat_id]["ui_message"] = call.message
    flow_next(
        chat_id,
        progress(0, "Number") + "📱 <b>Send the new recipient number</b>\n\nExample: <code>017XXXXXXXX</code> or <code>88017XXXXXXXX</code>.",
        step_number,
        True,
        source=call.message,
    )


@bot.callback_query_handler(func=lambda c: c.data == "flow:editmsg")
def cb_editmsg(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id)
    sessions[chat_id]["ui_message"] = call.message
    flow_next(
        chat_id,
        progress(1, "Message") + f"💬 <b>Send the new custom SMS</b>\n\nLength: <code>{MIN_MESSAGE_LENGTH}–{MAX_MESSAGE_LENGTH}</code> characters.",
        step_message,
        True,
        source=call.message,
    )


@bot.callback_query_handler(func=lambda c: c.data == "flow:send")
def cb_send(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id if call.from_user else chat_id
    if block_if_banned_chat(chat_id, user_id):
        bot.answer_callback_query(call.id)
        return
    if chat_id not in sessions or "message" not in sessions[chat_id]:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id, "Dispatch started")
    sessions[chat_id]["user_id"] = user_id
    sessions[chat_id]["ui_message"] = call.message
    flow_screen(
        chat_id,
        progress(4, "Dispatch") + "⚙️ <b>Dispatching your SMS…</b>\n\n<i>Please wait while OCS API processes the request.</i>",
        source=call.message,
    )
    threading.Thread(target=dispatch_sms_job, args=(chat_id,), daemon=True).start()


def start_flow(chat_id, edit=None):
    uinfo = get_user_info(chat_id)
    if uinfo["balance"] < SMS_COST:
        msg_txt = (
            "⚠️ <b>Insufficient Coins</b>\n\n"
            f"You need at least <code>{SMS_COST}</code> coin to send an SMS.\n"
            f"Current balance: <code>{uinfo['balance']}</code> coins."
        )
        if edit is not None:
            edit_ui_message(edit, msg_txt, kb_main_menu())
        else:
            bot.send_message(chat_id, msg_txt, reply_markup=kb_main_menu())
        return

    if chat_id != ADMIN_ID and uinfo["daily_count"] >= DAILY_LIMIT:
        msg_txt = (
            "⚠️ <b>Daily Limit Reached</b>\n\n"
            f"You have reached today's limit of <code>{DAILY_LIMIT}</code> SMS."
        )
        if edit is not None:
            edit_ui_message(edit, msg_txt, kb_main_menu())
        else:
            bot.send_message(chat_id, msg_txt, reply_markup=kb_main_menu())
        return

    sessions[chat_id] = {"user_id": chat_id}
    consent_txt = "\n⚠️ <i>Only send messages to consenting recipients.</i>" if REQUIRE_RECIPIENT_CONSENT else ""
    text = (
        progress(0, "Number")
        + "📱 <b>What is the recipient's phone number?</b>\n\n"
        f"Send a valid BD number (e.g. <code>017XXXXXXXX</code> or <code>88017XXXXXXXX</code>).{consent_txt}"
    )
    flow_next(chat_id, text, step_number, False, source=edit)


def step_number(message, edit_only=False):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    normalized = normalize_bd_number(message.text or "")
    if not normalized:
        flow_next(
            chat_id,
            progress(0, "Number")
            + "⚠️ <b>Invalid phone number</b>\n\nPlease send a valid number like <code>017XXXXXXXX</code> or <code>88017XXXXXXXX</code>:",
            step_number,
            edit_only,
        )
        return
    sessions[chat_id]["number"] = normalized
    if edit_only and "message" in sessions[chat_id]:
        show_confirmation(chat_id)
        return
    flow_next(
        chat_id,
        progress(1, "Message")
        + f"✅ Number saved: <code>{safe_text(normalized)}</code>\n\n"
        f"💬 <b>Enter your custom SMS</b>\nSend text between <code>{MIN_MESSAGE_LENGTH}</code> and <code>{MAX_MESSAGE_LENGTH}</code> characters.",
        step_message,
        False,
    )


def step_message(message, edit_only=False):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    msg_text = (message.text or "").strip()
    if len(msg_text) < MIN_MESSAGE_LENGTH or len(msg_text) > MAX_MESSAGE_LENGTH:
        flow_next(
            chat_id,
            progress(1, "Message")
            + f"⚠️ <b>Invalid message length</b>\n\nMust be <code>{MIN_MESSAGE_LENGTH}–{MAX_MESSAGE_LENGTH}</code> characters. Try again:",
            step_message,
            edit_only,
        )
        return

    is_bad, matched = has_bad_message(msg_text)
    if is_bad:
        uinfo = get_user_info(chat_id)
        admin_tag = escape(get_admin_contact())
        try:
            bot.send_message(
                ADMIN_ID,
                f"🚨 <b>BAD SMS BLOCKED</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"🙋 <b>Name:</b> {safe_text(uinfo['full_name'])}\n"
                f"🔗 <b>Username:</b> {safe_text(uinfo['username'])}\n"
                f"🆔 <b>User ID:</b> <code>{chat_id}</code>\n"
                f"📱 <b>Number:</b> <code>{safe_text(sessions[chat_id].get('number', ''))}</code>\n"
                f"💬 <b>SMS:</b> {safe_text(msg_text)}\n"
                f"⚠️ <b>Filter:</b> <code>{safe_text(matched)}</code>\n"
                f"👑 <b>Admin:</b> {admin_tag}",
            )
        except Exception:
            pass
        flow_next(
            chat_id,
            progress(2, "Filter")
            + "🚫 <b>Blocked by content filter</b>\n\nRestricted words detected. No coins were deducted. Send a clean message:",
            step_message,
            edit_only,
        )
        return

    sessions[chat_id]["message"] = msg_text
    show_confirmation(chat_id)


def show_confirmation(chat_id):
    session = sessions[chat_id]
    uinfo = get_user_info(chat_id)
    text = (
        progress(3, "Review")
        + "<b>📋 Ready to dispatch SMS</b>\n\n"
        f"📱 <b>Number:</b> <code>{safe_text(session['number'])}</code>\n"
        f"💬 <b>Message:</b> {safe_text(session['message'])}\n"
        f"💸 <b>Cost:</b> <code>{SMS_COST}</code> coin (Balance: <code>{uinfo['balance']}</code>)\n\n"
        "Everything looks good. Send this SMS now?"
    )
    flow_screen(chat_id, text, kb_confirm())


def dispatch_sms_job(chat_id):
    session = sessions.get(chat_id)
    if not session:
        return
    uid = session.get("user_id", chat_id)
    number = session["number"]
    msg_text = session["message"]
    ui_message = session.get("ui_message")

    stop_event = threading.Event()
    anim_thread = (
        threading.Thread(target=animate_sms_dispatch, args=(ui_message, stop_event), daemon=True)
        if ui_message else None
    )
    if anim_thread:
        anim_thread.start()

    try:
        uinfo = get_user_info(uid)
        now_ts = int(datetime.now().timestamp())
        today = datetime.now().strftime("%Y-%m-%d")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if uid != ADMIN_ID:
            elapsed = now_ts - int(uinfo["last_sms_time"] or 0)
            if uinfo["last_sms_time"] and elapsed < RATE_LIMIT_SECONDS:
                stop_event.set()
                if anim_thread:
                    anim_thread.join(timeout=2)
                edit_ui_message(
                    ui_message,
                    f"⏳ <b>Cooldown Active</b>\n\nPlease wait <code>{RATE_LIMIT_SECONDS - elapsed}</code> seconds and try again.",
                    kb_main_menu(),
                )
                return

            if uinfo["daily_count"] >= DAILY_LIMIT:
                stop_event.set()
                if anim_thread:
                    anim_thread.join(timeout=2)
                edit_ui_message(
                    ui_message,
                    f"🚫 <b>Daily Limit Reached</b>\n\nYou have used all <code>{DAILY_LIMIT}</code> SMS for today.",
                    kb_main_menu(),
                )
                return

        if uinfo["balance"] < SMS_COST:
            stop_event.set()
            if anim_thread:
                anim_thread.join(timeout=2)
            edit_ui_message(
                ui_message,
                f"❌ <b>Insufficient Coins</b>\n\nCurrent balance: <code>{uinfo['balance']}</code> coins.",
                kb_main_menu(),
            )
            return

        sent_ok, gateway_info = send_sms_via_ocs_api(number, msg_text)
        stop_event.set()
        if anim_thread:
            anim_thread.join(timeout=2)

        with db_lock:
            con = connect()
            cur = con.cursor()
            if sent_ok:
                new_daily = uinfo["daily_count"] + 1
                cur.execute(
                    """
                    UPDATE users
                    SET balance = balance - ?, last_sms_time = ?, daily_count = ?, daily_date = ?
                    WHERE uid=? AND balance >= ?
                    """,
                    (SMS_COST, now_ts, new_daily, today, uid, SMS_COST),
                )
                cur.execute(
                    "INSERT INTO logs(uid, number, message, status, cost, created_at) VALUES(?, ?, ?, 'SENT', ?, ?)",
                    (uid, number, msg_text, SMS_COST, now_str),
                )
                log_id = cur.lastrowid
            else:
                cur.execute(
                    "INSERT INTO logs(uid, number, message, status, cost, created_at) VALUES(?, ?, ?, 'FAILED', 0, ?)",
                    (uid, number, msg_text, now_str),
                )
                log_id = cur.lastrowid
            con.commit()
            con.close()

        updated = get_user_info(uid)
        if sent_ok:
            done_txt = (
                "<b>✅ SMS DISPATCH COMPLETE</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>Log ID:</b> <code>#{log_id}</code>\n"
                f"📱 <b>To:</b> <code>{safe_text(number)}</code>\n"
                f"💸 <b>Cost:</b> <code>{SMS_COST}</code> coin\n"
                f"💰 <b>Balance:</b> <code>{updated['balance']}</code> coins\n"
                f"📝 <b>API:</b> <code>{safe_text(gateway_info)}</code>"
            )
            edit_ui_message(ui_message, done_txt, kb_main_menu())
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"📊 <b>SMS REPORT (SENT)</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🆔 <b>Log ID:</b> <code>#{log_id}</code>\n"
                    f"🙋 <b>User:</b> {safe_text(updated['full_name'])} (<code>{uid}</code>)\n"
                    f"📱 <b>Number:</b> <code>{safe_text(number)}</code>\n"
                    f"💬 <b>SMS:</b> {safe_text(msg_text)}\n"
                    f"💰 <b>Balance:</b> <code>{updated['balance']}</code>\n"
                    f"📝 <b>Info:</b> <code>{safe_text(gateway_info)}</code>",
                )
            except Exception:
                pass
        else:
            fail_txt = (
                "❌ <b>SMS DISPATCH FAILED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>Log ID:</b> <code>#{log_id}</code>\n"
                f"📱 <b>To:</b> <code>{safe_text(number)}</code>\n"
                f"💰 <b>Balance:</b> <code>{updated['balance']}</code> coins (no charge)\n"
                f"📝 <b>Reason:</b> <code>{safe_text(gateway_info)}</code>"
            )
            edit_ui_message(ui_message, fail_txt, kb_main_menu())
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"📊 <b>SMS REPORT (FAILED)</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🆔 <b>Log ID:</b> <code>#{log_id}</code>\n"
                    f"🙋 <b>User:</b> {safe_text(updated['full_name'])} (<code>{uid}</code>)\n"
                    f"📱 <b>Number:</b> <code>{safe_text(number)}</code>\n"
                    f"💬 <b>SMS:</b> {safe_text(msg_text)}\n"
                    f"📝 <b>Info:</b> <code>{safe_text(gateway_info)}</code>",
                )
            except Exception:
                pass
    finally:
        stop_event.set()
        reset(chat_id)


def is_admin_user(message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


def run_broadcast(msg_text: str) -> tuple[int, int]:
    user_ids = get_all_active_user_ids()
    sent = 0
    failed = 0
    payload = f"<b>📣 Admin Broadcast</b>\n━━━━━━━━━━━━━━━━━━\n{safe_text(msg_text)}"
    for target_uid in user_ids:
        try:
            bot.send_message(target_uid, payload)
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin_user(message):
        return
    sync_user_profile(message.from_user)
    sessions[message.chat.id] = {"user_id": message.from_user.id, "awaiting_admin_pass": True}
    msg = bot.send_message(message.chat.id, "🔐 <b>Admin Login</b>\n\nEnter admin password:", reply_markup=kb_cancel())
    bot.register_next_step_handler(msg, step_admin_password)


def step_admin_password(message):
    chat_id = message.chat.id
    if not is_admin_user(message):
        return
    pwd = (message.text or "").strip()
    reset(chat_id)
    if pwd != ADMIN_PASSWORD:
        bot.send_message(chat_id, "❌ <b>Wrong password</b>", reply_markup=kb_main_menu())
        return
    sessions[chat_id] = {"user_id": message.from_user.id, "admin_authed": True}
    bot.send_message(
        chat_id,
        "👑 <b>Admin Panel Unlocked</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/users — Total &amp; banned users\n"
        "/check ID — User details\n"
        "/add ID AMOUNT — Add coins\n"
        "/ban ID REASON — Ban user\n"
        "/unban ID — Unban user\n"
        "/logs — Recent SMS logs\n"
        "/broadcast TEXT — Broadcast to users\n"
        "/setnotice TEXT — Set notice\n"
        "/clearnotice — Clear notice\n"
        "/setapikey KEY — Set OCS API Key\n"
        "/setendpoint URL — Set OCS Endpoint\n"
        "/setcaller SENDER_ID — Set Sender ID\n"
        "/logout — Clear admin session",
        reply_markup=kb_main_menu(),
    )


@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not is_admin_user(message):
        return
    with db_lock:
        con = connect()
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
        con.close()
    bot.send_message(message.chat.id, f"👥 <b>Total Users:</b> <code>{total}</code>\n🚫 <b>Banned:</b> <code>{banned}</code>")


@bot.message_handler(commands=["check"])
def cmd_check(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        bot.send_message(message.chat.id, "Usage: <code>/check USER_ID</code>")
        return
    uid = int(parts[1].strip())
    info = get_user_info(uid)
    status = f"BANNED 🚫 ({safe_text(info['ban_reason'])})" if info["banned"] == 1 else "ACTIVE ✅"
    bot.send_message(
        message.chat.id,
        f"🙋 <b>Name:</b> {safe_text(info['full_name'])}\n"
        f"🔗 <b>Username:</b> {safe_text(info['username'])}\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"💰 <b>Balance:</b> <code>{info['balance']}</code> coins\n"
        f"👥 <b>Referrals:</b> <code>{info['referrals']}</code>\n"
        f"📌 <b>Status:</b> {status}",
    )


@bot.message_handler(commands=["add"])
def cmd_add(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.send_message(message.chat.id, "Usage: <code>/add USER_ID AMOUNT</code>")
        return
    uid, amount = int(parts[1]), int(parts[2])
    if amount <= 0:
        bot.send_message(message.chat.id, "❌ Amount must be positive.")
        return
    with db_lock:
        con = connect()
        cur = con.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE uid=?", (amount, uid))
        con.commit()
        updated = cur.rowcount
        con.close()
    bot.send_message(message.chat.id, f"✅ Added <code>{amount}</code> coins to <code>{uid}</code>." if updated else "❌ User not found.")


@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Usage: <code>/ban USER_ID REASON</code>")
        return
    uid = int(parts[1])
    if uid == ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Cannot ban admin.")
        return
    reason = parts[2].strip() if len(parts) == 3 and parts[2].strip() else "নিয়ম ভঙ্গ করার কারণে ব্যান করা হয়েছে।"
    today = datetime.now().strftime("%Y-%m-%d")
    with db_lock:
        con = connect()
        cur = con.cursor()
        exists = cur.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone()
        if exists:
            cur.execute("UPDATE users SET banned=1, ban_reason=? WHERE uid=?", (reason, uid))
        else:
            cur.execute(
                """
                INSERT INTO users(uid, full_name, username, balance, referrals, banned, ban_reason, last_sms_time, daily_count, daily_date)
                VALUES(?, 'Unknown', 'No Username', 0, 0, 1, ?, 0, 0, ?)
                """,
                (uid, reason, today),
            )
        con.commit()
        con.close()
    bot.send_message(message.chat.id, f"🚫 User <code>{uid}</code> banned.\n📝 <b>Reason:</b> {safe_text(reason)}")
    try:
        bot.send_message(uid, f"🚫 <b>You are banned!</b>\n📝 <b>Reason:</b> {safe_text(reason)}")
    except Exception:
        pass


@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        bot.send_message(message.chat.id, "Usage: <code>/unban USER_ID</code>")
        return
    uid = int(parts[1].strip())
    with db_lock:
        con = connect()
        cur = con.cursor()
        cur.execute("UPDATE users SET banned=0, ban_reason='' WHERE uid=?", (uid,))
        con.commit()
        updated = cur.rowcount
        con.close()
    bot.send_message(message.chat.id, f"✅ User <code>{uid}</code> unbanned." if updated else "❌ User not found.")


@bot.message_handler(commands=["logs"])
def cmd_logs(message):
    if not is_admin_user(message):
        return
    with db_lock:
        con = connect()
        rows = con.execute("SELECT id, uid, number, status, created_at FROM logs ORDER BY id DESC LIMIT 10").fetchall()
        con.close()
    if not rows:
        bot.send_message(message.chat.id, "📜 <b>SMS Logs</b>\n\nNo logs yet.")
        return
    lines = ["<b>📜 Last 10 SMS Logs</b>\n━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(f"<code>#{r[0]}</code> | <code>{safe_text(r[2])}</code> | <b>{safe_text(r[3])}</b> | UID:<code>{r[1]}</code> | <code>{safe_text(r[4] or '-')}</code>")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: <code>/broadcast MESSAGE</code>")
        return
    sent, failed = run_broadcast(parts[1].strip())
    bot.send_message(message.chat.id, f"📣 <b>Broadcast Done</b>\n✅ Sent: <code>{sent}</code> | ⚠️ Failed: <code>{failed}</code>")


@bot.message_handler(commands=["setnotice"])
def cmd_setnotice(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: <code>/setnotice NOTICE_TEXT</code>")
        return
    set_setting("notice", parts[1].strip())
    bot.send_message(message.chat.id, "✅ Notice updated.")


@bot.message_handler(commands=["clearnotice"])
def cmd_clearnotice(message):
    if not is_admin_user(message):
        return
    set_setting("notice", "")
    bot.send_message(message.chat.id, "🗑️ Notice cleared.")


@bot.message_handler(commands=["setapikey"])
def cmd_setapikey(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: <code>/setapikey YOUR_BEARER_KEY</code>")
        return
    set_setting("api_key", parts[1].strip())
    bot.send_message(message.chat.id, "✅ OCS API Key updated.")


@bot.message_handler(commands=["setendpoint"])
def cmd_setendpoint(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: <code>/setendpoint URL</code>")
        return
    set_setting("endpoint", parts[1].strip())
    bot.send_message(message.chat.id, "✅ OCS Endpoint updated.")


@bot.message_handler(commands=["setcaller"])
def cmd_setcaller(message):
    if not is_admin_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: <code>/setcaller SENDER_ID</code>")
        return
    set_setting("sender_id", parts[1].strip())
    bot.send_message(message.chat.id, "✅ Sender ID updated.")


@bot.message_handler(commands=["logout"])
def cmd_logout(message):
    if not is_admin_user(message):
        return
    reset(message.chat.id)
    bot.send_message(message.chat.id, "🔓 Admin session cleared.")


@bot.message_handler(func=lambda m: True, content_types=["text", "document", "photo"])
def fallback(message):
    if not message.from_user or block_if_banned_chat(message.chat.id, message.from_user.id):
        return
    bot.send_message(message.chat.id, "Choose an option below, or use /cancel to reset:", reply_markup=kb_main_menu())


if __name__ == "__main__":
    setup_db()
    ensure_banner()
    logger.info("Custom SMS Studio starting…")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
