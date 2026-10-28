"""📞 ارتباط با سازنده | 🧑‍🏫 ارتباط با ادمین ترم (گفت‌وگوی رله‌ای از طریق ربات).

- هر دانشجو برای هر مقصد (سازنده / ادمین ترمِ خودش) یک «گفت‌وگو» (thread) دارد؛ پاسخ‌ها با همین thread
  به کاربر درست می‌رسند و گفت‌وگوی کاربرها با هم قاطی نمی‌شود.
- هر نوع پیام (متن، عکس، فایل، ویس…) با copy_message رله می‌شود.
- هویت: به سازنده/ادمین فقط لقب (اگر انتخاب شده) + کدِ گفت‌وگو نشان داده می‌شود؛ آیدی/نام کاربری/شماره نه.
  به دانشجو هم هویتِ پاسخ‌دهنده نشان داده نمی‌شود.
- مقصدِ «ادمین ترم» فقط TERM_ADMINهای ترمِ خودِ دانشجو (از پروفایلش) است؛ ترم دیگر قابل انتخاب نیست.
- پاسخ: دکمه‌ی «✍️ پاسخ» یا reply‌کردنِ روی خودِ پیامِ دریافتی.
- جدول‌های جدید و مستقل؛ هیچ پیامی حذف نمی‌شود.
"""
import logging
import secrets
import time
import datetime as _dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, ContextTypes,
                          MessageHandler, filters)

import db as dbmod

logger = logging.getLogger(__name__)

KIND_CREATOR, KIND_ADMIN = "creator", "term_admin"
KIND_LABEL = {KIND_CREATOR: "سازنده‌ی ربات", KIND_ADMIN: "ادمین ترم"}
MAX_TEXT = 3000
RATE_LIMIT, RATE_WINDOW = 10, 60  # حداکثر ۱۰ پیام در دقیقه برای هر دانشجو
STATE_KEYS = ("ct_mode", "ct_reply")

_deps: dict = {}
_times: dict = {}


# ============================================================
# دیتابیس
# ============================================================

def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def ensure_tables() -> None:
    with dbmod.get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contact_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                term_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, kind, term_name)
            );
            CREATE TABLE IF NOT EXISTS contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL,
                direction TEXT NOT NULL,            -- 'in' از دانشجو | 'out' از سازنده/ادمین
                staff_id INTEGER,
                content_type TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                delivered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_contact_messages_thread ON contact_messages(thread_id, id);
            CREATE TABLE IF NOT EXISTS contact_relay (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            """
        )


def _row(sql: str, params=()):
    with dbmod.get_conn() as conn:
        r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_thread(thread_id: int):
    return _row("SELECT * FROM contact_threads WHERE id = ?", (thread_id,))


def get_or_create_thread(user_id: int, kind: str, term_name: str) -> dict:
    now = _now()
    with dbmod.get_conn() as conn:
        r = conn.execute("SELECT * FROM contact_threads WHERE user_id = ? AND kind = ? AND term_name = ?",
                         (user_id, kind, term_name)).fetchone()
        if r:
            if r["status"] != "open":
                conn.execute("UPDATE contact_threads SET status = 'open', updated_at = ? WHERE id = ?", (now, r["id"]))
            else:
                conn.execute("UPDATE contact_threads SET updated_at = ? WHERE id = ?", (now, r["id"]))
            return dict(conn.execute("SELECT * FROM contact_threads WHERE id = ?", (r["id"],)).fetchone())
        for _ in range(8):
            code = "T" + secrets.token_hex(3).upper()
            try:
                cur = conn.execute(
                    "INSERT INTO contact_threads (code, user_id, kind, term_name, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'open', ?, ?)", (code, user_id, kind, term_name, now, now))
                return dict(conn.execute("SELECT * FROM contact_threads WHERE id = ?", (cur.lastrowid,)).fetchone())
            except Exception as e:
                if "UNIQUE" not in str(e) or "code" not in str(e):
                    raise
    raise RuntimeError("thread code allocation failed")


def add_message(thread_id: int, direction: str, content_type: str, text: str, staff_id=None, delivered=False) -> int:
    with dbmod.get_conn() as conn:
        return conn.execute(
            "INSERT INTO contact_messages (thread_id, direction, staff_id, content_type, text, delivered, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, direction, staff_id, content_type, text[:4000], 1 if delivered else 0, _now())).lastrowid


def set_thread_status(thread_id: int, status: str) -> None:
    with dbmod.get_conn() as conn:
        conn.execute("UPDATE contact_threads SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), thread_id))


def remember_relay(chat_id: int, message_id: int, thread_id: int) -> None:
    with dbmod.get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO contact_relay (chat_id, message_id, thread_id) VALUES (?, ?, ?)",
                     (chat_id, message_id, thread_id))


def thread_for_relay(chat_id: int, message_id: int):
    r = _row("SELECT thread_id FROM contact_relay WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
    return get_thread(r["thread_id"]) if r else None


# ============================================================
# مقصدها و دسترسی
# ============================================================

def recipients_for(thread: dict) -> list:
    if thread["kind"] == KIND_CREATOR:
        return list(_deps["creator_ids"]())
    return list(_deps["term_admin_ids"](thread["term_name"]))


def is_staff_of(user_id: int, thread: dict) -> bool:
    return user_id in recipients_for(thread)


def _content(msg) -> tuple:
    """(نوع، متنِ قابل ذخیره)"""
    if msg.text:
        return "text", msg.text
    for attr, label in (("photo", "photo"), ("voice", "voice"), ("document", "document"), ("video", "video"),
                        ("audio", "audio"), ("sticker", "sticker"), ("video_note", "video_note"), ("animation", "animation")):
        if getattr(msg, attr, None):
            return label, (msg.caption or "")
    return "other", (getattr(msg, "caption", None) or "")


def clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in STATE_KEYS:
        context.user_data.pop(k, None)


def has_state(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return any(context.user_data.get(k) for k in STATE_KEYS)


def _rate_ok(user_id: int) -> bool:
    now = time.monotonic()
    ts = [t for t in _times.get(user_id, []) if now - t < RATE_WINDOW]
    ok = len(ts) < RATE_LIMIT
    if ok:
        ts.append(now)
    _times[user_id] = ts
    return ok


def _leave_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚪 خروج از گفت‌وگو", callback_data="ct_leave")]])


# ============================================================
# ورود از منو
# ============================================================

async def show_root(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    user_id = update.effective_user.id
    clear_state(context)
    if kind == KIND_CREATOR:
        if not _deps["creator_ids"]():
            await update.message.reply_text("فعلاً امکانِ ارتباط با سازنده تنظیم نشده است. 🙏")
            return
        term = ""
        intro = "📞 پیامت را برای سازنده‌ی ربات بنویس (متن، عکس، فایل یا ویس)."
    else:
        term = _deps["term_of"](user_id) or ""
        if not term:
            await update.message.reply_text("اول ترمت را مشخص کن (از «📅 برنامه شخصی» ← تغییر ترم) تا پیام به ادمینِ ترمِ خودت برسد.")
            return
        if not _deps["term_admin_ids"](term):
            await update.message.reply_text(
                f"برای «{term}» هنوز ادمینی ثبت نشده است. می‌توانی از «📞 ارتباط با سازنده» استفاده کنی.")
            return
        intro = f"🧑‍🏫 پیامت را برای ادمین «{term}» بنویس (متن، عکس، فایل یا ویس)."
    context.user_data["ct_mode"] = {"kind": kind, "term": term}
    await update.message.reply_text(
        intro + "\n\nنام کاربری و شماره‌ات نمایش داده نمی‌شود؛ فقط لقبت (در صورت انتخاب). هر چیزی که الان بفرستی برای آن‌ها می‌رود و جواب را همین‌جا می‌گیری.\n"
        "برای رفتن به بخش‌های دیگر، یکی از دکمه‌های منوی اصلی را بزن.",
        reply_markup=_leave_markup())


async def cb_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_state(context)
    await update.callback_query.answer("از گفت‌وگو خارج شدی.")
    try:
        await update.callback_query.edit_message_text("از گفت‌وگو خارج شدی. هر وقت خواستی دوباره از منو واردش شو.")
    except TelegramError:
        pass


async def cb_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cto:<thread> -- دکمه‌ی «ادامه‌ی گفت‌وگو» زیرِ پاسخ؛ فقط صاحبِ همان thread."""
    query = update.callback_query
    t = get_thread(int(query.data.split(":", 1)[1]))
    if not t or t["user_id"] != update.effective_user.id:
        await query.answer("پیدا نشد.", show_alert=True)
        return
    await query.answer()
    clear_state(context)
    context.user_data["ct_mode"] = {"kind": t["kind"], "term": t["term_name"]}
    await query.message.reply_text("هر پیامی بفرستی برای آن‌ها می‌رود.", reply_markup=_leave_markup())


# ============================================================
# ارسال از دانشجو → سازنده/ادمین
# ============================================================

def _label(uid: int) -> str:
    return _deps["nickname"](uid) or "دانشجو"


async def _send_with_copy(bot, chat_id: int, header: str, markup, msg, with_text: bool) -> list:
    """هدر (و برای غیرمتن، خودِ پیام) را می‌فرستد؛ شناسه‌ی پیام‌های ساخته‌شده را برمی‌گرداند."""
    ids = []
    sent = await bot.send_message(chat_id=chat_id, text=header, reply_markup=markup)
    ids.append(sent.message_id)
    if not with_text:
        cp = await bot.copy_message(chat_id=chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
        ids.append(cp.message_id)
    return ids


async def relay_from_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id
    mode = context.user_data.get("ct_mode")
    ctype, text = _content(msg)
    if ctype == "text" and (not text.strip() or len(text) > MAX_TEXT):
        await msg.reply_text(f"پیام باید خالی نباشد و حداکثر {MAX_TEXT} نویسه باشد.")
        return
    if not _rate_ok(user_id):
        await msg.reply_text("⏳ خیلی سریع پیام می‌فرستی؛ کمی صبر کن.")
        return
    # مقصدِ ادمین‌ترم همیشه از روی ترمِ فعلیِ خودِ کاربر دوباره چک می‌شود (نه فقط حالتِ ذخیره‌شده)
    term = mode["term"]
    if mode["kind"] == KIND_ADMIN and (_deps["term_of"](user_id) or "") != term:
        clear_state(context)
        await msg.reply_text("ترمت تغییر کرده؛ دوباره از منو وارد شو.")
        return
    thread = get_or_create_thread(user_id, mode["kind"], term)
    recips = recipients_for(thread)
    mid = add_message(thread["id"], "in", ctype, text)
    header = f"📨 پیام جدید — {KIND_LABEL[mode['kind']]}\nکد گفت‌وگو: {thread['code']}\nاز: {_label(user_id)}"
    if mode["kind"] == KIND_CREATOR and _deps["term_of"](user_id):
        header += f"\nترم: {_deps['term_of'](user_id)}"
    if ctype == "text":
        header += f"\n\n{text}"
    elif text:
        header += f"\n\n(کپشن) {text}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("✍️ پاسخ", callback_data=f"ctr:{thread['id']}"),
                                    InlineKeyboardButton("✅ بستن", callback_data=f"ctc:{thread['id']}")]])
    delivered = 0
    for rid in recips:
        try:
            for message_id in await _send_with_copy(context.bot, rid, header, markup, msg, ctype == "text"):
                remember_relay(rid, message_id, thread["id"])
            delivered += 1
        except (Forbidden, TelegramError):
            continue
    if delivered:
        with dbmod.get_conn() as conn:
            conn.execute("UPDATE contact_messages SET delivered = 1 WHERE id = ?", (mid,))
        await msg.reply_text("✅ پیامت ارسال شد؛ جواب را همین‌جا می‌گیری.")
    else:
        await msg.reply_text("⚠️ الان امکانِ رساندنِ پیام نبود (گیرنده در دسترس نیست). پیامت ذخیره شد؛ کمی بعد دوباره امتحان کن.")


# ============================================================
# پاسخ از سازنده/ادمین → دانشجو
# ============================================================

async def cb_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ctr:<thread> -- شروعِ پاسخ (پیامِ بعدیِ همین ادمین به دانشجو می‌رسد)."""
    query = update.callback_query
    t = get_thread(int(query.data.split(":", 1)[1]))
    if not t or not is_staff_of(update.effective_user.id, t):
        await query.answer("⛔️ مجاز نیستید.", show_alert=True)
        return
    await query.answer()
    context.user_data["ct_reply"] = t["id"]
    await query.message.reply_text(f"✍️ پاسخ به «{t['code']}» را بفرست (هر نوع پیامی). برای لغو، یکی از دکمه‌های منوی اصلی را بزن.")


async def cb_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    t = get_thread(int(query.data.split(":", 1)[1]))
    if not t or not is_staff_of(update.effective_user.id, t):
        await query.answer("⛔️ مجاز نیستید.", show_alert=True)
        return
    set_thread_status(t["id"], "closed")
    await query.answer("بسته شد ✅")
    try:
        await context.bot.send_message(chat_id=t["user_id"], text=f"✅ گفت‌وگوی «{t['code']}» با {KIND_LABEL[t['kind']]} بسته شد. اگر پیام تازه‌ای بفرستی دوباره باز می‌شود.")
    except (Forbidden, TelegramError):
        pass
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


async def relay_from_staff(update: Update, context: ContextTypes.DEFAULT_TYPE, thread: dict) -> None:
    msg = update.message
    staff_id = update.effective_user.id
    if not is_staff_of(staff_id, thread):
        await msg.reply_text("⛔️ مجاز نیستید.")
        return
    ctype, text = _content(msg)
    if ctype == "text" and (not text.strip() or len(text) > MAX_TEXT):
        await msg.reply_text(f"پاسخ باید خالی نباشد و حداکثر {MAX_TEXT} نویسه باشد.")
        return
    mid = add_message(thread["id"], "out", ctype, text, staff_id=staff_id)
    header = f"📩 پاسخ از طرف {KIND_LABEL[thread['kind']]} ({thread['code']})"
    if ctype == "text":
        header += f"\n\n{text}"
    elif text:
        header += f"\n\n{text}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("💬 ادامه‌ی گفت‌وگو", callback_data=f"cto:{thread['id']}")]])
    try:
        await _send_with_copy(context.bot, thread["user_id"], header, markup, msg, ctype == "text")
    except Forbidden:
        await msg.reply_text("⚠️ دانشجو ربات را بلاک کرده؛ پاسخ ارسال نشد.")
        return
    except TelegramError:
        await msg.reply_text("⚠️ ارسالِ پاسخ ناموفق بود؛ دوباره امتحان کن.")
        return
    with dbmod.get_conn() as conn:
        conn.execute("UPDATE contact_messages SET delivered = 1 WHERE id = ?", (mid,))
    set_thread_status(thread["id"], "open")
    await msg.reply_text("✅ پاسخ ارسال شد.")
    for other in recipients_for(thread):
        if other != staff_id:
            try:
                await context.bot.send_message(chat_id=other, text=f"↩️ به گفت‌وگوی «{thread['code']}» پاسخ داده شد.")
            except (Forbidden, TelegramError):
                pass


# ============================================================
# ورودی‌های پیام
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """از receive_text صدا زده می‌شود؛ True یعنی پیام مصرف شد."""
    ud = context.user_data
    if ud.get("ct_reply"):
        tid = ud.pop("ct_reply")
        t = get_thread(tid)
        if t:
            await relay_from_staff(update, context, t)
        return True
    if ud.get("ct_mode"):
        await relay_from_user(update, context)
        return True
    return False


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """غیرمتن (عکس/فایل/ویس…) فقط وقتی کاربر در حالتِ گفت‌وگو/پاسخ است؛ وگرنه هندلرهای قبلیِ ربات دست‌نخورده‌اند."""
    if not update.message or not has_state(context):
        return
    if await handle_text(update, context):
        raise ApplicationHandlerStop


async def reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """reply روی یکی از پیام‌های دریافتیِ ادمین/سازنده ← پاسخ به همان گفت‌وگو."""
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    t = thread_for_relay(update.effective_chat.id, msg.reply_to_message.message_id)
    if not t or not is_staff_of(update.effective_user.id, t):
        return
    await relay_from_staff(update, context, t)
    raise ApplicationHandlerStop


def register(app: Application, deps: dict) -> None:
    """deps: nickname(uid)->str|None، term_of(uid)->str|None، term_admin_ids(term)->list، creator_ids()->list."""
    _deps.update(deps)
    ensure_tables()
    app.add_handler(CallbackQueryHandler(cb_leave, pattern=r"^ct_leave$"))
    app.add_handler(CallbackQueryHandler(cb_open, pattern=r"^cto:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reply, pattern=r"^ctr:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_close, pattern=r"^ctc:\d+$"))
    media = (filters.PHOTO | filters.VOICE | filters.Document.ALL | filters.VIDEO | filters.AUDIO
             | filters.Sticker.ALL | filters.VIDEO_NOTE | filters.ANIMATION)
    # گروه‌های جدا: در هر گروه فقط یک هندلر اجرا می‌شود (market از group=-3 استفاده می‌کند)
    app.add_handler(MessageHandler(media, media_handler), group=-4)
    app.add_handler(MessageHandler(filters.REPLY, reply_handler), group=-5)
