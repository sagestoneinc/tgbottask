import logging
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/New_York")  # Eastern Time (EST/EDT)
DEFAULT_REMINDER_HOURS = int(os.getenv("DEFAULT_REMINDER_HOURS", "2"))

DB_PATH = "tgbot.sqlite3"

# ------- Customize these -------
DEFAULT_CHECKLIST_TITLE = "Ecom Ops Daily Task"
DEFAULT_CHECKLIST_ITEMS = [
    "🛡️ Hourly MID Ops – Nutra and XShield",
    "🍃 Manual Batch Reruns – before 9 EST",
    "🔁 Manual Batch Rebills",
    "✅ Mail Gun Health Check",
    "🔗 Malicious Link Url Check",
    "🧾 Duplicate Refund Check",
]

QUICK_LINKS = [
    ("📌 Notion", "https://www.notion.so/"),
    ("🎫 Jira", "https://www.atlassian.com/software/jira"),
    ("📁 Drive", "https://drive.google.com/"),
]
# -----------------------------


# In-memory pending confirmations: (chat_id, user_id) -> task_text
PENDING = {}


def normalize_webhook_path(raw_path: str, token: str) -> str:
    """
    Return a safe webhook path without leading/trailing slash.
    Defaults to a token-based path when no path is provided.
    """
    path = (raw_path or f"telegram/{token}").strip()
    return path.strip("/")


def derive_webhook_url(path: str) -> str:
    """
    Build webhook URL from explicit WEBHOOK_URL or Railway public domain.
    """
    explicit = os.getenv("WEBHOOK_URL", "").strip()
    if explicit:
        return explicit

    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if not domain:
        return ""

    if domain.startswith("http://") or domain.startswith("https://"):
        base = domain
    else:
        base = f"https://{domain}"
    return f"{base}/{path}"


def conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL DEFAULT 'America/New_York',
            reminder_hours INTEGER NOT NULL DEFAULT 2,
            quiet_start INTEGER NOT NULL DEFAULT 22,
            quiet_end INTEGER NOT NULL DEFAULT 8,
            snooze_until TEXT NULL,
            checklist_id INTEGER NULL,
            checklist_msg_id INTEGER NULL,
            checklist_msg_date TEXT NULL
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS checklists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            UNIQUE(chat_id)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS checklist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checklist_id INTEGER NOT NULL,
            sort INTEGER NOT NULL,
            label TEXT NOT NULL
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS checklist_done (
            checklist_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            done_by INTEGER NULL,
            done_at TEXT NULL,
            PRIMARY KEY(checklist_id, item_id, day)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )
        """)


def get_or_create_chat_state(chat_id: int):
    with conn() as con:
        row = con.execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            con.execute(
                "INSERT INTO chat_state(chat_id, tz, reminder_hours) VALUES (?, ?, ?)",
                (chat_id, DEFAULT_TZ, DEFAULT_REMINDER_HOURS),
            )
            row = con.execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row)


def update_chat_state(chat_id: int, **kwargs):
    if not kwargs:
        return
    cols = ", ".join([f"{k}=?" for k in kwargs.keys()])
    vals = list(kwargs.values()) + [chat_id]
    with conn() as con:
        con.execute(f"UPDATE chat_state SET {cols} WHERE chat_id=?", vals)


def safe_tz(tz_name: str) -> str:
    """
    Validate timezone name. Returns a safe IANA tz string.
    """
    try:
        ZoneInfo(tz_name)
        return tz_name
    except Exception:
        return "America/New_York"


def local_day(chat_state) -> str:
    tz = ZoneInfo(chat_state["tz"])
    return datetime.now(tz).date().isoformat()


def in_quiet_hours(chat_state) -> bool:
    tz = ZoneInfo(chat_state["tz"])
    hour = datetime.now(tz).hour
    qs, qe = int(chat_state["quiet_start"]), int(chat_state["quiet_end"])
    if qs > qe:
        return hour >= qs or hour < qe
    return qs <= hour < qe


def snoozed(chat_state) -> bool:
    su = chat_state.get("snooze_until")
    if not su:
        return False
    tz = ZoneInfo(chat_state["tz"])
    try:
        until = datetime.fromisoformat(su).astimezone(tz)
    except Exception:
        return False
    return datetime.now(tz) < until


def ensure_default_checklist(chat_id: int) -> int:
    with conn() as con:
        row = con.execute("SELECT id FROM checklists WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            return int(row["id"])

        cur = con.execute(
            "INSERT INTO checklists(chat_id, title) VALUES (?, ?)",
            (chat_id, DEFAULT_CHECKLIST_TITLE),
        )
        checklist_id = int(cur.lastrowid)

        for i, label in enumerate(DEFAULT_CHECKLIST_ITEMS, start=1):
            con.execute(
                "INSERT INTO checklist_items(checklist_id, sort, label) VALUES (?, ?, ?)",
                (checklist_id, i, label),
            )
        return checklist_id


def get_checklist_title(checklist_id: int) -> str:
    with conn() as con:
        return con.execute("SELECT title FROM checklists WHERE id=?", (checklist_id,)).fetchone()["title"]


def get_checklist_items(checklist_id: int):
    with conn() as con:
        rows = con.execute(
            "SELECT id, sort, label FROM checklist_items WHERE checklist_id=? ORDER BY sort ASC",
            (checklist_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_done_map(checklist_id: int, day: str) -> dict[int, bool]:
    with conn() as con:
        rows = con.execute(
            "SELECT item_id, done FROM checklist_done WHERE checklist_id=? AND day=?",
            (checklist_id, day),
        ).fetchall()
    return {int(r["item_id"]): bool(r["done"]) for r in rows}


def toggle_done(checklist_id: int, item_id: int, day: str, user_id: int) -> bool:
    now = datetime.utcnow().isoformat()
    with conn() as con:
        row = con.execute(
            "SELECT done FROM checklist_done WHERE checklist_id=? AND item_id=? AND day=?",
            (checklist_id, item_id, day),
        ).fetchone()

        if not row:
            con.execute(
                "INSERT INTO checklist_done(checklist_id, item_id, day, done, done_by, done_at) VALUES (?, ?, ?, 1, ?, ?)",
                (checklist_id, item_id, day, user_id, now),
            )
            return True

        new_done = 0 if int(row["done"]) == 1 else 1
        con.execute(
            "UPDATE checklist_done SET done=?, done_by=?, done_at=? WHERE checklist_id=? AND item_id=? AND day=?",
            (new_done, user_id, now, checklist_id, item_id, day),
        )
        return bool(new_done)


def build_checklist_text(title: str, items: list[dict], done_map: dict[int, bool]) -> str:
    total = len(items)
    done_count = sum(1 for it in items if done_map.get(it["id"], False))

    lines = [f"📝 {title}", "Group Checklist"]
    for it in items:
        is_done = done_map.get(it["id"], False)
        prefix = "✅" if is_done else "◯"
        lines.append(f"{prefix}  {it['label']}")
    lines.append("")
    lines.append(f"{done_count} of {total} completed")
    return "\n".join(lines)


def short_label(text: str, max_len: int = 44) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def checklist_keyboard(items: list[dict] | None = None, done_map: dict[int, bool] | None = None) -> InlineKeyboardMarkup:
    done_map = done_map or {}
    rows = []

    # Telegram-like checklist controls: one tap to toggle each item.
    if items:
        for idx, it in enumerate(items, start=1):
            is_done = done_map.get(it["id"], False)
            prefix = "✅" if is_done else "☐"
            label = short_label(it["label"])
            rows.append([InlineKeyboardButton(f"{prefix} {idx}. {label}", callback_data=f"ck_t:{it['id']}")])

    # Utility actions
    rows.extend([
        [InlineKeyboardButton("➕ Add", callback_data="task_add"),
         InlineKeyboardButton("🔗 Links", callback_data="links")],
        [InlineKeyboardButton("⏰ Snooze 1h", callback_data="ck_snooze1")],
    ])

    return InlineKeyboardMarkup(rows)


def picker_keyboard(items: list[dict], done_map: dict[int, bool]) -> InlineKeyboardMarkup:
    # 2 columns numeric picker, minimal footprint
    buttons = []
    row = []
    for idx, it in enumerate(items, start=1):
        status = "✅" if done_map.get(it["id"], False) else "◯"
        row.append(InlineKeyboardButton(f"{idx} {status}", callback_data=f"ck_t:{it['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="ck_back")])
    return InlineKeyboardMarkup(buttons)


def links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(name, url=url)] for name, url in QUICK_LINKS])


async def ensure_reminder_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int, hours: int):
    name = f"reminder:{chat_id}"
    for j in context.job_queue.get_jobs_by_name(name):
        j.schedule_removal()

    context.job_queue.run_repeating(
        reminder_tick,
        interval=hours * 3600,
        first=10,
        name=name,
        data={"chat_id": chat_id},
    )


async def post_or_update_checklist(context: ContextTypes.DEFAULT_TYPE, chat_id: int, force_new: bool = False):
    st = get_or_create_chat_state(chat_id)
    if in_quiet_hours(st) or snoozed(st):
        return

    checklist_id = st.get("checklist_id") or ensure_default_checklist(chat_id)
    day = local_day(st)

    title = get_checklist_title(checklist_id)
    items = get_checklist_items(checklist_id)
    done_map = get_done_map(checklist_id, day)
    text = build_checklist_text(title, items, done_map)

    msg_id = st.get("checklist_msg_id")
    msg_day = st.get("checklist_msg_date")

    if (not force_new) and msg_id and msg_day == day:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(msg_id),
                text=text,
                reply_markup=checklist_keyboard(items, done_map),
            )
            return
        except Exception:
            pass

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=checklist_keyboard(items, done_map),
    )
    update_chat_state(chat_id, checklist_msg_id=sent.message_id, checklist_msg_date=day)


# ---------------- Commands ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    chat_id = update.effective_chat.id

    st = get_or_create_chat_state(chat_id)
    checklist_id = ensure_default_checklist(chat_id)
    update_chat_state(chat_id, checklist_id=checklist_id)

    await ensure_reminder_job(context, chat_id, int(st["reminder_hours"]))

    await update.message.reply_text(
        "✅ TGBOT is live.\n\n"
        "Commands:\n"
        "/checklist — post/refresh today’s checklist\n"
        "/settings — set reminders (1h or 2h)\n"
        "/links — quick links\n"
        "/tasks — list ad-hoc tasks\n"
        "/taskdone <id> — complete ad-hoc task\n"
        "/handoff — shift summary\n"
        "/tz <IANA_TZ> — set timezone (default: America/New_York)\n\n"
        "In groups, ad-hoc task capture requires prefix: `task:` / `t:` / `+ `",
        reply_markup=checklist_keyboard(),
    )


async def checklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await post_or_update_checklist(context, update.effective_chat.id, force_new=True)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = get_or_create_chat_state(chat_id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱️ Every 1 hour", callback_data="set_h:1"),
         InlineKeyboardButton("⏱️ Every 2 hours", callback_data="set_h:2")],
    ])
    await update.message.reply_text(
        f"⚙️ Reminder cadence is currently every {st['reminder_hours']} hour(s).",
        reply_markup=kb,
    )


async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔗 Quick links", reply_markup=links_keyboard())


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with conn() as con:
        rows = con.execute(
            "SELECT id, text FROM tasks WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 25",
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("📌 No open ad-hoc tasks.")
        return

    lines = ["📌 Open ad-hoc tasks:"]
    for r in rows:
        lines.append(f"• #{r['id']} {r['text']}")
    lines.append("\nUse /taskdone <id> to complete.")
    await update.message.reply_text("\n".join(lines))


async def taskdone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /taskdone <id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Task id must be a number.")
        return

    with conn() as con:
        cur = con.execute(
            "UPDATE tasks SET status='done' WHERE chat_id=? AND id=? AND status='open'",
            (chat_id, task_id),
        )
    await update.message.reply_text("✅ Task completed." if cur.rowcount else "Couldn’t find that open task.")


async def handoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    chat_id = update.effective_chat.id
    st = get_or_create_chat_state(chat_id)
    checklist_id = st.get("checklist_id") or ensure_default_checklist(chat_id)
    day = local_day(st)

    title = get_checklist_title(checklist_id)
    items = get_checklist_items(checklist_id)
    done_map = get_done_map(checklist_id, day)

    done_items = [it["label"] for it in items if done_map.get(it["id"], False)]
    open_items = [it["label"] for it in items if not done_map.get(it["id"], False)]

    with conn() as con:
        open_tasks = con.execute(
            "SELECT id, text FROM tasks WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 10",
            (chat_id,),
        ).fetchall()

    lines = [f"🧾 Handoff — {title} ({day})", ""]
    lines.append("✅ Completed checklist items:")
    lines += [f"• {t}" for t in (done_items or ["(none)"])]

    lines.append("")
    lines.append("⏳ Remaining checklist items:")
    lines += [f"• {t}" for t in (open_items or ["(none)"])]

    lines.append("")
    lines.append("📌 Open ad-hoc tasks:")
    if open_tasks:
        for r in open_tasks:
            lines.append(f"• #{r['id']} {r['text']}")
    else:
        lines.append("• (none)")

    await update.message.reply_text("\n".join(lines))


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        st = get_or_create_chat_state(chat_id)
        await update.message.reply_text(f"Timezone is currently: {st['tz']}\nExample: /tz America/New_York")
        return
    tz_name = " ".join(context.args).strip()
    tz_name = safe_tz(tz_name)
    update_chat_state(chat_id, tz=tz_name)
    await update.message.reply_text(f"✅ Timezone set to: {tz_name}")


# -------------- Task collector --------------

def should_capture_as_task(update: Update) -> bool:
    msg = update.effective_message
    if not msg or not msg.text:
        return False
    text = msg.text.strip()
    if text.startswith("/"):
        return False

    chat_type = update.effective_chat.type  # private, group, supergroup, channel
    if chat_type == "private":
        return True

    # In groups: require an explicit prefix to avoid collecting normal conversation.
    low = text.lower()
    return low.startswith("task:") or low.startswith("t:") or text.startswith("+ ")


async def capture_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not should_capture_as_task(update):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    raw = update.message.text.strip()
    # Strip common prefixes
    for p in ("task:", "t:"):
        if raw.lower().startswith(p):
            raw = raw[len(p):].strip()
    if raw.startswith("+ "):
        raw = raw[2:].strip()

    if not raw:
        return

    PENDING[(chat_id, user_id)] = raw
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Save", callback_data="task_save"),
         InlineKeyboardButton("🗑️ Discard", callback_data="task_discard")]
    ])
    await update.message.reply_text(f"Save this task?\n\n📝 {raw}", reply_markup=kb)


# -------------- Reminders --------------

async def reminder_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await post_or_update_checklist(context, chat_id, force_new=False)


# -------------- Callbacks --------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data

    chat_id = q.message.chat.id
    user_id = q.from_user.id
    st = get_or_create_chat_state(chat_id)

    # Settings
    if data.startswith("set_h:"):
        hours = int(data.split(":")[1])
        update_chat_state(chat_id, reminder_hours=hours)
        await ensure_reminder_job(context, chat_id, hours)
        await q.answer(f"Set to every {hours} hour(s).")
        return

    # Links (send separate message to avoid overwriting checklist)
    if data == "links":
        await q.answer()
        await context.bot.send_message(chat_id=chat_id, text="🔗 Quick links", reply_markup=links_keyboard())
        return

    # Snooze (toast only; don’t overwrite checklist)
    if data == "ck_snooze1":
        tz = ZoneInfo(st["tz"])
        until = datetime.now(tz) + timedelta(hours=1)
        update_chat_state(chat_id, snooze_until=until.isoformat())
        await q.answer("Snoozed 1 hour.")
        return

    # Open checklist picker (edit reply markup only)
    if data == "ck_open":
        checklist_id = st.get("checklist_id") or ensure_default_checklist(chat_id)
        day = local_day(st)
        items = get_checklist_items(checklist_id)
        done_map = get_done_map(checklist_id, day)

        # Remember which message is the main checklist message
        update_chat_state(chat_id, checklist_msg_id=q.message.message_id, checklist_msg_date=day)

        await q.answer()
        await q.edit_message_reply_markup(reply_markup=checklist_keyboard(items, done_map))
        return

    # Back to main keyboard (and refresh text)
    if data == "ck_back":
        checklist_id = st.get("checklist_id") or ensure_default_checklist(chat_id)
        day = local_day(st)

        title = get_checklist_title(checklist_id)
        items = get_checklist_items(checklist_id)
        done_map = get_done_map(checklist_id, day)
        text = build_checklist_text(title, items, done_map)

        await q.answer()
        await q.edit_message_text(text=text, reply_markup=checklist_keyboard(items, done_map))
        return

    # Toggle item and refresh checklist (stay in picker mode)
    if data.startswith("ck_t:"):
        item_id = int(data.split(":")[1])
        checklist_id = st.get("checklist_id") or ensure_default_checklist(chat_id)
        day = local_day(st)

        toggle_done(checklist_id, item_id, day, user_id)

        title = get_checklist_title(checklist_id)
        items = get_checklist_items(checklist_id)
        done_map = get_done_map(checklist_id, day)
        text = build_checklist_text(title, items, done_map)

        await q.answer()
        await q.edit_message_text(text=text, reply_markup=checklist_keyboard(items, done_map))
        return

    # Add task button
    if data == "task_add":
        await q.answer()
        await context.bot.send_message(chat_id=chat_id, text="Send a task like:\n`task: Follow up with vendor`\n`t: Check Mailgun`\n`+ Re-run batch`", parse_mode="Markdown")
        return

    # Task collector confirmations
    if data in ("task_save", "task_discard"):
        pending = PENDING.get((chat_id, user_id))
        if not pending:
            await q.answer("Nothing pending.")
            return

        if data == "task_discard":
            PENDING.pop((chat_id, user_id), None)
            await q.answer("Discarded.")
            return

        now = datetime.utcnow().isoformat()
        with conn() as con:
            con.execute(
                "INSERT INTO tasks(chat_id, text, created_at) VALUES (?, ?, ?)",
                (chat_id, pending, now),
            )
        PENDING.pop((chat_id, user_id), None)
        await q.answer("Saved.")
        return

    await q.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception while processing update: %s", context.error, exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN in .env")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("checklist", checklist_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("links", links_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("taskdone", taskdone_cmd))
    app.add_handler(CommandHandler("handoff", handoff_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capture_task))
    app.add_error_handler(on_error)

    mode = os.getenv("BOT_MODE", "").strip().lower()
    webhook_path = normalize_webhook_path(os.getenv("WEBHOOK_PATH", ""), BOT_TOKEN)
    webhook_url = derive_webhook_url(webhook_path)
    use_webhook = mode == "webhook" or (mode == "" and bool(webhook_url))

    if use_webhook:
        if not webhook_url:
            raise RuntimeError(
                "Webhook mode requires WEBHOOK_URL or RAILWAY_PUBLIC_DOMAIN."
            )

        port = int(os.getenv("PORT", "8080"))
        secret_token = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip() or None
        logger.info("Starting bot in webhook mode on port %s path=/%s", port, webhook_path)
        # IMPORTANT: do NOT await this, and do NOT wrap it in asyncio.run
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            secret_token=secret_token,
            drop_pending_updates=True,
        )
        return

    logger.info("Starting bot in polling mode")
    # IMPORTANT: do NOT await this, and do NOT wrap it in asyncio.run
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
