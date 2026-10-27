"""📚 بازارچه کتاب و جزوه (مرحله‌ی الف + ب: گزارش تخلف، بررسی توسط ناظر، عکسِ اختیاری).

- آگهی (فروش / واگذاری رایگان / امانت‌دادن) و درخواست (خرید / امانت‌گرفتن / دریافت رایگان)، با
  دسته‌بندی ترم ← درس ← منبع از ساختار واقعی ترم/درس‌های دیتابیس.
- گفت‌وگو فقط از طریق ربات (رله): هر گفت‌وگو به یک آگهی/درخواست و یک نفرِ پاسخ‌دهنده وصل است؛ پیام‌ها
  با شناسه‌ی گفت‌وگو جدا می‌شوند. هیچ نام/آیدی/شماره‌ای برای طرف مقابل نمایش داده نمی‌شود.
- قبل از شروعِ گفت‌وگو، پاسخ‌دهنده یک جمله‌ی «دلیل و قصد» می‌نویسد و صاحبِ آگهی می‌پذیرد/رد می‌کند.
  برای «درخواست»‌ها، درخواست‌کننده هم هنگام پذیرش یک جمله‌ی تأیید می‌نویسد.
- جدول‌های جدید و مستقل؛ هیچ آگهی/گفت‌وگو حذف نمی‌شود (فقط وضعیت عوض می‌شود).
- پرداخت در ربات نیست؛ تحویل و معامله با خودِ دانشجوهاست.
"""
import logging
import re
import secrets
import time
import datetime as _dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import db as dbmod

logger = logging.getLogger(__name__)

KIND_AD, KIND_REQ = "ad", "request"
AD_TYPES = {"sell": "💰 فروش", "free": "🎁 واگذاری رایگان", "lend": "🔄 امانت‌دادن"}
REQ_TYPES = {"buy": "💰 خرید", "borrow": "🔄 امانت‌گرفتن", "free": "🎁 دریافت رایگان"}
CONDITIONS = {
    "new": "نو", "like_new": "در حد نو", "used": "کارکرده (سالم)",
    "marked": "کارکرده با یادداشت/هایلایت", "handwritten": "جزوه‌ی دست‌نویس", "digital": "فایل دیجیتال",
}
STATUSES = ("available", "negotiating", "gone", "closed")
ACTIVE_STATUSES = ("available", "negotiating")
_ST_SHORT = {"a": "available", "n": "negotiating", "g": "gone", "c": "closed"}
_ST_SHORT_REV = {v: k for k, v in _ST_SHORT.items()}

MAX_ACTIVE_PER_KIND = 10
TITLE_MAX, TEACHER_MAX, DESC_MAX, INTENT_MAX, MSG_MAX = 80, 60, 500, 200, 1000
PRICE_MAX = 50_000_000
PAGE_SIZE = 8
MSG_RATE_LIMIT, MSG_RATE_WINDOW = 20, 60  # حداکثر ۲۰ پیام در دقیقه برای هر کاربر

_msg_times: dict = {}

ROOT_TEXT = (
    "📚 بازارچه کتاب و جزوه\n\n"
    "خرید، فروش، امانت و واگذاریِ رایگانِ منابع آموزشی بین دانشجوها.\n"
    "• گفت‌وگو فقط از طریق ربات انجام می‌شود و نام/شماره‌ات به طرف مقابل نمایش داده نمی‌شود.\n"
    "• تحویل منبع و معامله با خودِ شماست؛ ربات پرداخت انجام نمی‌دهد.\n"
    "• شماره یا اطلاعات شخصی‌ات را در آگهی ننویس."
)


# ============================================================
# دیتابیس
# ============================================================

def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def ensure_tables() -> None:
    with dbmod.get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                deal_type TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                term_name TEXT NOT NULL,
                course_id INTEGER NOT NULL,
                course_name TEXT NOT NULL,
                teacher TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                condition TEXT NOT NULL DEFAULT '',
                price INTEGER,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                photo_file_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_market_listings_browse ON market_listings(kind, course_id, status);
            CREATE INDEX IF NOT EXISTS idx_market_listings_owner ON market_listings(owner_id, kind);
            CREATE TABLE IF NOT EXISTS market_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                responder_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                intent TEXT NOT NULL,
                confirm_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(listing_id, responder_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_chats_users ON market_chats(owner_id, responder_id, status);
            CREATE TABLE IF NOT EXISTS market_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_market_messages_chat ON market_messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS market_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,            -- 'listing' | 'message'
                target_id INTEGER NOT NULL,           -- شناسه‌ی آگهی یا پیام
                listing_id INTEGER NOT NULL,
                chat_id INTEGER,
                reason TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',  -- open | dismissed | actioned
                reviewed_by INTEGER,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                UNIQUE(reporter_id, target_type, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_reports_status ON market_reports(status, id);
            """
        )
        # مهاجرتِ امن برای جدولِ مرحله‌ی الف (ستونِ عکس)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(market_listings)").fetchall()}
        if "photo_file_id" not in cols:
            conn.execute("ALTER TABLE market_listings ADD COLUMN photo_file_id TEXT")


def _rows(sql: str, params=()) -> list:
    with dbmod.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(sql: str, params=()):
    r = _rows(sql, params)
    return r[0] if r else None


def list_terms() -> list:
    return _rows(
        "SELECT t.id, t.name, g.name AS grp FROM terms t JOIN groups g ON g.id = t.group_id "
        "ORDER BY g.sort_order, g.id, t.sort_order, t.id"
    )


def list_courses_with_counts(term_id: int, kind: str = None) -> list:
    if kind:
        return _rows(
            "SELECT c.id, c.name, (SELECT COUNT(*) FROM market_listings m WHERE m.course_id = c.id "
            "AND m.kind = ? AND m.status IN ('available','negotiating')) AS n "
            "FROM courses c WHERE c.term_id = ? ORDER BY c.sort_order, c.id",
            (kind, term_id),
        )
    return _rows("SELECT id, name, 0 AS n FROM courses WHERE term_id = ? ORDER BY sort_order, id", (term_id,))


def get_listing(listing_id: int):
    return _row("SELECT * FROM market_listings WHERE id = ?", (listing_id,))


def count_active(owner_id: int, kind: str) -> int:
    return _row(
        "SELECT COUNT(*) AS n FROM market_listings WHERE owner_id = ? AND kind = ? AND status IN ('available','negotiating')",
        (owner_id, kind),
    )["n"]


def create_listing(d: dict, owner_id: int) -> tuple:
    """(id, code). code شناسه‌ی عمومیِ پایدار است."""
    ensure_tables()
    now = _now()
    with dbmod.get_conn() as conn:
        for _ in range(8):
            code = "MV" + secrets.token_hex(3).upper()
            try:
                cur = conn.execute(
                    "INSERT INTO market_listings (code, kind, deal_type, owner_id, term_name, course_id, course_name, "
                    "teacher, title, condition, price, description, status, created_at, updated_at, photo_file_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'available', ?, ?, ?)",
                    (code, d["kind"], d["deal_type"], owner_id, d["term_name"], d["course_id"], d["course_name"],
                     d.get("teacher", ""), d["title"], d.get("condition", ""), d.get("price"), d.get("desc", ""), now, now, d.get("photo")),
                )
                return cur.lastrowid, code
            except Exception as e:  # تصادمِ نادرِ code
                if "UNIQUE" not in str(e):
                    raise
    raise RuntimeError("could not allocate listing code")


def browse(kind: str, course_id: int, offset: int, limit: int) -> tuple:
    where = "kind = ? AND course_id = ? AND status IN ('available','negotiating')"
    total = _row(f"SELECT COUNT(*) AS n FROM market_listings WHERE {where}", (kind, course_id))["n"]
    rows = _rows(f"SELECT * FROM market_listings WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                 (kind, course_id, limit, offset))
    return total, rows


def user_listings(owner_id: int, kind: str) -> list:
    return _rows("SELECT * FROM market_listings WHERE owner_id = ? AND kind = ? ORDER BY id DESC LIMIT 30", (owner_id, kind))


def set_listing_status(listing_id: int, owner_id: int, status: str) -> bool:
    """فقط صاحبِ آگهی (شرطِ owner_id در خودِ SQL). حذفِ فیزیکی وجود ندارد."""
    if status not in STATUSES:
        return False
    with dbmod.get_conn() as conn:
        cur = conn.execute(
            "UPDATE market_listings SET status = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
            (status, _now(), listing_id, owner_id),
        )
        return cur.rowcount > 0


def get_chat(chat_id: int):
    return _row("SELECT * FROM market_chats WHERE id = ?", (chat_id,))


def find_chat(listing_id: int, responder_id: int):
    return _row("SELECT * FROM market_chats WHERE listing_id = ? AND responder_id = ?", (listing_id, responder_id))


def upsert_pending_chat(listing: dict, responder_id: int, intent: str) -> int:
    """یک ردیف برای هر (آگهی، پاسخ‌دهنده)؛ بعد از «ended» می‌شود دوباره درخواست داد (همان ردیف reset می‌شود)."""
    now = _now()
    with dbmod.get_conn() as conn:
        existing = conn.execute(
            "SELECT id, status FROM market_chats WHERE listing_id = ? AND responder_id = ?", (listing["id"], responder_id)
        ).fetchone()
        if existing:
            if existing["status"] != "ended":
                return -1
            conn.execute(
                "UPDATE market_chats SET intent = ?, confirm_text = '', status = 'pending', updated_at = ? WHERE id = ?",
                (intent, now, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO market_chats (listing_id, responder_id, owner_id, intent, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (listing["id"], responder_id, listing["owner_id"], intent, now, now),
        )
        return cur.lastrowid


def set_chat_status(chat_id: int, status: str, confirm_text: str = None, only_from: tuple = None) -> bool:
    sql, params = "UPDATE market_chats SET status = ?, updated_at = ?", [status, _now()]
    if confirm_text is not None:
        sql += ", confirm_text = ?"
        params.append(confirm_text)
    sql += " WHERE id = ?"
    params.append(chat_id)
    if only_from:
        sql += " AND status IN (" + ",".join("?" * len(only_from)) + ")"
        params.extend(only_from)
    with dbmod.get_conn() as conn:
        return conn.execute(sql, params).rowcount > 0


def user_chats(user_id: int) -> list:
    return _rows(
        "SELECT c.*, l.title, l.code, l.kind FROM market_chats c JOIN market_listings l ON l.id = c.listing_id "
        "WHERE (c.owner_id = ? OR c.responder_id = ?) AND c.status IN ('pending','active') ORDER BY c.updated_at DESC LIMIT 30",
        (user_id, user_id),
    )


def listing_chats(listing_id: int) -> list:
    return _rows("SELECT * FROM market_chats WHERE listing_id = ? AND status IN ('pending','active') ORDER BY id DESC LIMIT 30",
                 (listing_id,))


def add_message(chat_id: int, sender_id: int, text: str, delivered: bool) -> int:
    with dbmod.get_conn() as conn:
        return conn.execute(
            "INSERT INTO market_messages (chat_id, sender_id, text, delivered, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, sender_id, text, 1 if delivered else 0, _now()),
        ).lastrowid


# ============================================================
# کمک‌ها
# ============================================================

def _norm_digits(s: str) -> str:
    return s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def type_label(kind: str, deal_type: str) -> str:
    return (AD_TYPES if kind == KIND_AD else REQ_TYPES).get(deal_type, deal_type)


def status_label(kind: str, status: str) -> str:
    if kind == KIND_REQ:
        return {"available": "🟢 فعال", "negotiating": "🟡 در حال هماهنگی", "gone": "✅ انجام‌شده", "closed": "⚫️ بسته‌شده"}[status]
    return {"available": "🟢 موجود", "negotiating": "🟡 در حال هماهنگی", "gone": "🔴 واگذارشده/ناموجود", "closed": "⚫️ بسته‌شده"}[status]


def price_label(l: dict) -> str:
    if l["kind"] == KIND_AD and l["deal_type"] == "sell":
        return f"{l['price']:,} تومان" if l.get("price") else "توافقی"
    return ""


def listing_text(l: dict, viewer_is_owner: bool = False) -> str:
    lines = [f"{'📦 آگهی' if l['kind'] == KIND_AD else '🔎 درخواست'} — {l['title']}", f"کد: {l['code']}", "──────────────",
             f"نوع: {type_label(l['kind'], l['deal_type'])}", f"ترم/درس: {l['term_name']} › {l['course_name']}"]
    if l.get("teacher"):
        lines.append(f"استاد: {l['teacher']}")
    if l.get("condition"):
        lines.append(f"وضعیت منبع: {CONDITIONS.get(l['condition'], l['condition'])}")
    p = price_label(l)
    if p:
        lines.append(f"قیمت: {p}")
    if l.get("description"):
        lines.append(f"\nتوضیحات: {l['description']}")
    if l.get("photo_file_id"):
        lines.append("📷 دارای عکس")
    lines.append(f"\nوضعیت: {status_label(l['kind'], l['status'])}")
    return "\n".join(lines)


async def _edit(query, text: str, markup=None) -> None:
    msg = query.message
    if msg is not None and getattr(msg, "photo", None):
        # پیامِ عکس‌دار را نمی‌شود به متن تبدیل کرد: پاک و متنِ جدید فرستاده می‌شود
        try:
            await msg.delete()
        except TelegramError:
            pass
        await query.get_bot().send_message(chat_id=msg.chat_id, text=text, reply_markup=markup, disable_web_page_preview=True)
        return
    try:
        await query.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _show_listing(query, l: dict, user_id: int) -> None:
    text, markup = listing_text(l, l["owner_id"] == user_id), _view_markup(l, user_id)
    if not l.get("photo_file_id"):
        await _edit(query, text, markup)
        return
    if getattr(query.message, "photo", None):
        try:
            await query.edit_message_caption(caption=text[:1024], reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await query.message.reply_photo(photo=l["photo_file_id"], caption=text[:1024], reply_markup=markup)


def clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("mk_new", "mk_intent", "mk_confirm", "mchat", "mk_report"):
        context.user_data.pop(k, None)


def _root_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 مشاهده آگهی‌ها", callback_data="mk_browse:ad")],
        [InlineKeyboardButton("📝 ثبت آگهی جدید", callback_data="mk_new:ad")],
        [InlineKeyboardButton("🔎 مشاهده درخواست‌ها", callback_data="mk_browse:request")],
        [InlineKeyboardButton("➕ ثبت درخواست جدید", callback_data="mk_new:request")],
        [InlineKeyboardButton("🗂 آگهی‌های من", callback_data="mk_mine:ad"),
         InlineKeyboardButton("🗂 درخواست‌های من", callback_data="mk_mine:request")],
        [InlineKeyboardButton("💬 گفت‌وگوهای من", callback_data="mk_chats")],
        [InlineKeyboardButton("🚩 گزارش تخلف", callback_data="mk_report_info")],
    ])


async def show_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ورودی از منوی اصلی."""
    clear_state(context)
    await update.message.reply_text(ROOT_TEXT, reply_markup=_root_markup())


async def cb_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    clear_state(context)
    await _edit(query, ROOT_TEXT, _root_markup())


def _back(cb: str = "mk_root") -> list:
    return [InlineKeyboardButton("🔙 بازگشت", callback_data=cb)]


# ============================================================
# مشاهده: ترم ← درس ← فهرست
# ============================================================

async def cb_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    kind = query.data.split(":", 1)[1]
    if kind not in (KIND_AD, KIND_REQ):
        await query.answer()
        return
    await query.answer()
    buttons = [[InlineKeyboardButton(f"{t['grp']} › {t['name']}", callback_data=f"mkb_t:{kind}:{t['id']}")] for t in list_terms()]
    buttons.append(_back())
    await _edit(query, "کدام ترم؟", InlineKeyboardMarkup(buttons))


async def cb_browse_term(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, kind, tid = query.data.split(":")
    await query.answer()
    courses = list_courses_with_counts(int(tid), kind)
    buttons = [[InlineKeyboardButton(f"{c['name']} ({c['n']})" if c["n"] else c["name"],
                                     callback_data=f"mkb_c:{kind}:{c['id']}:0")] for c in courses]
    buttons.append(_back(f"mk_browse:{kind}"))
    await _edit(query, "کدام درس؟" if courses else "این ترم هنوز درسی ندارد.", InlineKeyboardMarkup(buttons))


async def cb_browse_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, kind, cid, page = query.data.split(":")
    cid, page = int(cid), max(0, int(page))
    course = dbmod.get_course_by_id(cid)
    if not course:
        await query.answer("این درس دیگر پیدا نشد.", show_alert=True)
        return
    await query.answer()
    total, rows = browse(kind, cid, page * PAGE_SIZE, PAGE_SIZE)
    buttons = []
    for l in rows:
        p = price_label(l)
        label = f"{type_label(kind, l['deal_type']).split(' ')[0]} {l['title']}" + (f" — {p}" if p else "")
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"mk_v:{l['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"mkb_c:{kind}:{cid}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"mkb_c:{kind}:{cid}:{page + 1}"))
    if nav:
        buttons.append(nav)
    term = _row("SELECT term_id FROM courses WHERE id = ?", (cid,))
    buttons.append(_back(f"mkb_t:{kind}:{term['term_id']}" if term else f"mk_browse:{kind}"))
    title = "📖 آگهی‌ها" if kind == KIND_AD else "🔎 درخواست‌ها"
    await _edit(query, f"{title} — {course['term_name']} › {course['name']}" if rows else
                f"{title} — {course['term_name']} › {course['name']}\n\nفعلاً موردی ثبت نشده است.",
                InlineKeyboardMarkup(buttons))


# ============================================================
# نمایشِ یک آگهی/درخواست + مدیریتِ وضعیت (فقط صاحب)
# ============================================================

def _view_markup(l: dict, user_id: int) -> InlineKeyboardMarkup:
    buttons = []
    if l["owner_id"] == user_id:
        row = []
        for s in STATUSES:
            if s != l["status"]:
                row.append(InlineKeyboardButton(status_label(l["kind"], s), callback_data=f"mk_st:{l['id']}:{_ST_SHORT_REV[s]}"))
        for i in range(0, len(row), 2):
            buttons.append(row[i:i + 2])
        buttons.append([InlineKeyboardButton("💬 گفت‌وگوهای این مورد", callback_data=f"mk_lc:{l['id']}")])
        buttons.append(_back(f"mk_mine:{l['kind']}"))
    else:
        if l["status"] in ACTIVE_STATUSES:
            label = "💬 پیام به آگهی‌دهنده" if l["kind"] == KIND_AD else "🙋 این منبع را دارم"
            buttons.append([InlineKeyboardButton(label, callback_data=f"mk_contact:{l['id']}")])
        buttons.append([InlineKeyboardButton("🚩 گزارش تخلف", callback_data=f"mkr_l:{l['id']}")])
        buttons.append(_back(f"mkb_c:{l['kind']}:{l['course_id']}:0"))
    return InlineKeyboardMarkup(buttons)


async def cb_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    l = get_listing(int(query.data.split(":", 1)[1]))
    if not l or (l["owner_id"] != user_id and l["status"] not in ACTIVE_STATUSES):
        await query.answer("این مورد دیگر فعال نیست.", show_alert=True)
        return
    await query.answer()
    await _show_listing(query, l, user_id)


async def cb_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    _, lid, short = query.data.split(":")
    status = _ST_SHORT.get(short)
    if not status or not set_listing_status(int(lid), user_id, status):
        await query.answer("این کار فقط برای صاحبِ آگهی ممکن است.", show_alert=True)
        return
    await query.answer("وضعیت تغییر کرد ✅")
    await _show_listing(query, get_listing(int(lid)), user_id)


async def cb_mine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    kind = query.data.split(":", 1)[1]
    await query.answer()
    rows = user_listings(update.effective_user.id, kind)
    buttons = [[InlineKeyboardButton(f"{status_label(kind, l['status']).split(' ')[0]} {l['title']}"[:60],
                                     callback_data=f"mk_v:{l['id']}")] for l in rows]
    buttons.append(_back())
    await _edit(query, ("🗂 آگهی‌های من" if kind == KIND_AD else "🗂 درخواست‌های من") if rows else "هنوز چیزی ثبت نکرده‌ای.",
                InlineKeyboardMarkup(buttons))


# ============================================================
# ثبت آگهی/درخواست (ویزارد)
# ============================================================

async def cb_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    kind = query.data.split(":", 1)[1]
    if kind not in (KIND_AD, KIND_REQ):
        await query.answer()
        return
    if count_active(user_id, kind) >= MAX_ACTIVE_PER_KIND:
        await query.answer(f"حداکثر {MAX_ACTIVE_PER_KIND} مورد فعال می‌توانی داشته باشی؛ یکی را ببند یا «واگذارشده» کن.", show_alert=True)
        return
    await query.answer()
    clear_state(context)
    context.user_data["mk_new"] = {"kind": kind, "step": "type"}
    types = AD_TYPES if kind == KIND_AD else REQ_TYPES
    buttons = [[InlineKeyboardButton(lbl, callback_data=f"mkn_d:{k}")] for k, lbl in types.items()]
    buttons.append([InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")])
    await _edit(query, "نوعش چیست؟", InlineKeyboardMarkup(buttons))


def _wiz(context: ContextTypes.DEFAULT_TYPE, step: str):
    w = context.user_data.get("mk_new")
    return w if w and w.get("step") == step else None


async def cb_new_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mk_new", None)
    await update.callback_query.answer("لغو شد.")
    await _edit(update.callback_query, ROOT_TEXT, _root_markup())


async def cb_new_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    w = _wiz(context, "type")
    key = query.data.split(":", 1)[1]
    if not w or key not in (AD_TYPES if w["kind"] == KIND_AD else REQ_TYPES):
        await query.answer("منقضی شد؛ دوباره شروع کن.", show_alert=True)
        return
    await query.answer()
    w["deal_type"], w["step"] = key, "term"
    buttons = [[InlineKeyboardButton(f"{t['grp']} › {t['name']}", callback_data=f"mkn_t:{t['id']}")] for t in list_terms()]
    buttons.append([InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")])
    await _edit(query, "مربوط به کدام ترم؟", InlineKeyboardMarkup(buttons))


async def cb_new_term(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    w = _wiz(context, "term")
    if not w:
        await query.answer("منقضی شد؛ دوباره شروع کن.", show_alert=True)
        return
    tid = int(query.data.split(":", 1)[1])
    term = _row("SELECT id, name FROM terms WHERE id = ?", (tid,))
    courses = list_courses_with_counts(tid)
    if not term or not courses:
        await query.answer("این ترم درسی ندارد.", show_alert=True)
        return
    await query.answer()
    w["term_name"], w["step"] = term["name"], "course"
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"mkn_c:{c['id']}")] for c in courses]
    buttons.append([InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")])
    await _edit(query, "کدام درس؟", InlineKeyboardMarkup(buttons))


async def cb_new_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    w = _wiz(context, "course")
    course = dbmod.get_course_by_id(int(query.data.split(":", 1)[1])) if w else None
    if not w or not course or course["term_name"] != w["term_name"]:
        await query.answer("منقضی شد؛ دوباره شروع کن.", show_alert=True)
        return
    await query.answer()
    w["course_id"], w["course_name"], w["step"] = course["id"], course["name"], "teacher"
    await _edit(query, f"نام استاد را بنویس (اختیاری)، یا «-» بفرست.\n(حداکثر {TEACHER_MAX} نویسه)",
                InlineKeyboardMarkup([[InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")]]))


async def cb_new_condition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    w = _wiz(context, "condition")
    key = query.data.split(":", 1)[1]
    if not w or key not in CONDITIONS:
        await query.answer("منقضی شد؛ دوباره شروع کن.", show_alert=True)
        return
    await query.answer()
    w["condition"] = key
    if w["deal_type"] == "sell":
        w["step"] = "price"
        await _edit(query, "قیمت را به تومان بنویس (فقط عدد)، یا «-» برای «توافقی».",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")]]))
    else:
        w["step"] = "desc"
        await _edit(query, f"توضیحات را بنویس (حداکثر {DESC_MAX} نویسه)، یا «-».",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")]]))


async def _wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, w: dict, text: str) -> bool:
    msg = update.message
    step = w["step"]
    if step == "teacher":
        if len(text) > TEACHER_MAX:
            await msg.reply_text(f"حداکثر {TEACHER_MAX} نویسه؛ دوباره بفرست (یا «-»).")
            return True
        w["teacher"] = "" if text == "-" else text
        w["step"] = "title"
        await msg.reply_text(f"عنوانِ {'آگهی' if w['kind'] == KIND_AD else 'درخواست'} را بنویس (مثلاً «جزوه‌ی بیوشیمی دکتر X»).")
        return True
    if step == "title":
        if not text or len(text) > TITLE_MAX:
            await msg.reply_text(f"عنوان باید خالی نباشد و حداکثر {TITLE_MAX} نویسه باشد.")
            return True
        w["title"] = text
        if w["kind"] == KIND_AD:
            w["step"] = "condition"
            buttons = [[InlineKeyboardButton(lbl, callback_data=f"mkn_s:{k}")] for k, lbl in CONDITIONS.items()]
            buttons.append([InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")])
            await msg.reply_text("وضعیتِ منبع؟", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            w["step"] = "desc"
            await msg.reply_text(f"توضیحات را بنویس (حداکثر {DESC_MAX} نویسه)، یا «-».")
        return True
    if step == "price":
        if text == "-":
            w["price"] = None
        else:
            digits = _norm_digits(text).replace(",", "").replace("٬", "").strip()
            if not digits.isdigit() or not (0 < int(digits) <= PRICE_MAX):
                await msg.reply_text("قیمت را فقط به‌صورت عدد (تومان) بنویس، یا «-» برای توافقی.")
                return True
            w["price"] = int(digits)
        w["step"] = "desc"
        await msg.reply_text(f"توضیحات را بنویس (حداکثر {DESC_MAX} نویسه)، یا «-».")
        return True
    if step == "desc":
        if len(text) > DESC_MAX or not text:
            await msg.reply_text(f"حداکثر {DESC_MAX} نویسه؛ دوباره بفرست (یا «-»).")
            return True
        w["desc"] = "" if text == "-" else text
        w["step"] = "photo"
        await msg.reply_text(
            "📷 اگر عکسی از منبع داری بفرست (اختیاری)، وگرنه دکمه‌ی زیر را بزن.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ بدون عکس", callback_data="mkn_nophoto")],
                                               [InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")]]),
        )
        return True
    await msg.reply_text("لطفاً از دکمه‌های همین مرحله استفاده کن (یا «✖️ انصراف»).")
    return True


async def _wizard_preview(message, w: dict) -> None:
    w["step"] = "ready"
    w["nonce"] = secrets.token_hex(4)
    preview = dict(w, code="(بعد از ثبت)", status="available", owner_id=0, description=w.get("desc", ""),
                   photo_file_id=w.get("photo"))
    await message.reply_text(
        "پیش‌نمایش:\n\n" + listing_text(preview) + "\n\nثبت شود؟",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ثبت", callback_data=f"mkn_ok:{w['nonce']}")],
            [InlineKeyboardButton("✖️ انصراف", callback_data="mkn_cancel")],
        ]),
    )


async def cb_new_nophoto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    w = _wiz(context, "photo")
    if not w:
        await query.answer("منقضی شد؛ دوباره شروع کن.", show_alert=True)
        return
    await query.answer()
    w["photo"] = None
    await _wizard_preview(query.message, w)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فقط وقتی کاربر در مرحله‌ی «عکس» ویزارد است عکس را می‌گیرد؛ در غیر این صورت هیچ کاری نمی‌کند
    و هندلرهای قبلیِ ربات (receive_file) دست‌نخورده کار می‌کنند."""
    w = context.user_data.get("mk_new")
    if not (w and w.get("step") == "photo") or not update.message or not update.message.photo:
        return
    w["photo"] = update.message.photo[-1].file_id
    await _wizard_preview(update.message, w)
    raise ApplicationHandlerStop


async def cb_new_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    w = context.user_data.get("mk_new")
    if not w or w.get("step") != "ready" or w.get("nonce") != query.data.split(":", 1)[1]:
        await query.answer("این پیش‌نمایش دیگر معتبر نیست.", show_alert=True)
        return
    context.user_data.pop("mk_new", None)  # یک‌بارمصرف: دابل‌تپ آگهی تکراری نمی‌سازد
    if count_active(user_id, w["kind"]) >= MAX_ACTIVE_PER_KIND:
        await query.answer("به سقفِ موردهای فعال رسیده‌ای.", show_alert=True)
        return
    try:
        listing_id, code = create_listing(w, user_id)
    except Exception:
        logger.exception("ثبتِ آگهی ناموفق بود (user=%s)", user_id)
        await query.answer("مشکلی پیش اومد؛ دوباره امتحان کن 🙏", show_alert=True)
        return
    await query.answer("ثبت شد ✅")
    await _edit(query, f"✅ ثبت شد. کد: {code}\nاز «🗂 آگهی‌ها/درخواست‌های من» می‌توانی وضعیتش را مدیریت کنی.",
                InlineKeyboardMarkup([[InlineKeyboardButton("📄 دیدنِ آن", callback_data=f"mk_v:{listing_id}")], _back()]))


# ============================================================
# شروعِ گفت‌وگو: جمله‌ی قصد → پذیرش/رد → رله
# ============================================================

async def cb_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    l = get_listing(int(query.data.split(":", 1)[1]))
    if not l or l["status"] not in ACTIVE_STATUSES:
        await query.answer("این مورد دیگر فعال نیست.", show_alert=True)
        return
    if l["owner_id"] == user_id:
        await query.answer("این مورد برای خودت است.", show_alert=True)
        return
    existing = find_chat(l["id"], user_id)
    if existing and existing["status"] == "pending":
        await query.answer("درخواستت قبلاً ثبت شده؛ منتظر پاسخ بمان.", show_alert=True)
        return
    if existing and existing["status"] == "active":
        await query.answer("گفت‌وگو فعال است؛ از «💬 گفت‌وگوهای من» واردش شو.", show_alert=True)
        return
    if existing and existing["status"] == "rejected":
        await query.answer("درخواستِ قبلی‌ات پذیرفته نشد.", show_alert=True)
        return
    await query.answer()
    clear_state(context)
    context.user_data["mk_intent"] = {"listing_id": l["id"]}
    await query.message.reply_text(
        "قبل از شروعِ گفت‌وگو، در یک جمله بنویس چرا پیام می‌دهی و قصدت چیست "
        f"(مثلاً «می‌خوام بخرمش، فردا می‌تونم تحویل بگیرم»). حداکثر {INTENT_MAX} نویسه.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ انصراف", callback_data="mk_intent_cancel")]]),
    )


async def cb_intent_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mk_intent", None)
    await update.callback_query.answer("لغو شد.")
    await _edit(update.callback_query, "لغو شد.")


async def _notify(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, markup=None) -> bool:
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except (Forbidden, TelegramError):
        return False


async def _submit_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    st = context.user_data.get("mk_intent")
    user_id = update.effective_user.id
    if not text or len(text) > INTENT_MAX:
        await update.message.reply_text(f"باید خالی نباشد و حداکثر {INTENT_MAX} نویسه باشد.")
        return True
    context.user_data.pop("mk_intent", None)
    l = get_listing(st["listing_id"])
    if not l or l["status"] not in ACTIVE_STATUSES or l["owner_id"] == user_id:
        await update.message.reply_text("این مورد دیگر فعال نیست.")
        return True
    chat_id = upsert_pending_chat(l, user_id, text)
    if chat_id < 0:
        await update.message.reply_text("برای این مورد قبلاً درخواست داده‌ای.")
        return True
    delivered = await _notify(
        context, l["owner_id"],
        f"📨 درخواستِ گفت‌وگو برای «{l['title']}» ({l['code']})\n\nپیامِ طرف مقابل:\n«{text}»\n\nمی‌پذیری؟",
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ بپذیر", callback_data=f"mk_acc:{chat_id}"),
                               InlineKeyboardButton("❌ رد", callback_data=f"mk_rej:{chat_id}")]]),
    )
    await update.message.reply_text(
        "✅ درخواستت برای صاحبِ آن ارسال شد؛ اگر بپذیرد خبرت می‌کنم." if delivered
        else "درخواستت ثبت شد، ولی الان امکانِ اطلاع‌رسانی به طرف مقابل نیست (ربات را بلاک کرده یا در دسترس نیست)."
    )
    return True


def _chat_open_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("💬 ورود به گفت‌وگو", callback_data=f"mk_open:{chat_id}")]])


async def cb_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    chat = get_chat(int(query.data.split(":", 1)[1]))
    if not chat or chat["owner_id"] != user_id or chat["status"] != "pending":
        await query.answer("این درخواست دیگر قابل پاسخ نیست.", show_alert=True)
        return
    l = get_listing(chat["listing_id"])
    await query.answer()
    if l and l["kind"] == KIND_REQ:
        # درخواست‌کننده پیش از آغازِ گفت‌وگو یک جمله برای تأییدِ قصدش می‌نویسد
        clear_state(context)
        context.user_data["mk_confirm"] = {"chat_id": chat["id"]}
        await _edit(query, "برای تأیید، در یک جمله بنویس چه می‌خواهی (مثلاً «می‌خوام همین جزوه رو امانت بگیرم، تا آخر هفته برمی‌گردونم»).")
        return
    await _activate_chat(update, context, chat, "")
    await _edit(query, "✅ پذیرفتی. حالا می‌توانید از طریق ربات پیام رد و بدل کنید.", _chat_open_markup(chat["id"]))


async def _activate_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, chat: dict, confirm_text: str) -> None:
    if not set_chat_status(chat["id"], "active", confirm_text, only_from=("pending",)):
        return
    l = get_listing(chat["listing_id"])
    extra = f"\n\nپیامِ طرف مقابل:\n«{confirm_text}»" if confirm_text else ""
    await _notify(context, chat["responder_id"],
                  f"✅ درخواستت برای «{l['title']}» ({l['code']}) پذیرفته شد.{extra}", _chat_open_markup(chat["id"]))


async def _submit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    st = context.user_data.get("mk_confirm")
    chat = get_chat(st["chat_id"]) if st else None
    if not text or len(text) > INTENT_MAX:
        await update.message.reply_text(f"باید خالی نباشد و حداکثر {INTENT_MAX} نویسه باشد.")
        return True
    context.user_data.pop("mk_confirm", None)
    if not chat or chat["owner_id"] != update.effective_user.id or chat["status"] != "pending":
        await update.message.reply_text("این درخواست دیگر قابل پاسخ نیست.")
        return True
    await _activate_chat(update, context, chat, text)
    await update.message.reply_text("✅ پذیرفتی. از «💬 گفت‌وگوهای من» واردِ گفت‌وگو شو.", reply_markup=_chat_open_markup(chat["id"]))
    return True


async def cb_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = get_chat(int(query.data.split(":", 1)[1]))
    if not chat or chat["owner_id"] != update.effective_user.id or not set_chat_status(chat["id"], "rejected", only_from=("pending",)):
        await query.answer("این درخواست دیگر قابل پاسخ نیست.", show_alert=True)
        return
    await query.answer("رد شد.")
    l = get_listing(chat["listing_id"])
    await _edit(query, "❌ رد شد.")
    await _notify(context, chat["responder_id"], f"متأسفانه درخواستت برای «{l['title']}» ({l['code']}) پذیرفته نشد.")


# ============================================================
# گفت‌وگوها
# ============================================================

def _role_label(chat: dict, user_id: int) -> str:
    return "صاحبِ آگهی" if chat["owner_id"] == user_id else "پاسخ‌دهنده"


async def cb_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()
    chats = user_chats(user_id)
    buttons = []
    for c in chats:
        if c["status"] == "pending":
            tag = "⏳ منتظر پذیرش شما" if c["owner_id"] == user_id else "⏳ منتظر پاسخ"
        else:
            tag = "💬 فعال"
        buttons.append([InlineKeyboardButton(f"{tag} — {c['title']}"[:60], callback_data=f"mk_cv:{c['id']}")])
    buttons.append(_back())
    await _edit(query, "💬 گفت‌وگوهای من" if chats else "گفت‌وگوی فعالی نداری.", InlineKeyboardMarkup(buttons))


async def cb_listing_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    l = get_listing(int(query.data.split(":", 1)[1]))
    if not l or l["owner_id"] != update.effective_user.id:
        await query.answer("فقط برای صاحبِ آگهی.", show_alert=True)
        return
    await query.answer()
    chats = listing_chats(l["id"])
    buttons = [[InlineKeyboardButton(f"{'⏳ منتظر پذیرش شما' if c['status'] == 'pending' else '💬 فعال'} — #{c['id']}",
                                     callback_data=f"mk_cv:{c['id']}")] for c in chats]
    buttons.append(_back(f"mk_v:{l['id']}"))
    await _edit(query, "گفت‌وگوهای این مورد:" if chats else "هنوز گفت‌وگویی ندارد.", InlineKeyboardMarkup(buttons))


async def cb_chat_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    chat = get_chat(int(query.data.split(":", 1)[1]))
    if not chat or user_id not in (chat["owner_id"], chat["responder_id"]):
        await query.answer("پیدا نشد.", show_alert=True)
        return
    await query.answer()
    l = get_listing(chat["listing_id"])
    head = f"«{l['title']}» ({l['code']})\nنقشِ تو: {_role_label(chat, user_id)}\n"
    if chat["status"] == "pending" and chat["owner_id"] == user_id:
        await _edit(query, head + f"\nپیامِ طرف مقابل:\n«{chat['intent']}»",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✅ بپذیر", callback_data=f"mk_acc:{chat['id']}"),
                                           InlineKeyboardButton("❌ رد", callback_data=f"mk_rej:{chat['id']}")], _back("mk_chats")]))
    elif chat["status"] == "pending":
        await _edit(query, head + "\nمنتظر پاسخِ طرف مقابل هستی.", InlineKeyboardMarkup([_back("mk_chats")]))
    elif chat["status"] == "active":
        await _edit(query, head + "\nگفت‌وگو فعال است.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("💬 ورود به گفت‌وگو", callback_data=f"mk_open:{chat['id']}")],
                                          [InlineKeyboardButton("🏁 پایان گفت‌وگو", callback_data=f"mk_end:{chat['id']}")],
                                          _back("mk_chats")]))
    else:
        await _edit(query, head + "\nاین گفت‌وگو بسته شده است.", InlineKeyboardMarkup([_back("mk_chats")]))


async def cb_open_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    chat = get_chat(int(query.data.split(":", 1)[1]))
    if not chat or user_id not in (chat["owner_id"], chat["responder_id"]) or chat["status"] != "active":
        await query.answer("این گفت‌وگو فعال نیست.", show_alert=True)
        return
    await query.answer()
    clear_state(context)
    context.user_data["mchat"] = chat["id"]
    l = get_listing(chat["listing_id"])
    await query.message.reply_text(
        f"💬 واردِ گفت‌وگو برای «{l['title']}» ({l['code']}) شدی.\n"
        "هر متنی که بفرستی برای طرف مقابل می‌رود (نام و شماره‌ات نمایش داده نمی‌شود).\n"
        "برای رفتن به بخش‌های دیگر، یکی از دکمه‌های منوی اصلی را بزن.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚪 خروج از گفت‌وگو", callback_data="mk_leave")],
                                           [InlineKeyboardButton("🏁 پایان گفت‌وگو", callback_data=f"mk_end:{chat['id']}")]]),
    )


async def cb_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mchat", None)
    await update.callback_query.answer("از گفت‌وگو خارج شدی.")
    await _edit(update.callback_query, "از گفت‌وگو خارج شدی. هر وقت خواستی از «💬 گفت‌وگوهای من» برگرد.")


async def cb_end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    chat = get_chat(int(query.data.split(":", 1)[1]))
    if not chat or user_id not in (chat["owner_id"], chat["responder_id"]) or not set_chat_status(chat["id"], "ended", only_from=("active",)):
        await query.answer("این گفت‌وگو فعال نیست.", show_alert=True)
        return
    context.user_data.pop("mchat", None)
    await query.answer("گفت‌وگو پایان یافت.")
    l = get_listing(chat["listing_id"])
    await _edit(query, "🏁 گفت‌وگو پایان یافت.")
    other = chat["responder_id"] if user_id == chat["owner_id"] else chat["owner_id"]
    await _notify(context, other, f"🏁 طرف مقابل گفت‌وگو درباره‌ی «{l['title']}» ({l['code']}) را پایان داد.")


def _rate_ok(user_id: int) -> bool:
    now = time.monotonic()
    times = [t for t in _msg_times.get(user_id, []) if now - t < MSG_RATE_WINDOW]
    if len(times) >= MSG_RATE_LIMIT:
        _msg_times[user_id] = times
        return False
    times.append(now)
    _msg_times[user_id] = times
    return True


async def _relay(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    user_id = update.effective_user.id
    chat = get_chat(context.user_data.get("mchat") or 0)
    if not chat or user_id not in (chat["owner_id"], chat["responder_id"]) or chat["status"] != "active":
        context.user_data.pop("mchat", None)
        await update.message.reply_text("این گفت‌وگو دیگر فعال نیست.")
        return True
    if not text or len(text) > MSG_MAX:
        await update.message.reply_text(f"پیام باید خالی نباشد و حداکثر {MSG_MAX} نویسه باشد.")
        return True
    if not _rate_ok(user_id):
        await update.message.reply_text("⏳ خیلی سریع پیام می‌فرستی؛ کمی صبر کن.")
        return True
    l = get_listing(chat["listing_id"])
    other = chat["responder_id"] if user_id == chat["owner_id"] else chat["owner_id"]
    msg_id = add_message(chat["id"], user_id, text, delivered=False)
    ok = await _notify(context, other, f"💬 «{l['title']}» ({l['code']})\n\n{text}",
                       InlineKeyboardMarkup([[InlineKeyboardButton("💬 ورود به گفت‌وگو", callback_data=f"mk_open:{chat['id']}"),
                                              InlineKeyboardButton("🚩 گزارش", callback_data=f"mkr_m:{msg_id}")]]))
    if ok:
        with dbmod.get_conn() as conn:
            conn.execute("UPDATE market_messages SET delivered = 1 WHERE id = ?", (msg_id,))
        await update.message.reply_text("✅ ارسال شد")
    else:
        await update.message.reply_text("⚠️ پیام ذخیره شد ولی الان به طرف مقابل نرسید (ربات را بلاک کرده یا در دسترس نیست).")
    return True


# ============================================================
# 🚩 گزارش تخلف و بررسی
# - دلیل ثبت می‌شود؛ گزارش به ناظرها (سوپرادمین + ادمین دارای مجوز market_moderate) می‌رود.
# - هیچ آگهی‌ای فقط با دریافتِ گزارش خودکار حذف/بسته نمی‌شود؛ ناظر تصمیم می‌گیرد.
# - هویتِ گزارش‌دهنده، صاحبِ آگهی و طرفِ گفت‌وگو به ناظر نمایش داده نمی‌شود.
# ============================================================

REPORT_REASONS = {
    "irrelevant": "نامرتبط با بازارچه", "spam": "تبلیغ/اسپم", "personal": "درج اطلاعات شخصی",
    "scam": "مشکوک به کلاه‌برداری", "abuse": "محتوای توهین‌آمیز", "other": "سایر",
}
REPORT_NOTE_MAX = 200
REPORTS_PER_DAY = 10


def is_moderator(user_id: int) -> bool:
    return dbmod.has_admin_permission(user_id, "market_moderate")


def moderator_ids() -> list:
    ids = {a["user_id"] for a in dbmod.list_admins() if a["role"] == "SUPER_ADMIN"}
    for uid, perm in dbmod.list_admin_permissions():
        if perm == "market_moderate" and dbmod.get_admin_roles(uid):
            ids.add(uid)
    return sorted(ids)


def reports_today(user_id: int) -> int:
    since = (_dt.datetime.now() - _dt.timedelta(days=1)).isoformat(timespec="seconds")
    return _row("SELECT COUNT(*) AS n FROM market_reports WHERE reporter_id = ? AND created_at >= ?", (user_id, since))["n"]


def create_report(reporter_id: int, target_type: str, target_id: int, listing_id: int, chat_id, reason: str, note: str) -> int:
    """شناسه‌ی گزارش؛ ۱- اگر همین کاربر قبلاً همین هدف را گزارش کرده (گزارشِ تکراری ساخته نمی‌شود)."""
    with dbmod.get_conn() as conn:
        try:
            return conn.execute(
                "INSERT INTO market_reports (reporter_id, target_type, target_id, listing_id, chat_id, reason, note, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (reporter_id, target_type, target_id, listing_id, chat_id, reason, note, _now()),
            ).lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                return -1
            raise


def get_report(report_id: int):
    return _row("SELECT * FROM market_reports WHERE id = ?", (report_id,))


def open_reports(limit: int = 15) -> list:
    return _rows(
        "SELECT r.id, r.reason, r.target_type, l.code FROM market_reports r JOIN market_listings l ON l.id = r.listing_id "
        "WHERE r.status = 'open' ORDER BY r.id LIMIT ?", (limit,))


def resolve_report(report_id: int, status: str, reviewer: int) -> bool:
    with dbmod.get_conn() as conn:
        return conn.execute(
            "UPDATE market_reports SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ? AND status = 'open'",
            (status, reviewer, _now(), report_id),
        ).rowcount > 0


def listing_report_count(listing_id: int) -> int:
    return _row("SELECT COUNT(*) AS n FROM market_reports WHERE listing_id = ?", (listing_id,))["n"]


def moderator_close_listing(listing_id: int, reviewer: int) -> bool:
    """بستنِ آگهی توسط ناظر (حذف نمی‌شود؛ فقط status=closed) و بستنِ گزارش‌های بازِ همان آگهی."""
    now = _now()
    with dbmod.get_conn() as conn:
        ok = conn.execute("UPDATE market_listings SET status = 'closed', updated_at = ? WHERE id = ?", (now, listing_id)).rowcount > 0
        conn.execute(
            "UPDATE market_reports SET status = 'actioned', reviewed_by = ?, reviewed_at = ? WHERE listing_id = ? AND status = 'open'",
            (reviewer, now, listing_id),
        )
    return ok


async def cb_report_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _edit(query,
                "🚩 گزارش تخلف\n\nاگر آگهی یا پیامِ نامرتبط، تبلیغاتی یا مشکوکی دیدی:\n"
                "• روی صفحه‌ی همان آگهی «🚩 گزارش تخلف» را بزن.\n"
                "• برای یک پیامِ گفت‌وگو، زیرِ همان پیام «🚩 گزارش» را بزن.\n\n"
                "گزارش‌ها را ناظرها بررسی می‌کنند؛ آگهی فقط با یک گزارش خودکار حذف نمی‌شود و هویتِ تو برای هیچ‌کس نمایش داده نمی‌شود.",
                InlineKeyboardMarkup([_back()]))


async def _start_report(update: Update, context: ContextTypes.DEFAULT_TYPE, rtype: str, target_id: int, listing: dict, chat_id) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    if reports_today(user_id) >= REPORTS_PER_DAY:
        await query.answer("امروز به سقفِ تعدادِ گزارش رسیده‌ای.", show_alert=True)
        return
    exists = _row("SELECT 1 AS x FROM market_reports WHERE reporter_id = ? AND target_type = ? AND target_id = ?",
                  (user_id, rtype, target_id))
    if exists:
        await query.answer("قبلاً این مورد را گزارش کرده‌ای؛ ممنون 🙏", show_alert=True)
        return
    await query.answer()
    context.user_data["mk_report"] = {"type": rtype, "id": target_id, "listing_id": listing["id"], "chat_id": chat_id, "step": "reason"}
    buttons = [[InlineKeyboardButton(lbl, callback_data=f"mkr_p:{k}")] for k, lbl in REPORT_REASONS.items()]
    buttons.append([InlineKeyboardButton("✖️ انصراف", callback_data="mkr_cancel")])
    await query.message.reply_text("دلیلِ گزارش چیست؟", reply_markup=InlineKeyboardMarkup(buttons))


async def cb_report_listing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    l = get_listing(int(query.data.split(":", 1)[1]))
    if not l or l["owner_id"] == update.effective_user.id:
        await query.answer("این مورد قابل گزارش نیست.", show_alert=True)
        return
    await _start_report(update, context, "listing", l["id"], l, None)


async def cb_report_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    msg = _row("SELECT * FROM market_messages WHERE id = ?", (int(query.data.split(":", 1)[1]),))
    chat = get_chat(msg["chat_id"]) if msg else None
    if not msg or not chat or user_id not in (chat["owner_id"], chat["responder_id"]) or msg["sender_id"] == user_id:
        await query.answer("این پیام قابل گزارش نیست.", show_alert=True)
        return
    await _start_report(update, context, "message", msg["id"], get_listing(chat["listing_id"]), chat["id"])


async def cb_report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mk_report", None)
    await update.callback_query.answer("لغو شد.")
    await _edit(update.callback_query, "لغو شد.")


async def cb_report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    st = context.user_data.get("mk_report")
    key = query.data.split(":", 1)[1]
    if not st or st.get("step") != "reason" or key not in REPORT_REASONS:
        await query.answer("منقضی شد؛ دوباره گزارش بده.", show_alert=True)
        return
    await query.answer()
    st["reason"], st["step"] = key, "note"
    await _edit(query, f"دلیل: {REPORT_REASONS[key]}\n\nیک توضیحِ کوتاه بنویس (حداکثر {REPORT_NOTE_MAX} نویسه)، یا «-».",
                InlineKeyboardMarkup([[InlineKeyboardButton("✖️ انصراف", callback_data="mkr_cancel")]]))


async def _report_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    st = context.user_data.get("mk_report")
    user_id = update.effective_user.id
    if st["step"] != "note":
        await update.message.reply_text("لطفاً یکی از دلیل‌ها را با دکمه انتخاب کن (یا «✖️ انصراف»).")
        return True
    if len(text) > REPORT_NOTE_MAX or not text:
        await update.message.reply_text(f"حداکثر {REPORT_NOTE_MAX} نویسه؛ دوباره بفرست (یا «-»).")
        return True
    context.user_data.pop("mk_report", None)
    note = "" if text == "-" else text
    rid = create_report(user_id, "listing" if st["type"] == "listing" else "message", st["id"], st["listing_id"],
                        st["chat_id"], st["reason"], note)
    if rid < 0:
        await update.message.reply_text("قبلاً این مورد را گزارش کرده‌ای؛ ممنون 🙏")
        return True
    await update.message.reply_text("✅ گزارشت ثبت شد و ناظرها بررسی می‌کنند. ممنون که کمک می‌کنی بازارچه تمیز بماند.")
    l = get_listing(st["listing_id"])
    for mid in moderator_ids():
        if mid == user_id:
            continue
        await _notify(context, mid, f"🚩 گزارشِ جدید #{rid}\nکدِ آگهی: {l['code']}\nدلیل: {REPORT_REASONS[st['reason']]}",
                      InlineKeyboardMarkup([[InlineKeyboardButton("🔍 بررسی", callback_data=f"mkm_v:{rid}")]]))
    return True


def _mod_markup(r: dict) -> InlineKeyboardMarkup:
    rows = []
    if r["status"] == "open":
        rows.append([InlineKeyboardButton("✅ رد گزارش (بدون اقدام)", callback_data=f"mkm_d:{r['id']}")])
        rows.append([InlineKeyboardButton("⚫️ بستنِ آگهی", callback_data=f"mkm_c:{r['id']}")])
        if r["target_type"] == "message" and r.get("chat_id"):
            rows.append([InlineKeyboardButton("🏁 پایان‌دادن به گفت‌وگو", callback_data=f"mkm_e:{r['id']}")])
    rows.append([InlineKeyboardButton("🔙 فهرستِ گزارش‌ها", callback_data="mkm_l")])
    return InlineKeyboardMarkup(rows)


def _mod_text(r: dict) -> str:
    l = get_listing(r["listing_id"])
    lines = [f"🚩 گزارش #{r['id']} — {'باز' if r['status'] == 'open' else ('بسته: ' + r['status'])}",
             f"دلیل: {REPORT_REASONS.get(r['reason'], r['reason'])}"]
    if r["note"]:
        lines.append(f"توضیح گزارش‌دهنده: {r['note']}")
    lines.append(f"تعداد کلِ گزارش‌های این آگهی: {listing_report_count(r['listing_id'])}")
    if r["target_type"] == "message":
        m = _row("SELECT text FROM market_messages WHERE id = ?", (r["target_id"],))
        lines.append(f"\n💬 پیامِ گزارش‌شده:\n«{(m or {}).get('text', '—')}»")
    lines.append("\n──────────────\n" + listing_text(l))
    return "\n".join(lines)


async def _mod_guard(update: Update, show: bool = True) -> bool:
    if is_moderator(update.effective_user.id):
        return True
    if update.callback_query:
        await update.callback_query.answer("⛔️ مجاز نیستید.", show_alert=True)
    return False


async def marketreports_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/marketreports -- فهرستِ گزارش‌های باز (ناظرها)."""
    if not is_moderator(update.effective_user.id):
        return
    rows = open_reports()
    buttons = [[InlineKeyboardButton(f"#{r['id']} — {REPORT_REASONS.get(r['reason'], r['reason'])} — {r['code']}",
                                     callback_data=f"mkm_v:{r['id']}")] for r in rows]
    await update.message.reply_text("🚩 گزارش‌های باز:" if rows else "گزارشِ بازی وجود ندارد. ✅",
                                    reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def cb_mod_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _mod_guard(update):
        return
    query = update.callback_query
    await query.answer()
    rows = open_reports()
    buttons = [[InlineKeyboardButton(f"#{r['id']} — {REPORT_REASONS.get(r['reason'], r['reason'])} — {r['code']}",
                                     callback_data=f"mkm_v:{r['id']}")] for r in rows]
    await _edit(query, "🚩 گزارش‌های باز:" if rows else "گزارشِ بازی وجود ندارد. ✅", InlineKeyboardMarkup(buttons) if buttons else None)


async def cb_mod_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _mod_guard(update):
        return
    query = update.callback_query
    r = get_report(int(query.data.split(":", 1)[1]))
    if not r:
        await query.answer("پیدا نشد.", show_alert=True)
        return
    await query.answer()
    await _edit(query, _mod_text(r)[:4000], _mod_markup(r))


async def cb_mod_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _mod_guard(update):
        return
    query = update.callback_query
    r = get_report(int(query.data.split(":", 1)[1]))
    if not r or not resolve_report(r["id"], "dismissed", update.effective_user.id):
        await query.answer("این گزارش قبلاً بررسی شده.", show_alert=True)
        return
    await query.answer("گزارش رد شد (بدون اقدام).")
    await _notify(context, r["reporter_id"], "✅ گزارشت بررسی شد. ممنون از دقتت.")
    await _edit(query, _mod_text(get_report(r["id"]))[:4000], _mod_markup(get_report(r["id"])))


async def cb_mod_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _mod_guard(update):
        return
    query = update.callback_query
    r = get_report(int(query.data.split(":", 1)[1]))
    if not r or r["status"] != "open":
        await query.answer("این گزارش قبلاً بررسی شده.", show_alert=True)
        return
    l = get_listing(r["listing_id"])
    moderator_close_listing(r["listing_id"], update.effective_user.id)
    await query.answer("آگهی بسته شد.")
    await _notify(context, l["owner_id"], f"⚫️ «{l['title']}» ({l['code']}) توسط ناظر بسته شد چون با قوانینِ بازارچه نمی‌خواند. "
                                         "اگر فکر می‌کنی اشتباه شده، دوباره با اصلاحِ متن آگهی بگذار.")
    await _notify(context, r["reporter_id"], "✅ گزارشت بررسی شد و اقدام انجام شد. ممنون.")
    await _edit(query, _mod_text(get_report(r["id"]))[:4000], _mod_markup(get_report(r["id"])))


async def cb_mod_end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _mod_guard(update):
        return
    query = update.callback_query
    r = get_report(int(query.data.split(":", 1)[1]))
    chat = get_chat(r["chat_id"]) if r and r.get("chat_id") else None
    if not r or r["status"] != "open" or not chat:
        await query.answer("این مورد قابل اقدام نیست.", show_alert=True)
        return
    set_chat_status(chat["id"], "ended", only_from=("pending", "active"))
    resolve_report(r["id"], "actioned", update.effective_user.id)
    l = get_listing(chat["listing_id"])
    for uid in (chat["owner_id"], chat["responder_id"]):
        await _notify(context, uid, f"🏁 گفت‌وگو درباره‌ی «{l['title']}» ({l['code']}) توسط ناظر پایان یافت.")
    await query.answer("گفت‌وگو پایان یافت.")
    await _edit(query, _mod_text(get_report(r["id"]))[:4000], _mod_markup(get_report(r["id"])))



STATE_KEYS = ("mk_new", "mk_intent", "mk_confirm", "mchat", "mk_report")


def has_state(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return any(context.user_data.get(k) for k in STATE_KEYS)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True یعنی پیام در بازارچه مصرف شد."""
    ud = context.user_data
    text = (update.message.text or "").strip()
    if ud.get("mk_new"):
        return await _wizard_text(update, context, ud["mk_new"], text)
    if ud.get("mk_intent"):
        return await _submit_intent(update, context, text)
    if ud.get("mk_confirm"):
        return await _submit_confirm(update, context, text)
    if ud.get("mk_report"):
        return await _report_text(update, context, text)
    if ud.get("mchat"):
        return await _relay(update, context, text)
    return False


def register(app: Application) -> None:
    ensure_tables()
    add = lambda fn, pat: app.add_handler(CallbackQueryHandler(fn, pattern=pat))
    add(cb_root, r"^mk_root$")
    add(cb_browse, r"^mk_browse:(ad|request)$")
    add(cb_browse_term, r"^mkb_t:(ad|request):\d+$")
    add(cb_browse_course, r"^mkb_c:(ad|request):\d+:\d+$")
    add(cb_view, r"^mk_v:\d+$")
    add(cb_set_status, r"^mk_st:\d+:[agnc]$")
    add(cb_mine, r"^mk_mine:(ad|request)$")
    add(cb_new_start, r"^mk_new:(ad|request)$")
    add(cb_new_cancel, r"^mkn_cancel$")
    add(cb_new_type, r"^mkn_d:[a-z]+$")
    add(cb_new_term, r"^mkn_t:\d+$")
    add(cb_new_course, r"^mkn_c:\d+$")
    add(cb_new_condition, r"^mkn_s:[a-z_]+$")
    add(cb_new_confirm, r"^mkn_ok:[0-9a-f]+$")
    add(cb_contact, r"^mk_contact:\d+$")
    add(cb_intent_cancel, r"^mk_intent_cancel$")
    add(cb_accept, r"^mk_acc:\d+$")
    add(cb_reject, r"^mk_rej:\d+$")
    add(cb_chats, r"^mk_chats$")
    add(cb_listing_chats, r"^mk_lc:\d+$")
    add(cb_chat_view, r"^mk_cv:\d+$")
    add(cb_open_chat, r"^mk_open:\d+$")
    add(cb_leave, r"^mk_leave$")
    add(cb_end_chat, r"^mk_end:\d+$")
    add(cb_new_nophoto, r"^mkn_nophoto$")
    add(cb_report_info, r"^mk_report_info$")
    add(cb_report_listing, r"^mkr_l:\d+$")
    add(cb_report_message, r"^mkr_m:\d+$")
    add(cb_report_reason, r"^mkr_p:[a-z]+$")
    add(cb_report_cancel, r"^mkr_cancel$")
    add(cb_mod_list, r"^mkm_l$")
    add(cb_mod_view, r"^mkm_v:\d+$")
    add(cb_mod_dismiss, r"^mkm_d:\d+$")
    add(cb_mod_close, r"^mkm_c:\d+$")
    add(cb_mod_end_chat, r"^mkm_e:\d+$")
    app.add_handler(CommandHandler("marketreports", marketreports_command))
    # group=-3: فقط در مرحله‌ی «عکس» ویزارد عمل می‌کند و در غیر این صورت بقیه‌ی هندلرها دست‌نخورده‌اند
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler), group=-3)
