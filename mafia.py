"""
بازی Mafia — MedVerse Hospital
================================

جایگزینِ کاملِ بازیِ سؤال‌محورِ قبلیِ بخشِ «🎮 بازی». این ماژول کاملاً خودکفاست
(فقط به db.py و telegram وابسته‌ست) تا وارد کردنش توی bot.py باعثِ وابستگیِ
دوری (circular import) نشه.

معماری: دقیقاً مثلِ «گپ دوستانه»‌یِ خودِ ربات، همه‌چیز از طریقِ چتِ خصوصیِ هر
کاربر با ربات پیش می‌ره (هیچ گروهِ واقعیِ تلگرامی درکار نیست) -- بازی هم به همون
اتاق (chat_type, course_id) محدود می‌شه؛ برای «گپ دوستانه» این یعنی
chat_type='casual', course_id=None.

جریانِ بازی:
    لابی -> (میزبان شروع می‌کنه) -> شب -> گزارشِ صبحگاهی -> بحث -> رأی‌گیری
    -> حذف -> [بررسیِ برد] -> شبِ بعد ... تا برنده مشخص بشه -> گزارشِ پایانی.

هر بخشِ زمان‌بندی‌شده (شب/بحث/رأی‌گیری) هم با تایمر (job_queue) و هم با تکمیلِ
زودهنگامِ همه‌ی اکشن‌های لازم پیش می‌ره؛ هرکدوم زودتر برسه، فاز رد می‌شه.
"""

import asyncio
import random
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db as dbmod

# ============================================================
# ثابت‌ها
# ============================================================

MIN_PLAYERS = 6
MAX_PLAYERS = 15

NIGHT_SECONDS = 45
DISCUSSION_SECONDS = 180
VOTING_SECONDS = 60

ROLE_LABELS = {
    "mafia": "🔪 مافیا",
    "detective": "🔎 بازرس",
    "doctor": "🩺 پزشک کشیک",
    "citizen": "👤 کادر بیمارستان",
}

GAME_BUTTON_LABEL = "🎭 مافیا"
INVITE_JOIN_BUTTON_LABEL = "🎲 پیوستن به بازی"

# وقتی جای خالیِ لابی به این عدد یا کمتر برسه، متنِ دعوت لحنِ فوری‌تری می‌گیره.
INVITE_ALMOST_FULL_SLOTS = 2

# فاصله‌ی خیلی کوتاه بین ارسال دعوت‌های عمومی به کاربرانِ مختلف -- فقط برای
# رعایتِ محدودیتِ نرخِ ارسالِ خودِ تلگرام، نه throttleِ منطقی.
INVITE_BATCH_SLEEP = 0.05
# هر چند نفر یک‌بار وضعیتِ Lobby رو دوباره چک کنیم (لغو/شروع/پرشدن در حینِ ارسال)
INVITE_STATUS_RECHECK_EVERY = 15

# نام‌گذاریِ jobهای job_queue -- برای اینکه بشه با پایانِ زودهنگامِ یه فاز،
# تایمرِ در حالِ انتظارش رو کنسل کرد.
def _job_name(kind: str, game_id: int, round_number: int) -> str:
    return f"mafia:{kind}:{game_id}:{round_number}"


def _cancel_jobs(context: ContextTypes.DEFAULT_TYPE, kind: str, game_id: int, round_number: int) -> None:
    if context.job_queue is None:
        return
    for job in context.job_queue.get_jobs_by_name(_job_name(kind, game_id, round_number)):
        job.schedule_removal()


# ============================================================
# کمکی -- نمایش/متن
# ============================================================

def _display_name_for(user_id: int, fallback_name: str) -> str:
    users = dbmod.load_users()
    nickname = users.get(str(user_id), {}).get("nickname")
    return nickname or fallback_name


def _mention(player: dict) -> str:
    return player["display_name"]


def _room_from_update(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("active_chat_room")


async def _send(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, markup=None) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
    except Exception:
        pass


async def _broadcast(context: ContextTypes.DEFAULT_TYPE, user_ids, text: str, markup=None) -> None:
    for uid in user_ids:
        await _send(context, uid, text, markup)


def _all_recipients(game_id: int) -> list:
    """همه‌ی بازیکنان (زنده و حذف‌شده) + تماشاگرها -- برای اعلان‌های عمومی."""
    players = dbmod.get_mafia_players(game_id)
    spectators = dbmod.get_mafia_spectators(game_id)
    return [p["user_id"] for p in players] + spectators


# ============================================================
# ورودی از منوی «🎮 بازی»
# ============================================================

async def chat_game_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """وقتی کاربرِ داخلِ یه اتاقِ گپ دکمه‌ی «🎮 بازی» رو می‌زنه. فعلاً تنها بازی
    Mafia هست، پس یه منویِ تک‌گزینه‌ای نشون می‌دیم (برای اینکه بعداً بازی‌های
    دیگه هم به همین‌جا اضافه بشن، بدونِ اینکه ساختار عوض بشه)."""
    room = _room_from_update(context)
    if not room:
        return
    user_id = update.effective_user.id
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await update.message.reply_text("⛔️ دسترسیت به این گپ محدود شده.")
        return
    buttons = [[InlineKeyboardButton(GAME_BUTTON_LABEL, callback_data="mf:home")]]
    await update.message.reply_text(
        "🎮 بخشِ بازی — فعلاً فقط یک بازی داریم، ولی چه بازی‌ای:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def mf_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    room = _room_from_update(context)
    if not room:
        await query.edit_message_text("اول باید داخلِ یه اتاقِ گپ باشی.")
        return
    user_id = update.effective_user.id
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await query.edit_message_text("⛔️ دسترسیت به این گپ محدود شده.")
        return

    game = dbmod.get_active_mafia_game(room["chat_type"], room.get("course_id"))
    if not game:
        text, markup = _no_game_view()
    elif game["status"] == "lobby":
        text, markup = _lobby_view(game, user_id)
    else:
        text, markup = _running_view(game, user_id)
    await query.edit_message_text(text, reply_markup=markup)


def _no_game_view():
    text = (
        "🏥 **MedVerse Hospital**\n\n"
        "هیچ بازیِ مافیایی این لحظه در جریان نیست.\n"
        "یکی باید میزبان بشه و یه اتاق بسازه (۶ تا ۱۵ نفر لازمه)."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("➕ ساخت بازی", callback_data="mf:create")]])
    return text, markup


def _lobby_view(game: dict, viewer_id: int):
    players = dbmod.get_mafia_players(game["id"])
    host = next((p for p in players if p["is_host"]), None)
    host_name = host["display_name"] if host else "—"
    lines = [
        "🎭 **MedVerse Mafia**",
        "",
        f"👑 میزبان: {host_name}",
        f"👥 بازیکنان: {len(players)}/{MAX_PLAYERS}",
        "⏳ منتظرِ بازیکنان...",
        "",
    ]
    for p in players:
        crown = "👑 " if p["is_host"] else "• "
        lines.append(f"{crown}{p['display_name']}")
    text = "\n".join(lines)

    is_member = any(p["user_id"] == viewer_id for p in players)
    is_host = host and host["user_id"] == viewer_id
    rows = []
    if is_member:
        rows.append([InlineKeyboardButton("❌ خروج", callback_data=f"mf:leave:{game['id']}")])
    else:
        rows.append([InlineKeyboardButton("🎭 ورود به بازی", callback_data=f"mf:join:{game['id']}")])
    if is_host:
        start_row = [InlineKeyboardButton("▶️ شروعِ بازی", callback_data=f"mf:start:{game['id']}")]
        rows.append(start_row)
        rows.append([InlineKeyboardButton("🗑 لغوِ بازی", callback_data=f"mf:cancel:{game['id']}")])
    rows.append([InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="mf:home")])
    return text, InlineKeyboardMarkup(rows)


def _running_view(game: dict, viewer_id: int):
    players = dbmod.get_mafia_players(game["id"])
    is_member = any(p["user_id"] == viewer_id for p in players)
    is_spectator = viewer_id in dbmod.get_mafia_spectators(game["id"])
    phase_labels = {
        "night": "🌙 شب",
        "day": "☀️ گزارشِ صبحگاهی",
        "discussion": "💬 بحث",
        "voting": "🗳 رأی‌گیری",
    }
    phase_label = phase_labels.get(game["phase"], game["phase"])
    text = (
        "🏥 **MedVerse Hospital**\n\n"
        f"یه بازی همین الان در جریانه (دورِ {game['round_number']} — {phase_label}).\n"
        "پرونده‌ها بازن؛ به هیچ‌کس اعتماد نکن."
    )
    rows = []
    if not is_member and not is_spectator:
        rows.append([InlineKeyboardButton("👀 تماشا", callback_data=f"mf:watch:{game['id']}")])
    elif is_spectator:
        rows.append([InlineKeyboardButton("🚪 توقفِ تماشا", callback_data=f"mf:unwatch:{game['id']}")])
    rows.append([InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="mf:home")])
    return text, InlineKeyboardMarkup(rows)


# ============================================================
# لابی: ساخت / ورود / خروج / لغو / شروع
# ============================================================

async def mf_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    room = _room_from_update(context)
    if not room:
        await query.answer("اول باید داخلِ یه اتاقِ گپ باشی.", show_alert=True)
        return
    user_id = update.effective_user.id
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await query.answer("⛔️ دسترسیت به این گپ محدود شده.", show_alert=True)
        return

    existing = dbmod.get_active_mafia_game(room["chat_type"], room.get("course_id"))
    if existing:
        await query.answer("یه بازی همین الان توی این گپ در جریانه.", show_alert=True)
        text, markup = (
            _lobby_view(existing, user_id) if existing["status"] == "lobby" else _running_view(existing, user_id)
        )
        await query.edit_message_text(text, reply_markup=markup)
        return

    display = _display_name_for(user_id, update.effective_user.first_name)
    game_id = dbmod.create_mafia_game(room["chat_type"], room.get("course_id"), user_id, display)
    await query.answer("اتاق ساخته شد 🎭")
    game = dbmod.get_mafia_game(game_id)
    text, markup = _lobby_view(game, user_id)
    await query.edit_message_text(text, reply_markup=markup)

    # دعوتِ عمومی به بقیه‌ی کاربرانِ بات -- در پس‌زمینه، تا ادیتِ پیامِ بالا معطل
    # ارسالِ دعوت به (احتمالاً صدها) کاربر نمونه.
    asyncio.create_task(_send_public_game_invite(context, game_id, user_id))


async def mf_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    room = _room_from_update(context)
    user_id = update.effective_user.id
    if not room:
        await query.answer("اول باید داخلِ یه اتاقِ گپ باشی.", show_alert=True)
        return
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await query.answer("⛔️ دسترسیت به این گپ محدود شده.", show_alert=True)
        return

    display = _display_name_for(user_id, update.effective_user.first_name)
    result = dbmod.join_mafia_game(game_id, user_id, display)
    alerts = {
        "not_open": "این بازی دیگه لابی نیست یا وجود نداره.",
        "already_in": "تو از قبل توی این بازی هستی.",
        "full": "ظرفیتِ این اتاق پر شده (۱۵ نفر).",
    }
    if result in alerts:
        await query.answer(alerts[result], show_alert=True)
    else:
        await query.answer("وارد شدی 🎭")

    game = dbmod.get_mafia_game(game_id)
    if not game:
        return
    if game["status"] == "lobby":
        await _refresh_lobby_for_all(context, game_id, exclude_user_id=user_id)
    text, markup = _lobby_view(game, user_id) if game["status"] == "lobby" else _running_view(game, user_id)
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        pass


async def mf_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    user_id = update.effective_user.id
    ok = dbmod.leave_mafia_game(game_id, user_id)
    if not ok:
        await query.answer("نمی‌شه الان از این بازی خارج شد.", show_alert=True)
        return
    await query.answer("از اتاق خارج شدی.")

    game = dbmod.get_mafia_game(game_id)
    players = dbmod.get_mafia_players(game_id) if game else []
    if game and game["status"] == "lobby":
        if not players:
            # همه رفتن -- اتاقِ خالی رو لغو می‌کنیم که برای همیشه لابی نمونه.
            dbmod.cancel_mafia_game(game_id)
        elif not any(p["is_host"] for p in players):
            # میزبان همینی بود که خارج شد -- خودِ بازی رو لغو می‌کنیم (تصمیمِ ساده و
            # قابلِ‌پیش‌بینی به‌جایِ انتقالِ خودکارِ میزبانی).
            dbmod.cancel_mafia_game(game_id)
            await _broadcast(
                context, [p["user_id"] for p in players],
                "🗑 میزبان از اتاق خارج شد؛ بازی لغو شد.",
            )
        else:
            await _refresh_lobby_for_all(context, game_id)

    text, markup = _no_game_view()
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        pass


async def mf_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    user_id = update.effective_user.id
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "lobby" or game["host_id"] != user_id:
        await query.answer("این کار فقط از میزبانِ اتاقِ در حالِ لابی برمیاد.", show_alert=True)
        return
    players = dbmod.get_mafia_players(game_id)
    dbmod.cancel_mafia_game(game_id)
    await query.answer("بازی لغو شد.")
    await _broadcast(
        context, [p["user_id"] for p in players if p["user_id"] != user_id],
        "🗑 میزبان بازی رو لغو کرد.",
    )
    text, markup = _no_game_view()
    await query.edit_message_text(text, reply_markup=markup)


async def _refresh_lobby_for_all(context: ContextTypes.DEFAULT_TYPE, game_id: int, exclude_user_id=None) -> None:
    """بعدِ ورودِ یه بازیکنِ جدید، به بقیه‌ی اعضایِ لابی یه پیامِ کوتاه می‌ده تا
    لازم نباشه خودشون دستی «به‌روزرسانی» بزنن. برای جلوگیری از اسپم، فقط یه
    خطِ کوتاهه، نه کارتِ کاملِ لابی."""
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "lobby":
        return
    players = dbmod.get_mafia_players(game_id)
    names = "، ".join(p["display_name"] for p in players)
    text = f"👥 بازیکنانِ اتاق ({len(players)}/{MAX_PLAYERS}): {names}"
    for p in players:
        if p["user_id"] == exclude_user_id:
            continue
        await _send(context, p["user_id"], text)


# ============================================================
# دعوتِ عمومی -- وقتی یه Lobby جدید ساخته می‌شه، به همه‌ی کاربرانی که بات رو
# Start کرده‌ن (به‌جز سازنده) یه پیامِ دعوتِ داینامیک با دکمه‌ی پیوستن می‌ره.
# ============================================================

def _invite_text(game: dict) -> str:
    players = dbmod.get_mafia_players(game["id"])
    count = len(players)
    slots_left = max(MAX_PLAYERS - count, 0)
    if slots_left <= INVITE_ALMOST_FULL_SLOTS:
        header = "🚨 **مافیا تقریباً تکمیل شد!**"
        slot_line = f"🔥 فقط `{slots_left}` جای خالی باقی مانده!"
    else:
        header = "🎭 **یک بازی مافیا در حال تشکیل است!**"
        slot_line = f"🔥 جای خالی: `{slots_left} نفر`"
    lines = [
        header,
        "",
        f"👥 بازیکنان: `{count}/{MAX_PLAYERS}`",
        slot_line,
        "",
        "یکی از کاربران در حال تشکیل یک بازی جدید است.",
        "اگر پایه‌ای، همین الان وارد بازی شو!",
    ]
    return "\n".join(lines)


async def _send_public_game_invite(context: ContextTypes.DEFAULT_TYPE, game_id: int, host_id: int) -> None:
    """بلافاصله بعدِ ساخته‌شدنِ یه Lobby جدید صدا زده می‌شه. فقط یک‌بار برای هر
    Lobby اجرا می‌شه (از mf_create صدا زده می‌شه، نه از هر جوینِ بعدی). کاربرانی
    که از قبل عضوِ همون Lobby هستن (لحظه‌ی ساخت، یعنی فقط خودِ سازنده) و
    سازنده حذف می‌شن؛ خطای ارسال به یه کاربر مانعِ ادامه‌ی ارسال به بقیه نمی‌شه."""
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "lobby":
        return
    already_in = {p["user_id"] for p in dbmod.get_mafia_players(game_id)}
    already_in.add(host_id)
    recipient_ids = dbmod.get_started_user_ids(exclude_user_ids=already_in)
    if not recipient_ids:
        return

    text = _invite_text(game)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(INVITE_JOIN_BUTTON_LABEL, callback_data=f"mf:invjoin:{game_id}")]]
    )

    sent = 0
    for i, uid in enumerate(recipient_ids):
        if i % INVITE_STATUS_RECHECK_EVERY == 0:
            current = dbmod.get_mafia_game(game_id)
            if not current or current["status"] != "lobby":
                break
        try:
            await context.bot.send_message(chat_id=uid, text=text, reply_markup=markup)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(INVITE_BATCH_SLEEP)

    if sent:
        dbmod.record_mafia_invites_sent(game_id, sent)


_INVITE_UNAVAILABLE_TEXT = "این بازی دیگر در دسترس نیست."
_INVITE_STARTED_TEXT = "این بازی شروع شده است."
_INVITE_FULL_TEXT = "این بازی تکمیل شده است."


async def mf_invite_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """کلیک روی دکمه‌ی «🎲 پیوستن به بازی»یِ پیامِ دعوتِ عمومی. برخلافِ mf_join،
    به context.user_data['active_chat_room'] وابسته نیست چون این پیام مستقلِ از
    هر نشستِ خاصی برای کاربر ارسال شده."""
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    user_id = update.effective_user.id

    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] in ("cancelled", "finished"):
        await query.answer(_INVITE_UNAVAILABLE_TEXT, show_alert=True)
        return

    # کاربری که از قبل عضوِ همین Lobby/بازیه -- به‌جایِ افزودنِ مجدد، وضعیتِ
    # فعلی رو نشونش می‌دیم.
    if dbmod.get_mafia_player(game_id, user_id):
        await query.answer()
        text, markup = _lobby_view(game, user_id) if game["status"] == "lobby" else _running_view(game, user_id)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
        return

    if game["status"] == "running":
        await query.answer(_INVITE_STARTED_TEXT, show_alert=True)
        return

    # از اینجا به بعد status == 'lobby' هست.
    course_id = game["course_id"]
    if course_id == dbmod.CASUAL_ROOM_KEY:
        course_id = None
    if dbmod.is_user_restricted(user_id, course_id):
        await query.answer("⛔️ دسترسیت به این گپ محدود شده.", show_alert=True)
        return

    display = _display_name_for(user_id, update.effective_user.first_name)
    result = dbmod.join_mafia_game(game_id, user_id, display, via_invite=True)

    if result == "not_open":
        await query.answer(_INVITE_UNAVAILABLE_TEXT, show_alert=True)
        return
    if result == "full":
        await query.answer(_INVITE_FULL_TEXT, show_alert=True)
        return

    await query.answer("وارد شدی 🎭")
    game = dbmod.get_mafia_game(game_id)
    if not game:
        return
    await _refresh_lobby_for_all(context, game_id, exclude_user_id=user_id)
    text, markup = _lobby_view(game, user_id) if game["status"] == "lobby" else _running_view(game, user_id)
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        pass


# ============================================================
# شروعِ بازی -- توزیعِ نقش‌ها
# ============================================================

def _role_counts(n: int) -> dict:
    if n <= 8:
        mafia_count = 1
    elif n <= 12:
        mafia_count = 2
    else:
        mafia_count = 3
    detective_count = 1
    doctor_count = 1
    citizen_count = n - mafia_count - detective_count - doctor_count
    return {"mafia": mafia_count, "detective": detective_count, "doctor": doctor_count, "citizen": citizen_count}


async def mf_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    user_id = update.effective_user.id
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "lobby" or game["host_id"] != user_id:
        await query.answer("این کار فقط از میزبانِ اتاقِ در حالِ لابی برمیاد.", show_alert=True)
        return
    players = dbmod.get_mafia_players(game_id)
    if len(players) < MIN_PLAYERS:
        await query.answer(f"حداقل {MIN_PLAYERS} بازیکن لازمه (الان {len(players)} نفرید).", show_alert=True)
        return

    await query.answer("بازی شروع شد 🎭")

    counts = _role_counts(len(players))
    pool = (
        ["mafia"] * counts["mafia"]
        + ["detective"] * counts["detective"]
        + ["doctor"] * counts["doctor"]
        + ["citizen"] * counts["citizen"]
    )
    random.shuffle(pool)
    role_by_user = {p["user_id"]: role for p, role in zip(players, pool)}
    dbmod.assign_mafia_roles(game_id, role_by_user)

    try:
        await query.edit_message_text(
            "🎭 بازی شروع شد! نقشت رو توی چتِ خصوصی برات فرستادم.\n"
            "🏥 بیمارستان در سکوتِ شب فرو رفته..."
        )
    except Exception:
        pass

    mafia_members = [p for p in players if role_by_user[p["user_id"]] == "mafia"]
    mafia_names = "، ".join(m["display_name"] for m in mafia_members)
    for p in players:
        role = role_by_user[p["user_id"]]
        text = _role_intro_text(role, mafia_names)
        await _send(context, p["user_id"], text)

    await begin_night(game_id, context)


def _role_intro_text(role: str, mafia_names: str) -> str:
    header = (
        "🏥 **MedVerse Hospital**\n\n"
        "بیمارستان در سکوتِ شب فرو رفته...\n"
        "اما مشخص نیست چه کسی واقعاً برای نجات اومده و چه کسی برای خرابکاری.\n\n"
        "از این لحظه، به هیچ‌کس اعتماد نکن.\n\n"
    )
    if role == "mafia":
        team = f"\n🔪 هم‌تیمی‌هات: {mafia_names}" if mafia_names else ""
        return header + f"نقشِ تو: {ROLE_LABELS['mafia']}\nهر شب با تیمت یه نفر رو هدف می‌گیرید.{team}"
    if role == "detective":
        return header + f"نقشِ تو: {ROLE_LABELS['detective']}\nهر شب می‌تونی هویتِ یه نفر رو بررسی کنی."
    if role == "doctor":
        return header + f"نقشِ تو: {ROLE_LABELS['doctor']}\nهر شب می‌تونی یه نفر رو برای نجات انتخاب کنی."
    return header + f"نقشِ تو: {ROLE_LABELS['citizen']}\nقدرتِ شبانه نداری؛ با بحث و رأی‌گیری کمک کن مافیا پیدا بشه."


# ============================================================
# فازِ شب
# ============================================================

def _target_buttons(game_id: int, action: str, candidates: list, allow_skip: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(p["display_name"], callback_data=f"mfn:{game_id}:{action}:{p['user_id']}")]
        for p in candidates
    ]
    if allow_skip:
        rows.append([InlineKeyboardButton("⏭ فعلاً کاری نمی‌کنم", callback_data=f"mfn:{game_id}:{action}:skip")])
    return InlineKeyboardMarkup(rows)


async def begin_night(game_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "running":
        return
    round_number = game["round_number"]
    dbmod.set_mafia_phase(game_id, "night", round_number)

    players = dbmod.get_mafia_players(game_id, alive_only=True)
    alive_by_role = {"mafia": [], "detective": [], "doctor": [], "citizen": []}
    for p in players:
        alive_by_role.setdefault(p["role"], []).append(p)

    night_label = "شبِ اول" if round_number == 1 else f"شبِ {round_number}"
    for m in alive_by_role["mafia"]:
        others = [p for p in players if p["role"] != "mafia"]
        text = f"🌙 **{night_label}**\n\nبیمارستان به خواب رفته...\nنوبتِ توئه: کی رو هدف می‌گیرید؟"
        await _send(context, m["user_id"], text, _target_buttons(game_id, "kill", others))
    for d in alive_by_role["doctor"]:
        text = f"🌙 **{night_label}**\n\n🩺 نوبتِ توئه: امشب کی رو نجات می‌دی؟"
        await _send(context, d["user_id"], text, _target_buttons(game_id, "save", players))
    for insp in alive_by_role["detective"]:
        others = [p for p in players if p["user_id"] != insp["user_id"]]
        text = f"🌙 **{night_label}**\n\n🔎 نوبتِ توئه: هویتِ کی رو بررسی می‌کنی؟"
        await _send(context, insp["user_id"], text, _target_buttons(game_id, "inspect", others))

    if context.job_queue is not None:
        context.job_queue.run_once(
            _night_timeout_job, when=NIGHT_SECONDS,
            data={"game_id": game_id, "round": round_number},
            name=_job_name("night", game_id, round_number),
        )


async def _night_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    await resolve_night(d["game_id"], d["round"], context)


async def mf_night_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, game_id_str, action, target_str = query.data.split(":", 3)
    game_id = int(game_id_str)
    user_id = update.effective_user.id

    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "running" or game["phase"] != "night":
        await query.answer("این اقدام دیگه معتبر نیست.", show_alert=True)
        return
    actor = dbmod.get_mafia_player(game_id, user_id)
    if not actor or not actor["alive"]:
        await query.answer("تو دیگه بخشی از این پرونده نیستی.", show_alert=True)
        return
    expected_role = {"kill": "mafia", "save": "doctor", "inspect": "detective"}.get(action)
    if actor["role"] != expected_role:
        await query.answer("این کارِ تو نیست.", show_alert=True)
        return

    target_id = None if target_str == "skip" else int(target_str)
    round_number = game["round_number"]
    dbmod.record_mafia_night_action(game_id, round_number, user_id, action, target_id)

    if target_id is None:
        await query.answer("باشه، امشب کاری نمی‌کنی.")
        try:
            await query.edit_message_text("⏭ امشب کاری نکردی.")
        except Exception:
            pass
    else:
        await query.answer("ثبت شد ✅")
        target = dbmod.get_mafia_player(game_id, target_id)
        try:
            await query.edit_message_text(f"✅ انتخابت ثبت شد: {target['display_name'] if target else '—'}")
        except Exception:
            pass
        if action == "inspect" and target:
            verdict = "🔪 مشکوکه و احتمالاً مافیائه." if target["role"] == "mafia" else "✅ ظاهراً پاکه."
            await _send(context, user_id, f"🔎 نتیجه‌ی بررسیِ {target['display_name']}:\n{verdict}")

    await _maybe_resolve_night_early(game_id, round_number, context)


async def _maybe_resolve_night_early(game_id: int, round_number: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    players = dbmod.get_mafia_players(game_id, alive_only=True)
    needed_roles = {p["role"] for p in players if p["role"] in ("mafia", "doctor", "detective")}
    actions = dbmod.get_mafia_night_actions(game_id, round_number)
    actors_by_role = {}
    for a in actions:
        actor = dbmod.get_mafia_player(game_id, a["actor_id"])
        if actor:
            actors_by_role.setdefault(actor["role"], set()).add(a["actor_id"])

    for role in needed_roles:
        needed_ids = {p["user_id"] for p in players if p["role"] == role}
        acted_ids = actors_by_role.get(role, set())
        if needed_ids - acted_ids:
            return  # هنوز یه نفر از این نقش اقدام نکرده

    _cancel_jobs(context, "night", game_id, round_number)
    await resolve_night(game_id, round_number, context)


async def resolve_night(game_id: int, round_number: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "running" or game["phase"] != "night" or game["round_number"] != round_number:
        return  # این فاز از قبل رد شده (هم تایمر هم تکمیلِ زودهنگام همزمان صدا زده شدن)
    _cancel_jobs(context, "night", game_id, round_number)

    kill_actions = [a for a in dbmod.get_mafia_night_actions(game_id, round_number, "kill") if a["target_id"]]
    save_actions = [a for a in dbmod.get_mafia_night_actions(game_id, round_number, "save") if a["target_id"]]

    killed_id = None
    if kill_actions:
        tally = {}
        for a in kill_actions:
            tally[a["target_id"]] = tally.get(a["target_id"], 0) + 1
        top = max(tally.values())
        winners = [tid for tid, n in tally.items() if n == top]
        killed_id = random.choice(winners)

    saved_ids = {a["target_id"] for a in save_actions}
    rescued = killed_id is not None and killed_id in saved_ids

    if killed_id and not rescued:
        dbmod.eliminate_mafia_player(game_id, killed_id, round_number, "night")

    dbmod.set_mafia_phase(game_id, "day", round_number)
    await begin_day(game_id, context, killed_id if (killed_id and not rescued) else None, rescued)


# ============================================================
# فازِ روز: گزارشِ صبحگاهی -> بحث -> رأی‌گیری
# ============================================================

async def begin_day(game_id: int, context: ContextTypes.DEFAULT_TYPE, killed_id, rescued: bool) -> None:
    game = dbmod.get_mafia_game(game_id)
    recipients = _all_recipients(game_id)

    if killed_id:
        victim = dbmod.get_mafia_player(game_id, killed_id)
        role_label = ROLE_LABELS.get(victim["role"], victim["role"]) if victim else ""
        text = (
            "☀️ **گزارشِ صبحگاهی**\n\n"
            "شبِ گذشته اتفاقِ مشکوکی در بیمارستان رخ داده...\n\n"
            f"متأسفانه **{victim['display_name']}** دیگه نمی‌تونه به ادامه‌ی تحقیقات کمک کنه.\n\n"
            f"🎭 نقش: {role_label}"
        )
    elif rescued:
        text = (
            "🩺 **گزارشِ اورژانس**\n\n"
            "یه تلاش برای حذفِ یکی از اعضایِ بیمارستان خنثی شد.\n\n"
            "☀️ همه‌ی بازیکنان هنوز توی بازی هستن."
        )
    else:
        text = (
            "☀️ **گزارشِ صبحگاهی**\n\n"
            "عجیبه، ولی این شب آروم گذشت.\n\n"
            "☀️ همه‌ی بازیکنان هنوز توی بازی هستن."
        )
    await _broadcast(context, recipients, text)

    outcome = _check_win(game_id)
    if outcome:
        await end_game(game_id, context, outcome)
        return

    await begin_discussion(game_id, context)


async def begin_discussion(game_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = dbmod.get_mafia_game(game_id)
    round_number = game["round_number"]
    dbmod.set_mafia_phase(game_id, "discussion", round_number)
    minutes = DISCUSSION_SECONDS // 60
    text = (
        "💬 **جلسه‌ی اضطراری**\n\n"
        "حالا وقتِ بحثه.\n\n"
        "چه کسی مشکوکه؟\nبه چه کسی اعتماد داری؟\n\n"
        f"⏱ زمانِ بحث: {minutes} دقیقه"
    )
    await _broadcast(context, _all_recipients(game_id), text)

    if context.job_queue is not None:
        context.job_queue.run_once(
            _discussion_timeout_job, when=DISCUSSION_SECONDS,
            data={"game_id": game_id, "round": round_number},
            name=_job_name("disc", game_id, round_number),
        )


async def _discussion_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    game = dbmod.get_mafia_game(d["game_id"])
    if not game or game["phase"] != "discussion" or game["round_number"] != d["round"]:
        return
    await begin_voting(d["game_id"], context)


async def begin_voting(game_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = dbmod.get_mafia_game(game_id)
    round_number = game["round_number"]
    _cancel_jobs(context, "disc", game_id, round_number)
    dbmod.set_mafia_phase(game_id, "voting", round_number)

    alive = dbmod.get_mafia_players(game_id, alive_only=True)
    await _broadcast(
        context, _all_recipients(game_id),
        "🗳 **رأی‌گیری شروع شد**\n\nرأیت کاملاً مخفیه. کی رو مشکوک می‌دونی؟",
    )
    for voter in alive:
        candidates = [p for p in alive if p["user_id"] != voter["user_id"]]
        rows = [
            [InlineKeyboardButton(p["display_name"], callback_data=f"mfv:{game_id}:{p['user_id']}")]
            for p in candidates
        ]
        rows.append([InlineKeyboardButton("🤍 امتناع", callback_data=f"mfv:{game_id}:skip")])
        await _send(context, voter["user_id"], "🗳 رأیت رو انتخاب کن:", InlineKeyboardMarkup(rows))

    if context.job_queue is not None:
        context.job_queue.run_once(
            _voting_timeout_job, when=VOTING_SECONDS,
            data={"game_id": game_id, "round": round_number},
            name=_job_name("vote", game_id, round_number),
        )


async def _voting_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    await resolve_voting(d["game_id"], d["round"], context)


async def mf_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, game_id_str, target_str = query.data.split(":", 2)
    game_id = int(game_id_str)
    user_id = update.effective_user.id

    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "running" or game["phase"] != "voting":
        await query.answer("رأی‌گیری این دور دیگه باز نیست.", show_alert=True)
        return
    voter = dbmod.get_mafia_player(game_id, user_id)
    if not voter or not voter["alive"]:
        await query.answer("بازیکنانِ حذف‌شده نمی‌تونن رأی بدن.", show_alert=True)
        return

    round_number = game["round_number"]
    if target_str == "skip":
        await query.answer("رأیِ سفید ثبت نمی‌شه؛ می‌تونی هر لحظه یکی رو انتخاب کنی.")
        try:
            await query.edit_message_text("🤍 فعلاً امتناع کردی.")
        except Exception:
            pass
        return

    target_id = int(target_str)
    target = dbmod.get_mafia_player(game_id, target_id)
    if not target or not target["alive"]:
        await query.answer("این بازیکن دیگه توی بازی نیست.", show_alert=True)
        return

    dbmod.record_mafia_vote(game_id, round_number, user_id, target_id)
    await query.answer("رأیت ثبت شد ✅")
    try:
        await query.edit_message_text(f"✅ رأی دادی به: {target['display_name']}")
    except Exception:
        pass

    alive = dbmod.get_mafia_players(game_id, alive_only=True)
    votes = dbmod.get_mafia_votes(game_id, round_number)
    if len({v["voter_id"] for v in votes}) >= len(alive):
        _cancel_jobs(context, "vote", game_id, round_number)
        await resolve_voting(game_id, round_number, context)


async def resolve_voting(game_id: int, round_number: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "running" or game["phase"] != "voting" or game["round_number"] != round_number:
        return
    _cancel_jobs(context, "vote", game_id, round_number)

    votes = dbmod.get_mafia_votes(game_id, round_number)
    tally = {}
    for v in votes:
        tally[v["target_id"]] = tally.get(v["target_id"], 0) + 1

    lines = ["🗳 **نتیجه‌یِ رأی‌گیری**", ""]
    eliminated_id = None
    if tally:
        ranked = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
        top_count = ranked[0][1]
        top_targets = [tid for tid, n in ranked if n == top_count]
        for tid, n in ranked:
            p = dbmod.get_mafia_player(game_id, tid)
            name = p["display_name"] if p else "—"
            lines.append(f"{name} — {n} رأی")
        if len(top_targets) == 1:
            eliminated_id = top_targets[0]
    else:
        lines.append("هیچ‌کس رأی نداد.")

    if eliminated_id:
        dbmod.eliminate_mafia_player(game_id, eliminated_id, round_number, "vote")
        victim = dbmod.get_mafia_player(game_id, eliminated_id)
        role_label = ROLE_LABELS.get(victim["role"], victim["role"]) if victim else ""
        lines.append("")
        lines.append(f"🔴 {victim['display_name']} از بازی خارج شد.")
        lines.append("")
        lines.append(f"🎭 نقش: {role_label}")
    else:
        lines.append("")
        lines.append("🤝 رأی‌ها مساوی شد یا کسی رأی قاطع نگرفت؛ امشب کسی حذف نشد.")

    await _broadcast(context, _all_recipients(game_id), "\n".join(lines))

    outcome = _check_win(game_id)
    if outcome:
        await end_game(game_id, context, outcome)
        return

    # فازِ بعدی «شبِ دورِ بعد»ه -- round_number رو هم همینجا جلو می‌بریم؛
    # begin_night خودش round_number تازه رو از دیتابیس می‌خونه.
    dbmod.set_mafia_phase(game_id, "night", round_number + 1)
    await begin_night(game_id, context)


# ============================================================
# شرطِ برد / پایانِ بازی
# ============================================================

def _check_win(game_id: int):
    players = dbmod.get_mafia_players(game_id)
    alive = [p for p in players if p["alive"]]
    mafia_alive = [p for p in alive if p["role"] == "mafia"]
    city_alive = [p for p in alive if p["role"] != "mafia"]
    if not mafia_alive:
        return "city"
    if len(mafia_alive) >= len(city_alive):
        return "mafia"
    return None


async def end_game(game_id: int, context: ContextTypes.DEFAULT_TYPE, winner: str) -> None:
    game = dbmod.get_mafia_game(game_id)
    players = dbmod.get_mafia_players(game_id)

    winning_team = "🔴 تیمِ سایه" if winner == "mafia" else "🟢 کادرِ بیمارستان"
    winning_players = [p for p in players if (p["role"] == "mafia") == (winner == "mafia")]
    mvp = random.choice(winning_players) if winning_players else random.choice(players)
    dbmod.finish_mafia_game(game_id, winner, mvp["user_id"])

    duration_minutes = "—"
    if game.get("started_at"):
        try:
            started = datetime.fromisoformat(game["started_at"])
            duration_minutes = str(max(1, int((datetime.now() - started).total_seconds() // 60)))
        except Exception:
            pass

    by_role = {"mafia": [], "detective": [], "doctor": [], "citizen": []}
    for p in players:
        by_role.setdefault(p["role"], []).append(p["display_name"])

    votes_total = 0
    for r in range(1, game["round_number"] + 1):
        votes_total += len(dbmod.get_mafia_votes(game_id, r))

    lines = [
        "🏁 **پرونده بسته شد**",
        "",
        f"{winning_team} پیروز شد.",
        "",
        f"👥 بازیکنان: {len(players)}",
        f"⏱ مدتِ بازی: {duration_minutes} دقیقه",
        f"🌀 تعدادِ دورها: {game['round_number']}",
        f"🗳 تعدادِ رأی‌ها: {votes_total}",
        "",
        f"🔪 مافیا:\n" + ("\n".join(by_role['mafia']) if by_role['mafia'] else "—"),
        "",
        f"🔎 بازرس:\n" + ("\n".join(by_role['detective']) if by_role['detective'] else "—"),
        "",
        f"🩺 پزشک:\n" + ("\n".join(by_role['doctor']) if by_role['doctor'] else "—"),
        "",
        f"👤 کادرِ بیمارستان:\n" + ("\n".join(by_role['citizen']) if by_role['citizen'] else "—"),
        "",
        f"⭐ بازیکنِ برتر: {mvp['display_name']}",
        "",
        "پرونده‌ی این شب در آرشیوِ MedVerse ثبت شد.",
    ]
    await _broadcast(context, _all_recipients(game_id), "\n".join(lines))

    dbmod.log_mafia_game_summary(
        game_id, game["chat_type"], game["course_id"],
        {
            "winner": winner,
            "players": len(players),
            "round_number": game["round_number"],
            "votes_total": votes_total,
            "mvp": mvp["display_name"],
            "roles": {role: names for role, names in by_role.items()},
        },
    )


# ============================================================
# قابلیتِ تماشا (Spectator)
# ============================================================

async def mf_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    user_id = update.effective_user.id
    game = dbmod.get_mafia_game(game_id)
    if not game or game["status"] != "running":
        await query.answer("این بازی الان قابلِ تماشا نیست.", show_alert=True)
        return
    dbmod.add_mafia_spectator(game_id, user_id)
    await query.answer("👀 به‌عنوانِ تماشاگر اضافه شدی.")
    text, markup = _running_view(game, user_id)
    await query.edit_message_text(text, reply_markup=markup)


async def mf_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    game_id = int(query.data.split(":", 2)[2])
    user_id = update.effective_user.id
    dbmod.remove_mafia_spectator(game_id, user_id)
    await query.answer("دیگه تماشاگر نیستی.")
    game = dbmod.get_mafia_game(game_id)
    if game:
        text, markup = _no_game_view() if game["status"] not in ("lobby", "running") else _running_view(game, user_id)
        await query.edit_message_text(text, reply_markup=markup)


# ============================================================
# هوکِ گپِ عمومی -- برای جلوگیری از صحبتِ بازیکنانِ حذف‌شده در حینِ بازی و
# سکوتِ اجباری در فازِ شب/رأی‌گیری (طبقِ فضاسازیِ روایی). فراخوانده می‌شه از
# bot.handle_chat_room_text قبل از فن‌اوتِ پیام.
# ============================================================

def chat_block_reason(chat_type: str, course_id, user_id: int) -> str:
    """اگه پیامِ کاربر باید بلاک بشه، متنِ دلیل رو برمی‌گردونه؛ وگرنه None."""
    game = dbmod.get_active_mafia_game(chat_type, course_id)
    if not game or game["status"] != "running":
        return None
    player = dbmod.get_mafia_player(game["id"], user_id)
    if player and not player["alive"]:
        return "👻 دیگه از بازی خارج شدی؛ می‌تونی تماشا کنی ولی نمی‌تونی توی گپ صحبت کنی."
    if game["phase"] in ("night", "voting"):
        if player or user_id in dbmod.get_mafia_spectators(game["id"]):
            return "🌙 الان نوبتِ بحث نیست -- توی فازِ شب/رأی‌گیری، گپ ساکته."
    return None
