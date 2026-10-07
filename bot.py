"""
ربات تلگرامی MedVerse -- اشتراک‌گذاری جزوه، پاورپوینت و نمونه‌سوال درس‌های پزشکی

ساختار داده:
{
  "فیزیوپات ۱": {
      "پاتولوژی": {
          "guide": "متن راهنمای مطالعه (اختیاری)",
          "has_practical": false,
          "categories": {
              "📖 رفرنس و پاور": {"files": [ {type, file_id/url, caption, downloads}, ... ]},
              "📝 جزوه": {"files": [...]},
              "📌 خلاصه": {"files": [...]},
              "⭐️ نکات امتحانی": {"files": [...]},
              "💡 پیشنهاد مطالعه": {"files": [...]},
              "📘 Study_Guid": {"files": [...]},
              "❓ نمونه‌سوالات": {"files": [...]},
              "🩺 تجربه ارسالی دانشجویان": {"files": [...]},
              "🧪 عملی": {"files": [...]},   # فقط برای درس‌هایی که has_practical=true
              "📂 فایل‌های تکمیلی": {"files": [...]},
              "📋 طرح درس": {"files": [...]}   # فقط برای درس‌هایی که has_lesson_plan=true
          }
      },
      ...
  },
  "فیزیوپات ۲": { ... همون ساختار ... },
  "فیزیوپات ۳": { ... همون ساختار ... }
}
سه برنامه‌ی پشتیبانی‌شده: PHYSIOPATH1_NAME, PHYSIOPATH2_NAME, PHYSIOPATH3_NAME.
فیزیوپات ۲ و ۳ با دروس خالی ساخته می‌شن؛ ادمین با /addcourse بهشون درس اضافه می‌کنه.
فایل‌ها خودشون روی سرور تلگرام می‌مونن؛ فقط یه JSON کوچیک نگه می‌داریم.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx
from dotenv import load_dotenv
import db as dbmod
import backup as backupmod
import ai_experience
import mafia
import payment
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, TelegramError

# ---------------------------------------------------------
# تنظیمات حساس (Secrets) و پیکربندیِ محیط
# همه‌شون از Environment Variables خونده می‌شن، نه از داخل کد.
# محلی (ویندوز): یه فایل .env کنار bot.py بساز (نمونه‌ش تو .env.example هست) --
#   load_dotenv() پایین خودکار می‌خوندش.
# روی سرور Cloud: این متغیرها رو مستقیماً در Environment Variables /
#   systemd EnvironmentFile سرویس تنظیم کن؛ لازم نیست فایل .env اونجا باشه.
# ---------------------------------------------------------
load_dotenv()


def _fatal_env_error(message: str) -> "None":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).critical(message)
    sys.exit(1)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    _fatal_env_error(
        "BOT_TOKEN تنظیم نشده. یه Environment Variable به اسم BOT_TOKEN بساز و "
        "توکنِ رباتت (از @BotFather) رو داخلش بذار. برای اجرای محلی می‌تونی یه فایل "
        ".env کنار bot.py بسازی (نگاه کن به .env.example)."
    )

_admin_ids_raw = os.environ.get("ADMIN_IDS", "").strip()
if not _admin_ids_raw:
    _fatal_env_error(
        "ADMIN_IDS تنظیم نشده. آیدی عددیِ تلگرامِ سوپرادمین(ها) رو با کاما جدا کن، "
        "مثلاً: ADMIN_IDS=1864207596 یا ADMIN_IDS=1864207596,123456789"
    )
try:
    ADMIN_IDS = [int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()]
except ValueError:
    _fatal_env_error("ADMIN_IDS باید فقط شامل عددهای صحیح جدا شده با کاما باشه.")
if not ADMIN_IDS:
    _fatal_env_error("ADMIN_IDS خالیه؛ حداقل یک آیدی عددی لازمه.")

# PROXY_URL اختیاریه: فقط وقتی لازم می‌شه که سرور نتونه مستقیم به تلگرام وصل بشه
# (مثلاً وقتی ربات روی سیستمی داخل ایران اجرا بشه). روی سرورهای Cloud خارج از ایران
# معمولاً به پروکسی نیازی نیست و این متغیر رو می‌تونی خالی بذاری/تنظیم نکنی.
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
CONNECTION_TEST_TIMEOUT = 5

# مسیر مرکزیِ داده‌های Runtime (اختیاری). اگه ست بشه (مثلاً روی Railway:
# MEDVERSE_DATA_DIR=/data که به یه Volume وصله)، تمام فایل‌های JSON که هنوز
# در Runtime نوشته می‌شن (پایین‌تر) داخل همین پوشه ذخیره می‌شن تا با
# Restart/Redeploy از بین نرن. اگه ست نشه، دقیقاً رفتار قبلی حفظ می‌شه:
# همون پوشه‌ی کنار bot.py (سازگار با اجرای محلی/VPS فعلی).
MEDVERSE_DATA_DIR = os.environ.get("MEDVERSE_DATA_DIR", "").strip()
if MEDVERSE_DATA_DIR:
    os.makedirs(MEDVERSE_DATA_DIR, exist_ok=True)


def _data_path(filename: str) -> str:
    """مسیر یه فایل Runtime رو برمی‌گردونه: اگه MEDVERSE_DATA_DIR ست باشه، داخل
    اون پوشه؛ وگرنه همون اسم فایل (رفتار قبلی، کنار bot.py)."""
    if MEDVERSE_DATA_DIR:
        return os.path.join(MEDVERSE_DATA_DIR, filename)
    return filename


# این چهار فایل قبلاً محل ذخیره‌ی مستقیم داده بودن؛ از فاز ۱ (SQLite) به بعد
# فقط برای «مهاجرت یک‌باره‌ی اولیه» استفاده می‌شن (نگاه کن به db.migrate_json_to_sqlite
# در main). خودِ ربات دیگه مستقیم این‌ها رو نمی‌خونه/نمی‌نویسه، برای همین عمداً به
# MEDVERSE_DATA_DIR منتقل نشدن -- فایل‌های ثابتِ کنار کدن، نه داده‌ی Runtime.
DATA_FILE = "courses_data.json"
SUBSCRIBERS_FILE = "subscribers.json"
USERS_FILE = "users.json"

# این‌ها هنوز واقعاً در Runtime خونده/نوشته می‌شن (نگاه کن به load_*/save_* پایین‌تر)
# پس باید داخل MEDVERSE_DATA_DIR (وقتی ست شده) باشن تا روی Railway پایدار بمونن.
PENDING_FILE = _data_path("pending_submissions.json")
ANALYTICS_FILE = _data_path("analytics.json")
REQUESTS_FILE = _data_path("requests.json")

# نشان‌های کیفیت که ادمین می‌تونه به فایل‌های منتخب بده
QUALITY_BADGES = {
    "top": "🏆",
    "star": "⭐️",
}

# انواع رویدادهای ثابت هفتگی. مقدارِ ذخیره‌شده انگلیسی و پایدار است؛
# برچسب/ورودی فارسی فقط در UI استفاده می‌شود.
FIXED_EVENT_TYPES = {
    "general": "🗓 عمومی",
    "food": "🍱 رزرو غذا",
    "cultural": "🌱 دوشنبه فرهنگی",
}

def normalize_fixed_event_type(raw: str) -> str:
    value = (raw or "").strip().lower()
    aliases = {
        "general": "general", "عمومی": "general", "رویداد عمومی": "general",
        "class": "general", "کلاس": "general",
        "food": "food", "غذا": "food", "رزرو غذا": "food",
        "cultural": "cultural", "فرهنگی": "cultural", "دوشنبه فرهنگی": "cultural",
    }
    return aliases.get(value, "general")

# ساعت‌هایی که ربات «فعال» حساب می‌شه (فقط وقتی خود برنامه روشنه معنا داره)
ACTIVE_HOURS_START = 0
ACTIVE_HOURS_END = 24

OFFLINE_MESSAGE = (
    "🌙 همراه MedVerse فعلاً در دسترس نیست.\n\n"
    "ربات در ساعات مشخصی فعال می‌شه. لطفاً بعداً دوباره تلاش کن.\n\n"
    "در این فاصله می‌تونی از مطالب کانال استفاده کنی. 🩺"
)

CHANNEL_USERNAME = "@Medversegoums"
CHANNEL_LINK = "https://t.me/Medversegoums"

MENU_COURSES = "📚 دروس"
MENU_NOTIFY = "🔔 اطلاع‌رسانی"
MENU_ABOUT = "ℹ️ درباره"
MENU_PARTICIPATE = "📩 مشارکت دانشجویان"
MENU_SCHEDULE = "📅 برنامه شخصی"
MENU_STUDY_GROUP = "👥 مطالعه گروهی"
MENU_CHAT = "💬 گپ دانشجویی"
MENU_SUPPORT = "⭐ حمایت مالی"

# دلایل استاندارد گزارش پیام در گپ دانشجویی
CHAT_REPORT_REASONS = [
    ("inappropriate", "🚫 محتوای نامناسب"),
    ("insult", "🤬 توهین"),
    ("harassment", "😠 مزاحمت"),
    ("spam", "📛 اسپم"),
    ("misinfo", "❗️ اطلاعات نادرست"),
    ("other", "🔹 سایر"),
]
CHAT_EXIT_BUTTON = "🚪 خروج از گپ"
CHAT_IDENTITY_TOGGLE_BUTTON = "🔄 تغییر هویت"
CHAT_RANDOM_TOPIC_BUTTON = "🎲 موضوع تصادفی"
CHAT_POLL_BUTTON = "📊 نظرسنجی"
CHAT_GAME_BUTTON = "🎮 بازی"

# حداکثر طول قطعه‌ی نقل‌قول‌شده از پیام اصلی وقتی یه پیام ریپلای می‌شه
CHAT_REPLY_SNIPPET_LEN = 60

# لیستِ موضوع‌های شروع‌گفتگوی گپ دوستانه -- غیردرسی/پزشکی/طنز، برای دکمه‌ی
# «🎲 موضوع تصادفی». هر وقت خواستید می‌تونید اضافه/کم کنید.
CHAT_RANDOM_TOPICS = [
    "به‌نظرت بهترین فیلم پزشکی که تا حالا دیدی چیه؟",
    "اگه می‌تونستی یه بیماری رو از بین ببری، کدوم رو انتخاب می‌کردی؟",
    "بهترین کتاب غیردرسی که خوندی چی بود؟",
    "یه خاطره‌ی خنده‌دار از دوران دانشجویی‌ات تعریف کن.",
    "به‌نظرت پزشکیِ ۲۰ سال دیگه چه شکلیه؟",
    "اگه یه روز پزشک نباشی، چی‌کار می‌کنی؟",
    "به‌نظرت سخت‌ترین بخش پزشکی چیه؟",
    "اگه می‌تونستی با یه پزشک تاریخ شام بخوری، کی رو انتخاب می‌کردی؟",
    "به‌نظرت چرا بعضی‌ها از پزشکی منصرف می‌شن؟",
    "یه عادت خوب که بهت کمک کرده رو به اشتراک بذار.",
    "بامزه‌ترین اتفاقی که سرِ کلاس افتاده چی بوده؟",
    "اگه یه روز تعطیل بدون درس‌خوندن داشته باشی، چی‌کار می‌کنی؟",
    "کدوم درس رو بیشتر از همه دوست داری و چرا؟",
    "بهترین نصیحتی که یه استاد بهت کرده چی بوده؟",
    "اگه بشه یه چیز رو توی برنامه‌ی درسی عوض کنی، چیه؟",
    "یه اپلیکیشن یا ابزار مفید که استفاده می‌کنی رو معرفی کن.",
    "دلت برای چه غذایی توی خونه تنگ می‌شه؟",
    "بهترین راهی که برای استرسِ امتحان پیدا کردی چیه؟",
    "اگه قرار بود یه تخصص دیگه انتخاب کنی، کدوم بود؟",
    "یه چیز جالب که این ترم یاد گرفتی رو بگو.",
]

STUDY_ROOM_LINK = "https://app.darsita.ir/study-rooms/join/a9l7ev7n"

# فایل ثابت «راهنمای استفاده از بات» -- خودت این فایل رو کنار bot.py روی سرور می‌ذاری
# (هر فرمتی: PDF/عکس/ویدیو و ...)؛ به‌ندرت عوض می‌شه، پس نیازی به مدیریت از تلگرام نیست.
USAGE_GUIDE_FILE = "MedVerse_Guide.html"

CLASS_SCHEDULE_FILE = _data_path("class_schedule.json")
CLASS_EXCEPTIONS_FILE = _data_path("class_exceptions.json")
FIXED_EVENTS_FILE = _data_path("fixed_events.json")
PERSONAL_SCHEDULE_FILE = _data_path("personal_schedule.json")
REMINDER_LOG_FILE = _data_path("reminder_log.json")
REMINDER_LEAD_MINUTES = 10  # چند دقیقه قبل از شروع کلاس رسمی یادآوری بفرسته
REMINDER_CATCHUP_MINUTES = 10  # اگر Job/سرور کمی عقب افتاد، یادآوری تا این مدت از دست نره

# منطقه‌ی زمانیِ صریح برای کل سیستم برنامه‌کلاسی/یادآوری. عمداً هاردکد شده تا
# مستقل از تنظیمِ timezone سرور (مثلاً UTC روی Railway) باشه؛ هر جا لازم بود
# «الان ساعت چنده / امروز چه روزیه» بدونیم، باید از now_tehran() استفاده کنیم،
# نه datetime.now() خام. اگه Railway در آینده timezone سیستم رو عوض کنه، این
# خط تغییری در رفتار بات ایجاد نمی‌کنه چون دیگه به ساعتِ سیستم متکی نیستیم.
TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def now_tehran() -> datetime:
    """زمانِ فعلی به وقتِ ایران (timezone-aware)، مستقل از timezone سیستم‌عاملِ سرور.
    فقط همین تابع باید برای محاسباتِ «الان»، «امروز» و weekday در بخشِ
    برنامه‌کلاسی/یادآوری/برنامه‌مطالعه‌ی شخصی استفاده بشه."""
    return datetime.now(TEHRAN_TZ)

DAYS_OF_WEEK = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
# نگاشت weekday() پایتون (دوشنبه=0 ... یکشنبه=6) به روزهای هفته‌ی فارسی (شنبه اول هفته)
PY_WEEKDAY_TO_FA = {5: "شنبه", 6: "یکشنبه", 0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه"}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# مراحل فرم «ارسال تجربه امتحان»: (کلید ذخیره، متن سوال)
EXAM_FIELDS = [
    ("teacher", "👨‍⚕️ اسم استاد؟"),
    ("exam_date", "📅 تاریخ امتحان؟"),
    ("source", "📚 منبع مطالعه‌ات چی بود؟"),
    ("level", "🎯 سطح امتحان چطور بود؟ (آسون/متوسط/سخت + توضیح کوتاه)"),
    ("notes", "📝 نکات مهمی که باید بدونن؟"),
    ("advice", "💡 پیشنهادت برای دانشجوهای بعدی؟"),
]

# دسته‌بندی‌هایی که دانشجو موقع ارسال فایل ازشون انتخاب می‌کنه
# (برچسب نمایشی، ایندکس متناظر توی لیست CATEGORIES)
SUBMIT_CATEGORIES = [
    ("📝 خلاصه شخصی", 2),
    ("❓ نمونه سوال", 6),
    ("⭐️ نکات مطالعه", 3),
    ("📂 فایل مفید", 9),
]

PHYSIOPATH1_NAME = "فیزیوپات ۱"
PHYSIOPATH2_NAME = "فیزیوپات ۲"
PHYSIOPATH3_NAME = "فیزیوپات ۳"
DEFAULT_PROGRAM_NAMES = [PHYSIOPATH1_NAME, PHYSIOPATH2_NAME, PHYSIOPATH3_NAME]
# فقط برای مهاجرتِ یک‌باره‌ی اولیه استفاده می‌شه (مثل DATA_FILE/SUBSCRIBERS_FILE/USERS_FILE
# بالاتر) -- فایل ثابتِ کنار کد است، نه داده‌ی Runtime، پس عمداً به MEDVERSE_DATA_DIR منتقل نشده.
PROGRAMS_FILE = "programs.json"

PHYSIOPATH1_COURSES = [
    "پاتولوژی",
    "فارماکولوژی",
    "ژنتیک",
    "تغذیه",
    "جراحی",
    "روان",
    "عفونی",
    "اطفال",
    "اپید",
    "دانش خانواده",
    "حوادث و بلایا",
    "فرهنگ و تمدن",
]

# دسته‌بندی‌های داخل هر درس. ترتیب همین لیست، ترتیب نمایش دکمه‌هاست.
#
# ۴ آیتم اول (اندیس ۰ تا ۳) زیرمجموعه‌ی منویِ «📚 منابع» هستن -- توی UI دانشجو
# اول یه دکمه‌ی «📚 منابع» نشون داده می‌شه که با زدنش زیرمنوی این ۴ تا باز می‌شه
# (نگاه کن به MANABE_COUNT و show_manabe_submenu). خودِ ذخیره‌سازی همچنان تخته
# (flat) روی این لیست انجام می‌شه؛ گروه‌بندی فقط یه لایه‌ی نمایشیه.
CATEGORIES = [
    "📖 پاور",
    "📝 جزوه",
    "📌 خلاصه",
    "⭐️ نکات امتحانی",
    "💡 پیشنهاد مطالعه",
    "📘 Study_Guid",
    "❓ نمونه‌سوالات",
    "🩺 تجربه ارسالی دانشجویان",
    "🧪 عملی",
    "📂 فایل‌های تکمیلی",
    "📋 طرح درس",
    # این یکی صرفاً یه اسمِ legacy/رزرو-شده‌ست که هیچ‌وقت به‌عنوان گزینه نشون داده
    # نمی‌شه (نگاه کن به REFERENCE_CATEGORY توی categories_for/build_course_category_buttons).
    # ساختِ واقعیِ ترم‌های «رفرنس» الان از طریقِ CUSTOM_MODE انجام می‌شه (پایین‌تر)،
    # نه از طریقِ این اسمِ ثابت.
    "📚 رفرنس‌ها",
]

# این اسم صرفاً reserved/legacy است (نگاه کن به توضیحِ بالای CATEGORIES) و در هیچ
# منویی به‌عنوان گزینه نشون داده نمی‌شه.
REFERENCE_CATEGORY = "📚 رفرنس‌ها"

# چند تا از ابتدای CATEGORIES زیرِ منوی «📚 منابع» جمع می‌شن
MANABE_COUNT = 4
MANABE_LABEL = "📚 منابع"

# حالتِ «سفارشی» یک درس (course_data["category_mode"] == CUSTOM_MODE، یا
# dbmod.get_course_mode(course_id) == CUSTOM_MODE) یعنی این درس دیگه اصلاً از
# لیستِ سراسریِ CATEGORIES پیروی نمی‌کنه -- هر دسته‌ای که واقعاً برای همین
# course_id در جدولِ categories باشه (هر اسمی، هر تعداد، مثل «گری»/«اسنل»/«اطلس»
# زیرِ درسِ «آناتومی» توی ترمِ «رفرنس») دقیقاً همون‌جوری نمایش داده می‌شه. جایگزینِ
# مکانیزمِ قدیمیِ single_category (که فقط یک اسمِ از CATEGORIES رو قبول می‌کرد).
STANDARD_MODE = "standard"
CUSTOM_MODE = "custom"

# این دسته فقط برای درس‌هایی که course["has_practical"] هست نشون داده می‌شه
PRACTICAL_CATEGORY = "🧪 عملی"

# فایل‌های رسمیِ تقسیم‌بندیِ مباحث بین استادان -- فقط برای درس‌هایی که
# course["has_lesson_plan"] هست نشون داده می‌شه (لیستِ دروس در
# db.LESSON_PLAN_COURSES_V2 تعریف شده و توسط migrate_category_restructure_v2
# روی دیتابیس اعمال می‌شه؛ دقیقاً همون منطقِ PRACTICAL_CATEGORY).
LESSON_PLAN_CATEGORY = "📋 طرح درس"
# ---------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# ذخیره و خواندن داده‌ی دروس
# ============================================================

def ensure_course_shape(course: dict) -> bool:
    """مطمئن می‌شه هر درس ساختار جدید (guide + categories) رو داره."""
    changed = False
    if "guide" not in course:
        course["guide"] = ""
        changed = True
    if "has_practical" not in course:
        course["has_practical"] = False
        changed = True
    if "has_lesson_plan" not in course:
        course["has_lesson_plan"] = False
        changed = True

    # نکته‌ی مهم (باگ‌فیکس): از migrate_categories_per_course_v3 به بعد، دسته‌های
    # هر درس id-based و در دیتابیس مستقلن -- یعنی ادمین می‌تونه از طریقِ
    # /editcategories یه دسته رو *عمداً* برای یه درسِ خاص حذف کنه. قبلاً اینجا
    # (توی else) هر دسته‌ای که توی CATEGORIES سراسری بود ولی توی دیکشنریِ این درس
    # نبود، دوباره ساخته می‌شد -- و چون load_data() با changed=True بلافاصله
    # save_data() رو صدا می‌زنه (که خودش هر دسته‌ی موجود در دیکشنری رو در دیتابیس
    # INSERT می‌کنه)، همین یه فراخوانیِ ساده‌ی load_data() (که تقریباً همه‌ی
    # هندلرها انجامش می‌دن) کافی بود تا دسته‌ی همین الان حذف‌شده فوراً از نو در
    # دیتابیس ساخته بشه -- یعنی حذف هیچ‌وقت واقعاً پایدار نمی‌موند.
    # برای همین این بازسازیِ خودکار فقط باید برای درسِ کاملاً تازه (که اصلاً
    # کلیدِ "categories" نداره) انجام بشه، نه برای درسی که از قبل دسته دارد ولی
    # ادمین چندتاشو عمداً حذف کرده.
    categories = course.get("categories")
    if categories is None:
        old_files = course.pop("files", [])
        course["categories"] = {cat: {"files": []} for cat in CATEGORIES}
        if old_files:
            course["categories"][CATEGORIES[-1]]["files"] = old_files
        changed = True

    for cat_data in course.get("categories", {}).values():
        for f in cat_data.get("files", []):
            if "fid" not in f:
                f["fid"] = uuid.uuid4().hex[:8]
                changed = True
    return changed


def load_programs() -> list:
    """لیست برنامه‌ها/ترم‌ها رو برمی‌گردونه (از SQLite). اگه خالی بود، با پیش‌فرض‌ها می‌سازتش."""
    programs = dbmod.get_program_names()
    if not programs:
        for name in DEFAULT_PROGRAM_NAMES:
            dbmod.add_program(name)
        programs = dbmod.get_program_names()
    return programs


def save_programs(programs: list) -> None:
    """نگه‌داشته شده برای سازگاری با کد قدیمی؛ در نسخه‌ی SQLite هر ترم با
    add_program جدا ثبت می‌شه، پس اینجا فقط مطمئن می‌شیم همه هستن."""
    for name in programs:
        dbmod.add_program(name)


def get_program_names() -> list:
    """لیست فعلی برنامه‌ها/ترم‌ها (شامل هر ترمی که با /addsemester اضافه شده)."""
    return load_programs()


def add_program(name: str, group_name: str = None) -> bool:
    """یه برنامه/ترم جدید رو به لیست اضافه می‌کنه. اگه از قبل بود، False برمی‌گردونه.
    group_name اختیاریه؛ اگه ندی، همون پیش‌فرضِ dbmod.add_program (فیزیوپات) استفاده می‌شه."""
    if group_name:
        return dbmod.add_program(name, group_name=group_name)
    return dbmod.add_program(name)


# ------------------------------------------------------------------
# مجموعه‌ی state هایی که یعنی «منتظر یه پیام متنیِ بعدیم برای یه ویزاردِ
# مدیریتیِ خاص». چون همه‌شون توی همون context.user_data مشترک نگه‌داری
# می‌شن و receive_text به ترتیبِ نوشته‌شدن چکشون می‌کنه، اگه یه ویزارد
# نیمه‌کاره رها بشه (مثلاً ادمین /addsemester رو زد ولی به‌جاش رفت سراغ
# /addcourse)، فلگِ قدیمی هنوز ست می‌مونه و پیامِ متنیِ بعدی -- که برای
# ویزاردِ جدیده -- به‌جاش توسط ویزاردِ فراموش‌شده مصرف می‌شه (دقیقاً همون
# باگی که باعث می‌شد به‌جای اضافه‌شدنِ درس به ترمِ انتخاب‌شده، یه ترمِ
# جدید ساخته بشه). برای همین، هر «شروعِ» یه ویزاردِ جدید باید اول این
# تابع رو صدا بزنه تا هر state نیمه‌کاره‌ی قبلی پاک بشه.
_ADMIN_FLOW_STATE_KEYS = [
    "awaiting_semester_name",
    "awaiting_semester_group",
    "awaiting_course_name_for_term",
    "awaiting_course_rename",
    "awaiting_term_rename",
    "awaiting_new_group_name",
    "awaiting_note_text",
    "awaiting_note_edit",
    "awaiting_group_rename",
    "awaiting_upload_new_name",
    "awaiting_draft_edit",
    "awaiting_section_name",
    "awaiting_section_rename",
    "awaiting_category_add",
    "awaiting_category_rename",
    "ecat_bulk",
    "pending_guide",
    "pending_rename",
    "pending_desc_edit",
    "pending_class_entry",
    "pending_class_location",
    "pending_quick_course_group",
    "pending_exam_entry",
    "pending_course_group",
    "pending_class_exception",
    "pending_term_period",
    "pending_exam_type_choice",
    "pending_fixed_event",
]


def clear_admin_flow_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """هر state نیمه‌کاره‌ی یه ویزاردِ مدیریتیِ دیگه رو پاک می‌کنه؛ قبل از ست‌کردنِ
    فلگِ ویزاردِ جدید صدا زده می‌شه (نگاه کن به توضیحِ _ADMIN_FLOW_STATE_KEYS)."""
    for key in _ADMIN_FLOW_STATE_KEYS:
        context.user_data.pop(key, None)


def ensure_programs(data: dict) -> bool:
    """مطمئن می‌شه هر برنامه/ترمِ ثبت‌شده توی داده وجود داره.
    (توجه: قبلاً اینجا درس‌های پیش‌فرضِ فیزیوپات۱ هم هر بار دوباره ساخته می‌شدن که
    باعث می‌شد حذف/تغییرنامِ عمدیِ ادمین روی این درس‌ها ماندگار نمونه. اون seeding حالا
    یه‌بار برای همیشه توی db.seed_physiopath1_defaults() در main() انجام می‌شه.)"""
    changed = False
    for program in get_program_names():
        if program not in data:
            data[program] = {}
            changed = True
    return changed


def load_data() -> dict:
    data = dbmod.load_data()

    changed = ensure_programs(data)
    for courses in data.values():
        for course in courses.values():
            if ensure_course_shape(course):
                changed = True
    if changed:
        save_data(data)

    return data


def save_data(data: dict) -> None:
    dbmod.save_data(data)


def new_course() -> dict:
    return {
        "guide": "",
        "has_practical": False,
        "has_lesson_plan": False,
        "categories": {cat: {"files": []} for cat in CATEGORIES},
    }


def categories_for(program: str, course_name: str) -> list:
    """لیستِ (category_id واقعی در دیتابیس, نامِ دسته) رو برمی‌گردونه -- برای همه‌ی
    منوهای انتخابِ دسته (آپلود/حذف/جابه‌جایی/نشان/تغییرنام/توضیح/فایل‌گروپ) استفاده
    می‌شه. مقدارِ اول دیگه ایندکسِ CATEGORIES سراسری نیست؛ این عمداً همون id واقعیِ
    ردیفِ categories است -- چون CATEGORIES فقط دسته‌های *استاندارد* رو می‌شناسه و
    دسته‌های سفارشیِ یک درس (مثلاً «گری»/«اسنل»/«اطلس») اصلاً توش نیستن. با
    threading کردنِ همین id (به‌جای ایندکس) در callback_data، این دسته‌ها هم برای
    اولین‌بار درست در منوها ظاهر می‌شن.

    اگه درس در حالتِ سفارشی باشه (dbmod.get_course_mode == CUSTOM_MODE -- مثلاً
    ترم‌های «رفرنس»)، *همه‌ی* دسته‌های واقعیِ این درس در دیتابیس برمی‌گرده، بدونِ
    هیچ فیلترِ عملی/طرح‌درس/منابعِ استاندارد. برای درس‌های استاندارد، دقیقاً همون
    منطقِ قبلی (فیلترِ CATEGORIES + عملی/طرح‌درس + دسته‌های عمداً حذف‌شده) حفظ شده."""
    course_id = dbmod.get_course_id_by_name(program, course_name)
    if course_id is None:
        return []

    if dbmod.get_course_mode(course_id) == CUSTOM_MODE:
        return [(c["id"], c["name"]) for c in dbmod.get_categories_for_course(course_id)]

    has_practical = dbmod.get_has_practical(program, course_name)
    has_lesson_plan = dbmod.get_has_lesson_plan(program, course_name)
    # نکته‌ی مهم (باگ‌فیکس): active_names یعنی چه دسته‌هایی *واقعاً* توی دیتابیسِ
    # این درسِ خاص هنوز موجودن. اگه ادمین یه دسته رو از طریقِ /editcategories حذف
    # کرده باشه، دیگه نباید توی منوهای آپلود/جابه‌جایی/تغییرنام/... به‌عنوان گزینه
    # نشون داده بشه -- وگرنه انتخابِ همون گزینه‌ی «حذف‌شده» و آپلودِ یه فایل توش،
    # همون دسته رو دوباره در دیتابیس می‌سازه (چون save_data() هر دسته‌ی توی
    # دیکشنریِ ورودی رو که در دیتابیس نباشه INSERT می‌کنه) و انگار حذف اصلاً
    # ماندگار نبوده. اگه active_names برگرده None (مثلاً درس هنوز کلاً ساخته
    # نشده)، هیچ فیلترِ اضافه‌ای اعمال نمی‌کنیم -- رفتارِ قبلی حفظ می‌شه.
    active_names = dbmod.get_active_category_names(program, course_name)
    result = []
    for cat in CATEGORIES:
        if cat == PRACTICAL_CATEGORY and not has_practical:
            continue
        if cat == LESSON_PLAN_CATEGORY and not has_lesson_plan:
            continue
        if cat == REFERENCE_CATEGORY:
            continue
        if active_names is not None and cat not in active_names:
            continue
        cat_id = dbmod.get_category_id_for_course(course_id, cat)
        if cat_id is None:
            continue
        result.append((cat_id, cat))
    return result


def build_course_category_buttons(program: str, course_name: str, course_data: dict) -> list:
    """دکمه‌های بخش‌های یه درس برای نمایش به دانشجو: یه دکمه‌ی گروهیِ «📚 منابع»
    (که با زدنش زیرمنوی ۴ زیربخشش باز می‌شه) + بقیه‌ی بخش‌های مستقل، با فیلترِ
    🧪 عملی بر اساس has_practical و 📋 طرح درس بر اساس has_lesson_plan همون درس.

    اگه درس در حالتِ سفارشی باشه (category_mode == CUSTOM_MODE -- مثلاً ترم‌های
    «رفرنس»)، به‌جاش هر دسته‌ی واقعیِ این درس در دیتابیس (با هر اسمی، هر تعداد --
    مثلاً «گری»/«اسنل»/«اطلس») مستقیم به‌عنوان یه دکمه‌ی مستقل نشون داده می‌شه --
    بدونِ گروهِ «📚 منابع» و بدونِ فیلترِ عملی/طرح‌درس.

    توجه: callback_data از course_id استفاده می‌کنه (نه program:course_name) چون
    تلگرام callback_data رو به ۶۴ بایت UTF-8 محدود می‌کنه؛ برای درس/ترم‌های با
    اسم فارسیِ طولانی (مثل «فیزیوپات ۳ - آسیب‌شناسی اختصاصی۲») این حد رد می‌شد
    و کل کیبورد رندر نمی‌شد (دکمه‌ها اصلاً کار نمی‌کردن). course_id همیشه کوتاهه."""
    categories = course_data.get("categories", {})
    course_id = course_data.get("id")

    # حالتِ سفارشی (مثلاً ترم‌های «رفرنس»): همه‌ی دسته‌های واقعیِ این درس رو مستقیم
    # از دیتابیس می‌خونیم و نشون می‌دیم -- بدونِ گروهِ «📚 منابع»، بدونِ فیلترِ
    # عملی/طرح‌درس، و بدونِ محدودیتِ «فقط یه دسته» که single_category قدیمی داشت.
    if course_id is not None and course_data.get("category_mode") == CUSTOM_MODE:
        cats = dbmod.get_categories_for_course(course_id)
        buttons = []
        for c in cats:
            n = len(categories.get(c["name"], {}).get("files", []))
            label = f"{c['name']} ({n})" if n else c["name"]
            buttons.append([InlineKeyboardButton(label, callback_data=f"cat:{course_id}:{c['id']}")])
        return buttons

    has_practical = bool(course_data.get("has_practical"))
    has_lesson_plan = bool(course_data.get("has_lesson_plan"))

    # نکته‌ی مهم (باگ‌فیکس): categories همون دیکشنریِ واقعیِ برگرفته از دیتابیسه
    # (نگاه کن به dbmod.load_data) -- یعنی اگه ادمین یه دسته رو از طریقِ
    # /editcategories حذف کرده باشه، دیگه توی این دیکشنری نیست. قبلاً این تابع
    # بدونِ توجه به این موضوع، دکمه‌ی هر ۱۰-۱۱ دسته‌ی استاندارد رو همیشه نشون
    # می‌داد (فقط تعدادِ فایلش صفر می‌شد) -- یعنی از دیدِ دانشجو/ادمینِ درحالِ
    # آپلود، انگار دسته‌ی حذف‌شده اصلاً حذف نشده بود. برای همین این‌جا هر دسته‌ی
    # غایب از دیکشنری (یعنی واقعاً حذف‌شده) رو کامل از منو کنار می‌ذاریم.
    manabe_cats = [c for c in CATEGORIES[:MANABE_COUNT] if c in categories]
    buttons = []
    if manabe_cats:
        manabe_n = sum(len(categories.get(c, {}).get("files", [])) for c in manabe_cats)
        manabe_label = f"{MANABE_LABEL} ({manabe_n})" if manabe_n else MANABE_LABEL
        buttons.append([InlineKeyboardButton(manabe_label, callback_data=f"manabe:{course_id}")])

    for i, cat in enumerate(CATEGORIES):
        if i < MANABE_COUNT:
            continue
        if cat == PRACTICAL_CATEGORY and not has_practical:
            continue
        if cat == LESSON_PLAN_CATEGORY and not has_lesson_plan:
            continue
        if cat == REFERENCE_CATEGORY:
            continue
        if cat not in categories:
            continue
        cat_id = dbmod.get_category_id_for_course(course_id, cat)
        if cat_id is None:
            continue
        n = len(categories.get(cat, {}).get("files", []))
        label = f"{cat} ({n})" if n else cat
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat:{course_id}:{cat_id}")])

    return buttons


def category_name_by_id(cat_idx):
    """توجه: در سراسرِ این فایل، متغیرهایی به اسمِ cat_idx که از callback_data
    (مثلِ cat:{course_id}:{cat_idx} یا pick_cat_upload:{course_id}:{cat_idx}) خونده
    می‌شن، دیگه ایندکسِ CATEGORIES سراسری نیستن -- همون id واقعیِ ردیفِ categories در
    دیتابیسن (نگاه کن به categories_for/build_course_category_buttons). این تابع
    اسمِ واقعیِ دسته رو برمی‌گردونه؛ برای دسته‌های سفارشی (مثلِ «گری») هم درست کار
    می‌کنه چون مستقیم از جدولِ categories می‌خونه، نه از لیستِ سراسری. اگه id دیگه
    وجود نداشته باشه (مثلاً دسته حذف شده)، None برمی‌گردونه."""
    row = dbmod.get_category_by_id(cat_idx)
    return row["name"] if row else None


def find_file_by_fid(data: dict, fid: str):
    """توی کل دیتابیس دنبال فایلی با این fid می‌گرده.
    خروجی: (program, course_name, category, index, file_dict) یا None."""
    for program, courses in data.items():
        for course_name, course in courses.items():
            for category, cat_data in course.get("categories", {}).items():
                files = cat_data.get("files", [])
                for i, f in enumerate(files):
                    if f.get("fid") == fid:
                        return program, course_name, category, i, f
    return None


async def deliver_file_by_fid(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, fid: str) -> bool:
    """فایل/لینک/نوت را با fid تحویل می‌دهد؛ همان داده و شمارنده‌ی دانلودِ مسیر عادی را استفاده می‌کند.
    این تابع برای Deep Link است و هیچ مسیر دسترسیِ جدیدی خارج از عضویت/دسترسی فعلی بات ایجاد نمی‌کند."""
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        return False
    _program, _course_name, _category, _index, file_info = found

    file_info["downloads"] = file_info.get("downloads", 0) + 1
    save_data(data)

    caption = display_caption(file_info)
    description = file_info.get("description", "")
    full_caption = f"{caption}\n\n📝 {description}" if description else caption
    ftype = file_info.get("type")
    if ftype == "link":
        await context.bot.send_message(chat_id=chat_id, text=f"{caption}:\n{file_info['url']}")
    elif ftype in ("text", "note"):
        content = file_info.get("content", caption)
        await context.bot.send_message(chat_id=chat_id, text=(f"📝 {caption}\n\n{content}" if caption and ftype == "note" else content))
    else:
        await context.bot.send_document(chat_id=chat_id, document=file_info["file_id"], caption=full_caption)
    return True


def display_caption(file_info: dict) -> str:
    """کپشن فایل رو با نشان کیفیت (اگه داشته باشه) برمی‌گردونه."""
    badge = file_info.get("badge")
    emoji = QUALITY_BADGES.get(badge, "")
    caption = file_info.get("caption", "")
    return f"{emoji} {caption}".strip() if emoji else caption


def apply_original_extension(new_name: str, original_name: str) -> str:
    """اگه ادمین موقعِ تغییرنام پسوند ننوشته باشه، پسوندِ فایلِ اصلی (original_name)
    رو خودکار بهش اضافه می‌کنه -- کاربر مجبور نیست پسوند رو حفظ/تایپ کنه.

    اگه new_name خودش با چیزی شبیهِ یه پسوند (۱ تا ۸ کاراکترِ حرف/عدد بعد از نقطه)
    تموم بشه، یعنی احتمالاً عمداً پسوند رو نوشته -- دست‌نخورده برمی‌گرده."""
    new_name = (new_name or "").strip()
    _, orig_ext = os.path.splitext(original_name or "")
    if not orig_ext:
        return new_name
    _, new_ext = os.path.splitext(new_name)
    if new_ext and 1 <= len(new_ext) - 1 <= 8 and new_ext[1:].isalnum():
        return new_name  # کاربر خودش یه پسوند نوشته؛ دست نمی‌زنیم
    return new_name + orig_ext


def is_admin(user_id: int) -> bool:
    """هر سطحی از ادمین (SUPER_ADMIN یا TERM_ADMIN) -- کاربر ممکنه هم‌زمان
    ادمینِ چند ترمِ مختلف باشه؛ کافیه حداقل یک نقش داشته باشه."""
    return len(dbmod.get_admin_roles(user_id)) > 0


def is_super_admin(user_id: int) -> bool:
    return any(r["role"] == "SUPER_ADMIN" for r in dbmod.get_admin_roles(user_id))


def can_manage_term(user_id: int, term_name: str) -> bool:
    """آیا این کاربر اجازه‌ی مدیریت این ترمِ به‌خصوص رو داره؟ چک اصلی سطح backend."""
    return dbmod.can_manage_term(user_id, term_name)


def admin_allowed_programs(user_id: int) -> list:
    """لیست ترم‌هایی که این ادمین توی منوها می‌بینه (سوپرادمین: همه، Term Admin: فقط ترم خودش)."""
    return dbmod.allowed_terms_for(user_id)


async def deny_term_access(query, term_name: str) -> None:
    await query.edit_message_text(f"⛔️ شما دسترسی مدیریت «{term_name}» رو ندارید.")


def get_super_admin_ids() -> list:
    return [a["user_id"] for a in dbmod.list_admins() if a["role"] == "SUPER_ADMIN"]


def load_subscribers() -> list:
    return dbmod.load_subscribers()


def save_subscribers(subscribers: list) -> None:
    dbmod.save_subscribers(subscribers)


def get_notify_prefs(user_id: int) -> dict:
    return dbmod.get_notify_prefs(user_id)


def set_notify_pref(user_id: int, key: str, value: bool) -> dict:
    return dbmod.set_notify_pref(user_id, key, value)


NOTIFY_LABELS = {
    "new_files": "📥 فایل/بخش جدیدِ مربوط به ترم خودم",
    "exam_chat": "📚 گپ امتحانیِ ترم خودم در جریانه",
    "casual_chat": "☕ گپ دوستانه در جریانه",
    "startup": "🟢 روشن‌شدنِ ربات",
    "schedule_change": "🏫 تغییرِ برنامه‌ی کلاسیِ ترم خودم",
    "class_reminder": "⏰ یادآوریِ قبل از شروعِ کلاس",
}
NOTIFY_ORDER = ["new_files", "exam_chat", "casual_chat", "startup", "schedule_change", "class_reminder"]

NOTIFY_MENU_INTRO = (
    "🔔 اطلاع‌رسانی\n\n"
    "هر بخش رو جدا روشن/خاموش کن؛ با هر تپ، وضعیتش عوض می‌شه:"
)


def build_notify_menu_text(user_id: int) -> str:
    text = NOTIFY_MENU_INTRO
    if not get_user_class_program(user_id):
        text += (
            "\n\n⚠️ برای اینکه «گپ امتحانی» و «فایل جدید» دقیقاً مالِ ترم خودت باشه، "
            "اول باید ترمت رو انتخاب کنی (پایین همین پیام یا از «📅 برنامه شخصی → 🔁 تغییر ترم»)."
        )
    return text


def build_notify_menu_markup(user_id: int) -> InlineKeyboardMarkup:
    prefs = get_notify_prefs(user_id)
    buttons = [
        [InlineKeyboardButton(
            f"{'✅' if prefs.get(key) else '◻️'} {NOTIFY_LABELS[key]}",
            callback_data=f"toggle_notify:{key}",
        )]
        for key in NOTIFY_ORDER
    ]
    if not get_user_class_program(user_id):
        buttons.append([InlineKeyboardButton("🔁 انتخاب ترم", callback_data="change_class_program")])
    buttons.append([InlineKeyboardButton("✖️ بستن", callback_data="notify_menu_close")])
    return InlineKeyboardMarkup(buttons)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_COURSES), KeyboardButton(MENU_NOTIFY)],
            [KeyboardButton(MENU_PARTICIPATE), KeyboardButton(MENU_ABOUT)],
            [KeyboardButton(MENU_SCHEDULE), KeyboardButton(MENU_STUDY_GROUP)],
            [KeyboardButton(MENU_CHAT), KeyboardButton(MENU_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def chat_room_keyboard() -> ReplyKeyboardMarkup:
    """کیبورد موقتی که وقتی کاربر داخل یه اتاق گپه جایگزین منوی اصلی می‌شه."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(CHAT_IDENTITY_TOGGLE_BUTTON), KeyboardButton(CHAT_EXIT_BUTTON)],
            [KeyboardButton(CHAT_RANDOM_TOPIC_BUTTON), KeyboardButton(CHAT_POLL_BUTTON)],
            [KeyboardButton(CHAT_GAME_BUTTON)],
        ],
        resize_keyboard=True,
    )


def _atomic_json_dump(path: str, data) -> None:
    """نوشتن JSON به‌صورت atomic تا crash وسط write فایل Runtime را نیمه‌کاره نکند."""
    tmp = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_class_schedule() -> dict:
    """برنامه رسمی کلاس‌ها. رکوردهای قدیمی در اولین خواندن یک id پایدار می‌گیرند."""
    if not os.path.exists(CLASS_SCHEDULE_FILE):
        return {}
    with open(CLASS_SCHEDULE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    changed = False
    for days in data.values():
        for entries in days.values():
            for entry in entries:
                if not entry.get("id"):
                    entry["id"] = uuid.uuid4().hex[:12]
                    changed = True
    if changed:
        save_class_schedule(data)
    return data


def save_class_schedule(schedule: dict) -> None:
    _atomic_json_dump(CLASS_SCHEDULE_FILE, schedule)


def load_class_exceptions() -> dict:
    """لغوهای استثناییِ یه‌روزه‌ی کلاس‌های رسمی -- بدونِ حذفِ خودِ بلوکِ
    تکرارشونده (فاز ۱۴). ساختار: {برنامه: {روز: {"درس|شروع|پایان":
    ["YYYY-MM-DD", ...]}}}. تاریخ عمداً میلادیه (نه جلالی) چون همون فرمتیه که
    check_reminders هم برای today_date_str استفاده می‌کنه (now_tehran) -- برای
    مچ‌شدنِ خودکار، بدونِ نیازِ به یه کتابخانه‌ی تقویمِ جدید."""
    if not os.path.exists(CLASS_EXCEPTIONS_FILE):
        return {}
    with open(CLASS_EXCEPTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_class_exceptions(data: dict) -> None:
    _atomic_json_dump(CLASS_EXCEPTIONS_FILE, data)


def _class_entry_key(entry: dict) -> str:
    # از فاز اصلاح برنامه‌ریزی، هر بلوک شناسه‌ی پایدار دارد تا دو گروه از یک درس
    # با ساعت یکسان، لغوهای یک‌روزه‌ی همدیگر را به اشتباه به ارث نبرند.
    entry_id = entry.get("id")
    if entry_id:
        return f"id:{entry_id}"
    # fallback برای رکورد/پیام خیلی قدیمی
    return f"{entry['course']}|{entry['start']}|{entry['end']}"


def get_class_exception_dates(program: str, day: str, entry: dict) -> list:
    exceptions = load_class_exceptions()
    day_map = exceptions.get(program, {}).get(day, {})
    key = _class_entry_key(entry)
    dates = list(day_map.get(key, []))
    # سازگاری با لغوهایی که قبل از اضافه‌شدن id ذخیره شده‌اند.
    if entry.get("id"):
        legacy_key = f"{entry['course']}|{entry['start']}|{entry['end']}"
        # لغو legacy فقط وقتی قابل‌اعتماد است که دقیقاً یک کلاس با این امضا وجود داشته باشد؛
        # وگرنه (مثلاً دو گروه هم‌زمان) معلوم نیست لغو متعلق به کدام گروه بوده.
        same_signature = [
            e for e in load_class_schedule().get(program, {}).get(day, [])
            if f"{e.get('course')}|{e.get('start')}|{e.get('end')}" == legacy_key
        ]
        if len(same_signature) == 1:
            for d in day_map.get(legacy_key, []):
                if d not in dates:
                    dates.append(d)
    return sorted(dates)


def is_class_cancelled_on(program: str, day: str, entry: dict, date_str: str) -> bool:
    return date_str in get_class_exception_dates(program, day, entry)


def next_date_for_fa_weekday(day: str, *, include_today: bool = True):
    """تاریخ نزدیک‌ترین رخدادِ این روز هفته را به وقت تهران برمی‌گرداند."""
    now = now_tehran()
    current_day = PY_WEEKDAY_TO_FA[now.weekday()]
    try:
        cur_idx = DAYS_OF_WEEK.index(current_day)
        target_idx = DAYS_OF_WEEK.index(day)
    except ValueError:
        return None
    delta = (target_idx - cur_idx) % 7
    if delta == 0 and not include_today:
        delta = 7
    return (now.date() + timedelta(days=delta)).strftime("%Y-%m-%d")


def add_class_exception(program: str, day: str, entry: dict, date_str: str) -> None:
    exceptions = load_class_exceptions()
    key = _class_entry_key(entry)
    dates = exceptions.setdefault(program, {}).setdefault(day, {}).setdefault(key, [])
    if date_str not in dates:
        dates.append(date_str)
        dates.sort()
    save_class_exceptions(exceptions)


def remove_class_exception(program: str, day: str, entry: dict, date_str: str) -> bool:
    exceptions = load_class_exceptions()
    key = _class_entry_key(entry)
    dates = exceptions.get(program, {}).get(day, {}).get(key, [])
    if date_str in dates:
        dates.remove(date_str)
        save_class_exceptions(exceptions)
        return True
    return False


def load_fixed_events() -> list:
    """رویدادهای ثابت هفتگی؛ رکوردهای قدیمی در اولین خواندن id پایدار می‌گیرند."""
    if not os.path.exists(FIXED_EVENTS_FILE):
        return []
    with open(FIXED_EVENTS_FILE, "r", encoding="utf-8") as f:
        events = json.load(f)
    changed = False
    for event in events:
        if not event.get("id"):
            event["id"] = uuid.uuid4().hex[:12]
            changed = True
        if not event.get("type"):
            normalized_name = (event.get("name") or "").strip()
            if normalized_name == "رزرو غذا":
                event["type"] = "food"
            elif normalized_name == "دوشنبه فرهنگی":
                event["type"] = "cultural"
            else:
                event["type"] = "general"
            changed = True
    if changed:
        save_fixed_events(events)
    return events


def save_fixed_events(events: list) -> None:
    _atomic_json_dump(FIXED_EVENTS_FILE, events)


def get_fixed_events_for_day(day: str, program: str = None) -> list:
    """رویدادهای ثابتِ یه روزِ خاص که به این program مربوط می‌شن (یا scope='all')."""
    return [e for e in load_fixed_events() if e["day"] == day and (e["scope"] == "all" or e["scope"] == program)]


def load_personal_schedule() -> dict:
    """برنامه مطالعه شخصی؛ هر بلوک id پایدار دارد تا حذف/Snooze به index وابسته نباشد."""
    if not os.path.exists(PERSONAL_SCHEDULE_FILE):
        return {}
    with open(PERSONAL_SCHEDULE_FILE, "r", encoding="utf-8") as f:
        schedule = json.load(f)
    changed = False
    for days in schedule.values():
        for blocks in days.values():
            for block in blocks:
                if not block.get("id"):
                    block["id"] = uuid.uuid4().hex[:12]
                    changed = True
    if changed:
        save_personal_schedule(schedule)
    return schedule


def save_personal_schedule(schedule: dict) -> None:
    _atomic_json_dump(PERSONAL_SCHEDULE_FILE, schedule)


def get_user_class_program(user_id: int):
    users = load_users()
    return users.get(str(user_id), {}).get("class_program")


def set_user_class_program(user_id: int, program: str) -> None:
    users = load_users()
    entry = users.setdefault(str(user_id), {})
    entry["class_program"] = program
    save_users(users)


def _resolve_program_token(token: str):
    """callbackهای جدید term_id می‌فرستن؛ نامِ قدیمی هم برای پیام‌های قبلی پشتیبانی می‌شه."""
    if token.isdigit():
        return program_for_term_id(int(token))
    return token if token in get_program_names() else None


def _program_buttons(prefix: str) -> list:
    buttons = []
    for program in get_program_names():
        term_id = term_id_for_program(program)
        token = str(term_id) if term_id is not None else program
        data = f"{prefix}:{token}"
        if validate_callback_data(data, context_label=f"{prefix}/program={program}"):
            buttons.append([InlineKeyboardButton(program, callback_data=data)])
    return buttons


def _rename_schedule_references(old_name: str, new_name: str) -> None:
    """همه‌ی داده‌های Runtime برنامه‌ریزی را بعد از rename ترم همگام می‌کند."""
    schedule = load_class_schedule()
    if old_name in schedule:
        old_days = schedule.pop(old_name)
        dest = schedule.setdefault(new_name, {})
        for day, entries in old_days.items():
            dest.setdefault(day, []).extend(entries)
        save_class_schedule(schedule)

    exceptions = load_class_exceptions()
    if old_name in exceptions:
        old_days = exceptions.pop(old_name)
        dest = exceptions.setdefault(new_name, {})
        for day, values in old_days.items():
            dest.setdefault(day, {}).update(values)
        save_class_exceptions(exceptions)

    personal = load_personal_schedule()
    changed = False
    for days in personal.values():
        for blocks in days.values():
            for b in blocks:
                if b.get("program") == old_name:
                    b["program"] = new_name
                    changed = True
    if changed:
        save_personal_schedule(personal)

    events = load_fixed_events()
    changed = False
    for e in events:
        if e.get("scope") == old_name:
            e["scope"] = new_name
            changed = True
    if changed:
        save_fixed_events(events)
    dbmod.update_users_class_program(old_name, new_name)


def _delete_schedule_references(term_name: str) -> None:
    schedule = load_class_schedule()
    if schedule.pop(term_name, None) is not None:
        save_class_schedule(schedule)
    exceptions = load_class_exceptions()
    if exceptions.pop(term_name, None) is not None:
        save_class_exceptions(exceptions)

    personal = load_personal_schedule()
    changed = False
    for days in personal.values():
        for day in list(days):
            kept = [b for b in days[day] if b.get("program") != term_name]
            if len(kept) != len(days[day]):
                days[day] = kept
                changed = True
    if changed:
        save_personal_schedule(personal)

    events = load_fixed_events()
    kept_events = [e for e in events if e.get("scope") != term_name]
    if len(kept_events) != len(events):
        save_fixed_events(kept_events)
    dbmod.clear_users_class_program(term_name)


def _rename_course_schedule_references(term_name: str, course_id: int, old_name: str, new_name: str) -> None:
    schedule = load_class_schedule()
    changed = False
    for entries in schedule.get(term_name, {}).values():
        for e in entries:
            if e.get("course_id") == course_id or (e.get("course_id") is None and e.get("course") == old_name):
                e["course"] = new_name
                e["course_id"] = course_id
                changed = True
    if changed:
        save_class_schedule(schedule)
    personal = load_personal_schedule()
    changed = False
    for days in personal.values():
        for blocks in days.values():
            for b in blocks:
                if b.get("program") == term_name and b.get("course") == old_name:
                    b["course"] = new_name
                    changed = True
    if changed:
        save_personal_schedule(personal)


def _delete_course_schedule_references(term_name: str, course_id: int, course_name: str) -> None:
    schedule = load_class_schedule()
    removed_entries = []
    changed = False
    for day, entries in schedule.get(term_name, {}).items():
        kept = []
        for e in entries:
            if e.get("course_id") == course_id or (e.get("course_id") is None and e.get("course") == course_name):
                removed_entries.append((day, e))
                changed = True
            else:
                kept.append(e)
        schedule.get(term_name, {})[day] = kept
    if changed:
        save_class_schedule(schedule)

    if removed_entries:
        exceptions = load_class_exceptions()
        term_ex = exceptions.get(term_name, {})
        ex_changed = False
        for day, e in removed_entries:
            day_map = term_ex.get(day, {})
            for key in (_class_entry_key(e), f"{e.get('course')}|{e.get('start')}|{e.get('end')}"):
                if key in day_map:
                    day_map.pop(key, None)
                    ex_changed = True
        if ex_changed:
            save_class_exceptions(exceptions)

    personal = load_personal_schedule()
    p_changed = False
    for days in personal.values():
        for day in list(days):
            kept = [b for b in days[day] if not (b.get("program") == term_name and b.get("course") == course_name)]
            if len(kept) != len(days[day]):
                days[day] = kept
                p_changed = True
    if p_changed:
        save_personal_schedule(personal)


def _jalali_to_gregorian(jy: int, jm: int, jd: int):
    """تبدیلِ تاریخِ شمسی به میلادی (الگوریتمِ استانداردِ Borkowski -- همونی که
    کتابخانه‌ی jdatetime هم ازش استفاده می‌کنه). خروجی: (gy, gm, gd)."""
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd + (
        (jm - 1) * 31 if jm < 7 else ((jm - 7) * 30) + 186
    )
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    is_leap = gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)
    sal_a = [0, 31, 29 if is_leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    for i in range(1, 13):
        v = sal_a[i]
        if gd <= v:
            gm = i
            break
        gd -= v
    return gy, gm, gd


def _gregorian_to_jalali(gy: int, gm: int, gd: int):
    """معکوسِ _jalali_to_gregorian -- برای نمایشِ یه تاریخِ میلادیِ ذخیره‌شده به
    شکلِ شمسی (مثلاً تاریخ‌های لغوِ کلاسی که تویِ فایل همیشه میلادی ذخیره
    می‌شن، نگاه کن به is_class_cancelled_on)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def parse_flexible_date_to_gregorian(raw: str):
    """می‌تونه هم تاریخِ میلادی (YYYY-MM-DD یا YYYY/MM/DD) بگیره هم شمسی (همون
    فرمت‌ها، با رقم‌های فارسی یا انگلیسی) -- تشخیص بر اساسِ عددِ سال: زیرِ ۱۷۰۰
    یعنی شمسی، وگرنه میلادی. همیشه یه رشته‌ی میلادیِ نرمال‌شده‌ی YYYY-MM-DD
    برمی‌گردونه (یا None اگه نامعتبر بود) -- چون check_reminders و ذخیره‌سازیِ
    استثناها همیشه بر اساسِ همون فرمت مقایسه می‌شن."""
    cleaned = raw.strip().translate(_PERSIAN_DIGITS).replace("/", "-")
    parts = cleaned.split("-")
    if len(parts) != 3:
        return None
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        return None
    try:
        if y < 1700:
            # اعتبارسنجیِ دستیِ ماه/روزِ شمسی -- برخلافِ datetime() که خودش
            # تاریخِ نامعتبر رو رد می‌کنه، فرمولِ تبدیلِ شمسی به میلادی برای
            # ماه/روزِ خارج از بازه هم (بدونِ خطا) یه چیزی برمی‌گردونه، پس باید
            # اینجا صریحاً چک بشه.
            if not (1 <= m <= 12):
                return None
            max_day = 31 if m <= 6 else 30
            if not (1 <= d <= max_day):
                return None
            gy, gm, gd = _jalali_to_gregorian(y, m, d)
        else:
            gy, gm, gd = y, m, d
        return datetime(gy, gm, gd).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


def format_gregorian_as_jalali(iso_date: str) -> str:
    """'YYYY-MM-DD' میلادی -> 'YYYY/MM/DD' شمسی، برای نمایشِ خواناتر به ادمین.
    اگه پارس نشد، همون رشته‌ی ورودی رو برمی‌گردونه (fail-safe، نه کرش)."""
    try:
        gy, gm, gd = (int(p) for p in iso_date.split("-"))
        jy, jm, jd = _gregorian_to_jalali(gy, gm, gd)
        return f"{jy:04d}/{jm:02d}/{jd:02d}"
    except (ValueError, TypeError):
        return iso_date


def valid_time(t: str) -> bool:
    return bool(TIME_RE.match(t.strip()))


def _time_to_minutes(t: str) -> int:
    h, m = map(int, t.strip().split(":"))
    return h * 60 + m


def _ranges_overlap(s1: str, e1: str, s2: str, e2: str) -> bool:
    return _time_to_minutes(s1) < _time_to_minutes(e2) and _time_to_minutes(s2) < _time_to_minutes(e1)


def _parse_time_range(time_part: str):
    """'HH:MM-HH:MM' -> (start, end). بازه‌ی صفر/منفی پذیرفته نمی‌شود."""
    if "-" not in time_part:
        return None
    start, end = [p.strip() for p in time_part.split("-", 1)]
    if not (valid_time(start) and valid_time(end)):
        return None
    if _time_to_minutes(end) <= _time_to_minutes(start):
        return None
    return start, end


def filter_class_entries_for_student(entries: list, user_id: int):
    """بلوک‌های برنامه‌کلاسیِ یک روز رو برای یه دانشجویِ خاص فیلتر می‌کنه (فاز
    ۱۳ -- گروه‌بندی): بلوک‌های بدونِ group_id همیشه می‌مونن؛ اگر
    group_pending=True باشه یعنی گروه هنوز از طرف ادمین مشخص نشده و موقتاً برای
    همه نمایش داده می‌شه؛ در غیر این صورت بلوکِ بدون گروه واقعاً عمومی است.
    بلوک‌های گروه‌دار فقط اگه گروهِ انتخابیِ دانشجو برای همون درس دقیقاً مچ
    بشه نشون داده می‌شن. خروجی: (visible, missing_course_names) -- دومی
    اسمِ درس‌هایی که گروه‌بندی دارن ولی دانشجو هنوز گروهشو مشخص نکرده (برای
    نمایشِ هشدار، به‌جای سکوتِ کامل)."""
    visible, missing = [], []
    seen_missing = set()
    for e in entries:
        group_id = e.get("group_id")
        if group_id is None:
            visible.append(e)
            continue
        group = dbmod.get_course_group_by_id(group_id)
        if group is None:
            # گروه بعداً حذف شده -- این بلوک دیگه معتبر نیست
            continue
        selected = dbmod.get_student_course_group(user_id, group["course_id"])
        if selected == group_id:
            visible.append(e)
        elif selected is None and e["course"] not in seen_missing:
            missing.append(e["course"])
            seen_missing.add(e["course"])
        # اگه selected یه گروهِ دیگه‌ست، این بلوک قطعاً مالِ این دانشجو نیست --
        # بی‌صدا حذف می‌شه (نه warning، چون واقعاً بهش ربطی نداره).
    return visible, missing


def minutes_before(time_str: str, minutes: int) -> str:
    h, m = map(int, time_str.split(":"))
    total = (h * 60 + m - minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def course_exists(program: str, course_name: str) -> bool:
    data = load_data()
    return course_name in data.get(program, {})


# ============================================================
# برنامه کلاس‌ها (رسمی): کوتاه‌سازیِ callback_data
# ------------------------------------------------------------
# تلگرام برای callback_data حداکثر ۶۴ **بایت** UTF-8 قبول می‌کنه (نه کاراکتر).
# چون حروف فارسی معمولاً هر کدوم ۲ بایت مصرف می‌کنن، گذاشتنِ نامِ کاملِ
# ترم + نامِ کاملِ درس مستقیم توی callback_data (که هرکدوم می‌تونه ده‌ها
# کاراکتر باشه) به‌سادگی از این سقف رد می‌شه -- و در اون حالت تلگرام دکمه رو
# نشون نمی‌ده یا کل کیبورد رو رد می‌کنه، بدون اینکه خطایی به چشمِ کاربر بیاد.
#
# راه‌حل: به‌جای اسمِ کامل، از id واقعیِ همون رکورد توی دیتابیس استفاده
# می‌کنیم (terms.id برای ترم، courses.id برای درس -- که dbmod از قبل نگه
# می‌داره). این idها همیشه اعداد کوتاهن، پس صرف‌نظر از طولِ اسمِ فارسیِ
# ترم/درس مشکلی پیش نمیاد. مطابقِ همین اصل برای هر callback جدیدِ این بخش.
# ============================================================

MAX_CALLBACK_DATA_BYTES = 64


def callback_data_byte_len(data: str) -> int:
    return len(data.encode("utf-8"))


def validate_callback_data(data: str, context_label: str = "") -> bool:
    """قبل از ساختِ هر InlineKeyboardButton توی بخشِ برنامه‌کلاسی صدا زده می‌شه.
    اگه طولِ بایتیِ callback_data از سقفِ ۶۴-بایتیِ تلگرام رد بشه، به‌جای ساختِ یه
    دکمه‌ی خراب (که تلگرام بی‌سروصدا ردش می‌کنه)، خطا رو واضح لاگ می‌کنیم --
    شاملِ خودِ callback و طولِ دقیقِ بایتیش -- تا در آینده منشأش سریع پیدا بشه.
    خروجی False یعنی این دکمه‌ی خاص نباید ساخته بشه؛ صدازننده باید همون یه دکمه
    رو ردش کنه، نه اینکه کلِ منو رو خراب کنه."""
    n = callback_data_byte_len(data)
    if n > MAX_CALLBACK_DATA_BYTES:
        logger.error(
            "callback_data too long: %d bytes (max %d) — data=%r context=%s",
            n,
            MAX_CALLBACK_DATA_BYTES,
            data,
            context_label or "?",
        )
        return False
    return True


async def edit_message_with_keyboard_safe(query, text: str, buttons: list, context_label: str = "") -> None:
    """مثلِ query.edit_message_text ولی اگه با وجودِ validate_callback_data تلگرام
    باز هم روی ساخت/ارسالِ این کیبورد خطا بده، exception پنهان نمی‌مونه -- لاگ
    می‌شه و یه پیامِ عمومیِ خطا (به‌جای کرشِ خاموشِ handler) به کاربر نشون داده
    می‌شه."""
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except (BadRequest, TelegramError) as e:
        logger.error("Telegram API error building/sending inline keyboard [%s]: %s", context_label or "?", e)
        try:
            await query.edit_message_text(
                "یه مشکلی توی نمایشِ این بخش پیش اومد. لطفاً دوباره امتحان کن یا با پشتیبانی تماس بگیر."
            )
        except Exception:
            pass


def term_id_for_program(program: str):
    """id ترم رو از روی اسمش برمی‌گردونه (یا None اگه پیدا نشد) -- برای گذاشتن
    توی callback_data به‌جای اسمِ کامل."""
    return dbmod.get_term_id_by_name(program)


def program_for_term_id(term_id):
    """برعکسِ term_id_for_program: اسمِ ترم رو از روی id برمی‌گردونه (یا None)."""
    if term_id is None:
        return None
    term = dbmod.get_term_by_id(term_id)
    return term["name"] if term else None


def resolve_course_id_for_class_entry(program: str, entry: dict):
    """course_id واقعیِ یه رکوردِ برنامه‌کلاسی رو برمی‌گردونه: اول فیلدِ
    ذخیره‌شده‌ی entry['course_id'] رو چک می‌کنه (رکوردهای جدید یا migrate‌شده)،
    وگرنه با program + نامِ درس توی دیتابیس جستجو می‌کنه (سازگاری با رکوردهای
    قدیمی که هنوز course_id ندارن). خروجی None یعنی درسِ متناظری توی دیتابیس
    نیست -- در این حالت خودِ برنامه‌کلاسی همچنان نمایش داده می‌شه، فقط دکمه‌ی
    منابع برای همون یه کلاس ساخته نمی‌شه."""
    cid = entry.get("course_id")
    if isinstance(cid, int):
        return cid
    cid = dbmod.get_course_id_by_name(program, entry.get("course", ""))
    if cid is not None:
        return cid
    # نه course_id ذخیره‌شده‌ای هست، نه اسمِ کلاس با هیچ درسی مچ می‌شه (مثلاً
    # برچسبِ عمومیِ «کورس» برای کلاس‌های چرخشیِ فیزیوپات که اسمِ دقیقِ درسِ
    # جاری از قبل معلوم نیست) -- فال‌بک به کورسی که ادمین همین الان به‌عنوانِ
    # کورسِ فعالِ این ترم علامت زده (اگه چنین چیزی تعریف شده باشه).
    return dbmod.get_active_course_id(program)


def migrate_class_schedule_course_ids() -> None:
    """Migration امن و idempotent: به رکوردهای قدیمیِ class_schedule.json که فقط
    نامِ درس دارن (بدونِ course_id)، در صورتِ پیداشدنِ درسِ متناظر توی دیتابیس،
    فیلدِ course_id رو اضافه می‌کنه -- تا دفعاتِ بعد نیازی به جستجوی نام‌محور
    نباشه. هیچ رکورد/روز/ترمی حذف یا بازنویسیِ ساختاری نمی‌شه؛ فقط یه فیلدِ
    اختیاریِ جدید کنارِ فیلدهای قبلی اضافه می‌شه. قبل از هر نوشتنی، از فایلِ
    اصلی یه نسخه‌ی backup می‌گیره؛ اگه backup موفق نشه، اصلاً چیزی نوشته
    نمی‌شه."""
    if not os.path.exists(CLASS_SCHEDULE_FILE):
        return
    try:
        schedule = load_class_schedule()
    except Exception as e:
        logger.error("migrate_class_schedule_course_ids: failed to load class_schedule.json: %s", e)
        return

    changed = False
    for program, days in schedule.items():
        if not isinstance(days, dict):
            continue
        for day, classes in days.items():
            if not isinstance(classes, list):
                continue
            for entry in classes:
                if not isinstance(entry, dict) or "course_id" in entry:
                    continue
                cid = dbmod.get_course_id_by_name(program, entry.get("course", ""))
                if cid is not None:
                    entry["course_id"] = cid
                    changed = True

    if not changed:
        return

    backup_path = CLASS_SCHEDULE_FILE + f".bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        with open(CLASS_SCHEDULE_FILE, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(original)
    except Exception as e:
        logger.error("migrate_class_schedule_course_ids: backup failed, aborting migration: %s", e)
        return

    try:
        save_class_schedule(schedule)
        logger.info(
            "migrate_class_schedule_course_ids: course_id added to existing records (backup saved to %s)",
            backup_path,
        )
    except Exception as e:
        logger.error("migrate_class_schedule_course_ids: save failed after backup: %s", e)


def load_reminder_log() -> dict:
    if not os.path.exists(REMINDER_LOG_FILE):
        return {"date": "", "sent": []}
    with open(REMINDER_LOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_reminder_log(log: dict) -> None:
    _atomic_json_dump(REMINDER_LOG_FILE, log)


def is_active_hours() -> bool:
    # استفاده از timezone ثابت ایران؛ وابسته به ساعت سیستم سرور (مثل UTC) نباشد
    hour = now_tehran().hour
    if ACTIVE_HOURS_START <= ACTIVE_HOURS_END:
        return ACTIVE_HOURS_START <= hour < ACTIVE_HOURS_END
    return hour >= ACTIVE_HOURS_START or hour < ACTIVE_HOURS_END


async def broadcast_new_file(
    context: ContextTypes.DEFAULT_TYPE, program: str, course_name: str, category: str, caption: str
) -> None:
    """program همون term_name دقیقیه که کاربر موقعِ انتخاب ترم ذخیره می‌کنه، پس فقط
    کاربرانی که «فایل جدید» رو روشن دارن و ترمشون همینه خبردار می‌شن."""
    recipients = dbmod.get_users_for_notify("new_files", class_program=program)
    text = (
        f"📥 فایل جدید اضافه شد!\n{program} - {course_name} - {category}\n"
        f"«{caption}»\n\nبرای دریافت: {MENU_COURSES}"
    )
    for chat_id in recipients:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            pass


async def _broadcast_lifecycle_message(app: Application, text: str, label: str, recipients) -> None:
    """پیام کوتاهِ روشن/خاموش شدنِ ربات رو به لیستِ گیرندگانِ داده‌شده، به‌صورت بی‌صدا
    (زنگوله، بدون نوتیفِ کامل) می‌فرسته. recipients توسط تابعِ صداکننده مشخص می‌شه
    (نگاه کن به notify_startup / notify_shutdown)."""
    recipients = list(recipients)
    if not recipients:
        logger.info("هیچ گیرنده‌ای برای پیامِ %s وجود نداره.", label)
        return
    sent = failed = 0
    for uid in recipients:
        try:
            await app.bot.send_message(chat_id=int(uid), text=text, disable_notification=True)
            sent += 1
        except Exception:
            failed += 1
        # فاصله‌ی خیلی کوتاه بین پیام‌ها فقط برای رعایتِ محدودیتِ نرخِ ارسالِ
        # خودِ تلگرام (نه محدودیتِ دفعاتِ روشن/خاموش‌شدن) -- در تعداد کمِ کاربر عملاً حس نمی‌شه.
        await asyncio.sleep(0.05)
    logger.info("پیامِ %s برای %s کاربر ارسال شد (%s ناموفق).", label, sent, failed)


async def notify_startup(app: Application) -> None:
    """هر بار که ربات روشن می‌شه (استارت یا ری‌استارت)، فقط به کاربرانی که دسته‌ی
    «🟢 روشن‌شدنِ ربات» رو از منوی اطلاع‌رسانی روشن دارن یه پیامِ بی‌صدا می‌ده -- این
    دسته مستقل از ترمه (سراسریه). طبق خواسته، بدون محدودیت/throttle روی تعداد
    دفعات روشن‌شدن اجرا می‌شه."""
    recipients = dbmod.get_users_for_notify("startup")
    await _broadcast_lifecycle_message(
        app, "🟢 ربات روشن شد و آماده‌ی استفاده‌ست!", "روشن‌شدنِ ربات", recipients
    )


async def notify_shutdown(app: Application) -> None:
    """موقعِ خاموش‌شدنِ کنترل‌شده‌ی ربات (مثلاً Ctrl+C یا systemctl stop، یعنی
    سیگنال‌های SIGINT/SIGTERM که PTB به‌صورت خودکار موقع run_polling می‌گیره)،
    یه پیامِ بی‌صدا برای همه‌ی کاربرانِ جدولِ users می‌فرسته (این یکی تغییر نکرده --
    فقط پیامِ روشن‌شدن به اطلاع‌رسانیِ اختیاری منتقل شد). توجه: این فقط خاموشیِ
    عادی/کنترل‌شده رو پوشش می‌ده -- در کرشِ ناگهانی (kill -9، قطعِ برق و مثل اینا) کد
    اصلاً فرصتِ اجرا پیدا نمی‌کنه و این پیام فرستاده نمی‌شه."""
    users = dbmod.load_users()
    await _broadcast_lifecycle_message(app, "🔴 ربات خاموش شد.", "خاموش‌شدنِ ربات", users.keys())


def can_connect_directly() -> bool:
    try:
        with httpx.Client(timeout=CONNECTION_TEST_TIMEOUT, trust_env=False) as client:
            response = client.get("https://api.telegram.org")
            return response.status_code is not None
    except httpx.HTTPError:
        return False


# ============================================================
# لقب انتخابی و وضعیت کاربر (عضویت کانال، لقب و...)
# ============================================================

def load_users() -> dict:
    return dbmod.load_users()


def save_users(users: dict) -> None:
    dbmod.save_users(users)


def get_display_name(user_id: int, fallback_name: str) -> str:
    users = load_users()
    nickname = users.get(str(user_id), {}).get("nickname")
    display = nickname or fallback_name
    # حامی‌های تأییدشده یک ستاره کنار نام‌شان دارند.
    if dbmod.is_supporter(user_id):
        display += " ⭐"
    return display


def get_saved_fids(user_id: int) -> list:
    users = load_users()
    return users.get(str(user_id), {}).get("saved_fids", [])


def toggle_saved_fid(user_id: int, fid: str) -> bool:
    """fid رو به علاقه‌مندی‌های کاربر اضافه یا ازش حذف می‌کنه. خروجی: بعد از این عملیات ذخیره شده یا نه."""
    users = load_users()
    entry = users.setdefault(str(user_id), {})
    saved = entry.setdefault("saved_fids", [])
    if fid in saved:
        saved.remove(fid)
        is_saved = False
    else:
        saved.append(fid)
        is_saved = True
    save_users(users)
    return is_saved


def has_joined_channel(user_id: int) -> bool:
    users = load_users()
    return bool(users.get(str(user_id), {}).get("joined_channel"))


def mark_joined_channel(user_id: int) -> None:
    users = load_users()
    entry = users.setdefault(str(user_id), {})
    entry["joined_channel"] = True
    save_users(users)


def unmark_joined_channel(user_id: int) -> None:
    """وقتی می‌فهمیم کاربر از کانال لفت داده (via ChatMemberHandler)، فلگِ
    joined_channel رو برمی‌گردونیم False تا گیتِ عضویت دوباره جلوش رو بگیره."""
    users = load_users()
    entry = users.setdefault(str(user_id), {})
    entry["joined_channel"] = False
    save_users(users)


NICKNAME_PROMPT_TEXT = (
    "برای تجربه‌ی شخصی‌تر MedVerse می‌تونی یه لقب برای خودت انتخاب کنی.\n"
    "این لقب می‌تونه هر چیزی باشه و نیازی به اسم واقعیت نیست."
)


def nickname_buttons(with_skip: bool = True) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("Future Doctor", callback_data="pick_nick:Future Doctor")],
        [InlineKeyboardButton("MedStudent", callback_data="pick_nick:MedStudent")],
        [InlineKeyboardButton("✏️ لقب دلخواه", callback_data="pick_nick_custom")],
    ]
    if with_skip:
        buttons.append([InlineKeyboardButton("رد کردن", callback_data="pick_nick_skip")])
    return InlineKeyboardMarkup(buttons)


async def send_welcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, first_name: str) -> None:
    display_name = get_display_name(user_id, first_name)
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"سلام {display_name} 👋🩺\n\n"
            "به کتابخونه‌ی دیجیتال MedVerse خوش اومدی!\n"
            "جزوه، پاور و نمونه‌سوال هر درس اینجاست، یه دکمه باهاش فاصله داری.\n\n"
            f"از منوی پایین شروع کن، یا بزن {MENU_COURSES}"
        ),
        reply_markup=main_menu_keyboard(),
    )


async def send_join_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    buttons = [
        [InlineKeyboardButton("📢 عضویت در کانال", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="check_membership")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "برای استفاده از ربات، اول باید توی کانال ما عضو باشی:\n"
            f"{CHANNEL_LINK}\n\n"
            "بعد از عضویت، دکمه‌ی «✅ عضو شدم» رو بزن."
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def proceed_after_join(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, first_name: str) -> None:
    """بعد از تأیید عضویت کانال، یا پرسیدن لقب (بار اول) یا خوش‌آمدگویی عادی."""
    users = load_users()
    entry = users.get(str(user_id), {})
    if not entry.get("nickname_asked"):
        users.setdefault(str(user_id), {})["nickname_asked"] = True
        save_users(users)
        await context.bot.send_message(chat_id=chat_id, text=NICKNAME_PROMPT_TEXT, reply_markup=nickname_buttons())
        return
    await send_welcome_or_ask_program(context, chat_id, user_id, first_name)


ONBOARD_PROGRAM_TEXT = (
    "آخرین قدم: ترمت رو انتخاب کن 🎓\n\n"
    "این فقط برای اینه که برنامه‌ی کلاس/امتحان و اطلاع‌رسانیِ گپ امتحانی رو مستقیم "
    "مالِ ترم خودت نشونت بدیم؛ به درس‌ها و گپ‌های همه‌ی ترم‌های دیگه هم بازم دسترسیِ "
    "کامل داری، و هر وقت خواستی از «📅 برنامه شخصی → 🔁 تغییر ترم» می‌تونی عوضش کنی."
)


async def prompt_onboarding_program(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    buttons = [[InlineKeyboardButton(p, callback_data=f"pick_start_program:{p}")] for p in get_program_names()]
    buttons.append([InlineKeyboardButton("⏭ فعلاً رد کن", callback_data="pick_start_program_skip")])
    await context.bot.send_message(chat_id=chat_id, text=ONBOARD_PROGRAM_TEXT, reply_markup=InlineKeyboardMarkup(buttons))


async def send_welcome_or_ask_program(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, first_name: str
) -> None:
    """قدمِ آخرِ آنبوردینگ (بعد از عضویتِ کانال و لقب): اگه کاربر هنوز ترمش رو
    انتخاب نکرده و قبلاً هم این سؤال ازش پرسیده نشده، اول می‌پرسه؛ وگرنه مستقیم
    می‌ره سراغ پیامِ خوش‌آمد. جواب به این سؤال کاملاً اختیاریه (دکمه‌ی رد‌کردن داره)
    و روی دسترسی به بقیه‌ی ترم‌ها هیچ محدودیتی نمی‌ذاره -- فقط پیش‌فرضِ برنامه/
    اطلاع‌رسانیِ ترمِ خودشه."""
    if get_user_class_program(user_id):
        await send_welcome(context, chat_id, user_id, first_name)
        return
    users = load_users()
    entry = users.get(str(user_id), {})
    if entry.get("program_asked"):
        await send_welcome(context, chat_id, user_id, first_name)
        return
    users.setdefault(str(user_id), {})["program_asked"] = True
    save_users(users)
    await prompt_onboarding_program(context, chat_id)


async def pick_start_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    set_user_class_program(update.effective_user.id, program)
    await query.edit_message_text(f"باشه، ترمت رو «{program}» ثبت کردم ✅ (هر وقت خواستی از «🔁 تغییر ترم» عوضش کن.)")
    await send_welcome(context, query.message.chat_id, update.effective_user.id, update.effective_user.first_name)


async def pick_start_program_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("باشه، فعلاً رد شد. هر وقت خواستی از «📅 برنامه شخصی → 🔁 تغییر ترم» انتخابش کن.")
    await send_welcome(context, query.message.chat_id, update.effective_user.id, update.effective_user.first_name)


async def check_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        is_member = member.status in ("member", "administrator", "creator")
    except Exception:
        # قبلاً اینجا به‌اشتباه is_member = True بود ("محافظه‌کارانه عبور داده شد")
        # که باعث می‌شد هر خطای موقتِ API (نه فقط عدم عضویت) کاربر رو بدونِ
        # عضویتِ واقعی رد کنه. باید fail-closed باشه: اگه نتونستیم عضویت رو
        # تأیید کنیم، دسترسی نمی‌دیم، نه اینکه پیش‌فرض رو "عضوه" بذاریم.
        logger.warning("خطا در بررسی عضویت کانال برای user_id=%s؛ دسترسی داده نشد.", user_id, exc_info=True)
        await query.answer("مشکلی توی بررسی عضویت پیش اومد، چند لحظه دیگه دوباره امتحان کن 🙏", show_alert=True)
        return

    if not is_member:
        await query.answer("هنوز عضو کانال نیستی 😅", show_alert=True)
        return

    await query.answer()
    mark_joined_channel(user_id)
    await query.edit_message_text("عضویتت تأیید شد ✅")

    pending_fid = context.user_data.pop("pending_file_deeplink", None)
    if pending_fid:
        if not await deliver_file_by_fid(context, query.message.chat_id, user_id, pending_fid):
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="این فایل پیدا نشد یا دیگه در دسترس نیست.",
            )
        return

    await proceed_after_join(context, query.message.chat_id, user_id, update.effective_user.first_name)


# ============================================================
# آمار استفاده (کاربران، بخش‌ها، درس‌های محبوب)
# ============================================================

def load_analytics() -> dict:
    if not os.path.exists(ANALYTICS_FILE):
        return {"users": [], "section_counts": {}, "course_counts": {}}
    with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
        analytics = json.load(f)
    analytics.setdefault("users", [])
    analytics.setdefault("section_counts", {})
    analytics.setdefault("course_counts", {})
    return analytics


def save_analytics(analytics: dict) -> None:
    with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
        json.dump(analytics, f, ensure_ascii=False, indent=2)


def track_user(user_id: int) -> None:
    analytics = load_analytics()
    if user_id not in analytics["users"]:
        analytics["users"].append(user_id)
        save_analytics(analytics)
    # last_seen برای معیارِ «کاربرِ فعالِ اخیر» توی /stats؛ چون قبلاً هیچ زمانی
    # ذخیره نمی‌شد، این تاریخچه از همین لحظه به بعد شکل می‌گیره (نه عقب‌تر).
    # یه UPDATE سبک روی همون ردیف، نه لود/سیوِ کاملِ جدولِ کاربران.
    dbmod.touch_last_seen(user_id, now_tehran().isoformat())


def track_section(section_key: str) -> None:
    analytics = load_analytics()
    analytics["section_counts"][section_key] = analytics["section_counts"].get(section_key, 0) + 1
    save_analytics(analytics)


def track_course_view(course_label: str) -> None:
    analytics = load_analytics()
    analytics["course_counts"][course_label] = analytics["course_counts"].get(course_label, 0) + 1
    save_analytics(analytics)


# ============================================================
# صف ارسال‌های دانشجویان که منتظر تأیید ادمین‌اند
# ============================================================

def load_pending() -> list:
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pending(pending: list) -> None:
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def next_pending_id(pending: list) -> int:
    return max((p["id"] for p in pending), default=0) + 1


def format_exam_submission(entry: dict) -> str:
    a = entry["answers"]
    lines = [
        f"🩺 تجربه امتحان «{entry['course']}» ({entry['term']})",
        f"👨‍⚕️ استاد: {a.get('teacher', '-')}",
        f"📅 تاریخ: {a.get('exam_date', '-')}",
        f"📚 منبع مطالعه: {a.get('source', '-')}",
        f"🎯 سطح امتحان: {a.get('level', '-')}",
        f"📝 نکات مهم: {a.get('notes', '-')}",
        f"💡 پیشنهاد به بعدی‌ها: {a.get('advice', '-')}",
    ]
    if not entry.get("anonymous"):
        lines.append(f"\n🙋 ارسال‌کننده: {entry.get('sender_display', '-')}")
    return "\n".join(lines)


async def notify_admin_pending(context: ContextTypes.DEFAULT_TYPE, entry: dict) -> None:
    if entry["type"] == "file":
        sender_line = "ناشناس 🙈" if entry["anonymous"] else entry["sender_display"]
        text = (
            "📥 ارسال جدید (فایل) برای بررسی\n"
            f"{entry['term']} - {entry['course']} - {entry['category']}\n"
            f"«{entry['caption']}»\n"
        )
        if entry.get("description"):
            text += f"📝 توضیح: {entry['description']}\n"
        text += f"فرستنده: {sender_line}"
    else:
        text = format_exam_submission(entry) + "\n\n⏳ در انتظار تأیید"

    buttons = [
        [
            InlineKeyboardButton("✅ تأیید", callback_data=f"approve_sub:{entry['id']}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject_sub:{entry['id']}"),
        ]
    ]
    if entry["type"] == "exam":
        # فاز ۱۶ -- دلیلِ آماده برای ردِ سریع‌ترِ تجربه‌هایی که ادب رو رعایت
        # نکردن (نگاه کن به هشدارِ ادب که تو submit_exam_start_form اضافه شده).
        buttons.append(
            [InlineKeyboardButton("🤬 ردِ سریع: الفاظِ نامناسب", callback_data=f"reject_sub_bad:{entry['id']}")]
        )
    # فقط به ادمین‌های مجاز این ترم اطلاع بده: سوپرادمین‌ها + ادمین همون ترم
    for admin_id in dbmod.get_admins_for_term(entry["term"]):
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass


# ============================================================
# صندوق «درخواست‌ها» -- وقتی کاربر چیزی رو پیدا نمی‌کنه
# ============================================================

def load_requests() -> list:
    if not os.path.exists(REQUESTS_FILE):
        return []
    with open(REQUESTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_requests(requests_list: list) -> None:
    with open(REQUESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(requests_list, f, ensure_ascii=False, indent=2)


def next_request_id(requests_list: list) -> int:
    return max((r["id"] for r in requests_list), default=0) + 1


async def submit_request_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_request"] = True
    await query.edit_message_text("چی رو دنبالش می‌گشتی و پیدا نکردی؟ بنویس تا بررسیش کنم.")


async def close_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    req_id = int(query.data.split(":", 1)[1])
    requests_list = [r for r in load_requests() if r["id"] != req_id]
    save_requests(requests_list)
    await query.edit_message_text("بررسی‌شده علامت خورد ✅")


async def requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    requests_list = load_requests()
    if not requests_list:
        await update.message.reply_text("درخواست بازی وجود نداره.")
        return
    text = "💡 درخواست‌های بازِ دانشجوها:\n\n"
    for r in requests_list:
        text += f"#{r['id']} از {r['sender_display']}:\n{r['text']}\n\n"
    await update.message.reply_text(text)


# ============================================================
# سوپرادمین: مدیریت ادمین‌ها (RBAC چندسطحی)
# ============================================================

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addadmin <user_id>            -> می‌کنتش SUPER_ADMIN
       /addadmin <user_id> <نام ترم>   -> می‌کنتش TERM_ADMIN همون ترم
    یه کاربر می‌تونه هم‌زمان ادمینِ چند ترمِ مختلف باشه -- اجرای چندبارهِ این دستور
    برای همون کاربر با ترم‌های متفاوت، هیچ‌کدوم از ادمینی‌های قبلی‌ش رو حذف نمی‌کنه.
    فقط SUPER_ADMIN می‌تونه این دستور رو بزنه."""
    if not is_super_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await update.message.reply_text(
            "فرمت درست:\n"
            "/addadmin USER_ID  -> سوپرادمین می‌کنتش\n"
            "/addadmin USER_ID نام‌ترم -> ادمین همون ترم می‌کنتش (بدون حذفِ ادمینی‌های قبلیِ کاربر)\n"
            "مثال: /addadmin 123456789 فیزیوپات ۱"
        )
        return
    target_id = int(parts[1])
    term_name = parts[2].strip() if len(parts) > 2 else None
    role = "TERM_ADMIN" if term_name else "SUPER_ADMIN"
    ok, msg = dbmod.add_admin(target_id, role, term_name)
    if not ok:
        await update.message.reply_text(f"❌ {msg}")
        return
    dbmod.log_action(
        update.effective_user.id, "ADD_ADMIN", term=term_name,
        detail=f"target={target_id} role={role}",
    )
    if role == "SUPER_ADMIN":
        await update.message.reply_text(f"✅ کاربر {target_id} حالا سوپرادمینه.")
    else:
        other_terms = [t for t in dbmod.allowed_terms_for(target_id) if t != term_name]
        extra = f"\nترم‌های دیگه‌ش دست‌نخورده موند: {'، '.join(other_terms)}" if other_terms else ""
        await update.message.reply_text(f"✅ کاربر {target_id} حالا (هم) ادمینِ «{term_name}»ـه.{extra}")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🎉 شما به‌عنوان "
                + ("سوپرادمین" if role == "SUPER_ADMIN" else f"ادمینِ «{term_name}»")
                + " ربات MedVerse تعیین شدید."
            ),
        )
    except Exception:
        pass


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removeadmin USER_ID              -> اگه کاربر فقط یه نقش داره همونو حذف می‌کنه
       /removeadmin USER_ID نام‌ترم        -> فقط ادمینیِ همون ترم رو حذف می‌کنه
       /removeadmin USER_ID سوپرادمین      -> فقط نقشِ سوپرادمین رو حذف می‌کنه
    حذفِ یه نقش، به بقیه‌ی نقش‌های (ترم‌های) دیگه‌ی همون کاربر دست نمی‌زنه."""
    if not is_super_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await update.message.reply_text(
            "فرمت درست:\n"
            "/removeadmin USER_ID -> اگه کاربر فقط یه نقش داره همونو حذف می‌کنه\n"
            "/removeadmin USER_ID نام‌ترم -> فقط ادمینیِ همون ترم رو حذف می‌کنه\n"
            "/removeadmin USER_ID سوپرادمین -> فقط نقشِ سوپرادمین رو حذف می‌کنه"
        )
        return
    target_id = int(parts[1])
    term_arg = parts[2].strip() if len(parts) > 2 else None
    roles = dbmod.get_admin_roles(target_id)
    if not roles:
        await update.message.reply_text("این کاربر اصلاً ادمین نبود.")
        return

    if term_arg:
        if term_arg in ("سوپرادمین", "super", "SUPER_ADMIN"):
            removed = dbmod.remove_admin(target_id, role="SUPER_ADMIN")
            label = "سوپرادمین"
        else:
            removed = dbmod.remove_admin(target_id, term_name=term_arg)
            label = f"«{term_arg}»"
        if removed:
            dbmod.log_action(update.effective_user.id, "REMOVE_ADMIN", term=term_arg, detail=f"target={target_id}")
            remaining = dbmod.allowed_terms_for(target_id)
            extra = f"\nبقیه‌ی دسترسی‌های کاربر دست‌نخورده موند: {'، '.join(remaining)}" if remaining else ""
            await update.message.reply_text(f"✅ دسترسیِ {label} کاربر {target_id} حذف شد.{extra}")
        else:
            await update.message.reply_text(f"کاربر {target_id} چنین دسترسی‌ای نداشت.")
        return

    if len(roles) > 1:
        labels = ["سوپرادمین" if r["role"] == "SUPER_ADMIN" else r["term"] for r in roles]
        await update.message.reply_text(
            "این کاربر چند نقش هم‌زمان داره؛ باید مشخص کنی کدوم حذف بشه:\n"
            "/removeadmin USER_ID نام‌ترم  (یا «سوپرادمین»)\n\n"
            "نقش‌های فعلیش: " + "، ".join(labels)
        )
        return

    only = roles[0]
    if only["role"] == "SUPER_ADMIN":
        dbmod.remove_admin(target_id, role="SUPER_ADMIN")
    else:
        dbmod.remove_admin(target_id, term_name=only["term"])
    dbmod.log_action(update.effective_user.id, "REMOVE_ADMIN", term=only["term"], detail=f"target={target_id}")
    await update.message.reply_text(f"✅ دسترسی ادمین کاربر {target_id} حذف شد.")


async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هر ردیف یک نقش/ترمه -- اگه کاربری هم‌زمان ادمینِ چند ترم باشه، همون تعداد
    بار (یه بار به‌ازای هر ترم) توی این لیست دیده می‌شه."""
    if not is_super_admin(update.effective_user.id):
        return
    admins = dbmod.list_admins()
    if not admins:
        await update.message.reply_text("هیچ ادمینی ثبت نشده.")
        return
    by_user = {}
    for a in admins:
        by_user.setdefault(a["user_id"], []).append(a)
    lines = ["👑 لیست ادمین‌ها:\n"]
    for user_id, rows in by_user.items():
        labels = ["سوپرادمین" if r["role"] == "SUPER_ADMIN" else f"«{r['term']}»" for r in rows]
        icon = "👑" if any(r["role"] == "SUPER_ADMIN" for r in rows) else "👤"
        lines.append(f"{icon} {user_id} -- {' + '.join(labels)}")
    await update.message.reply_text("\n".join(lines))


async def auditlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/auditlog          -> سوپرادمین: ۲۰ فعالیت آخر همه‌ی ترم‌ها؛ Term Adminِ
                              تک‌ترمی: لاگِ همون ترم؛ Term Adminِ چند-ترمی: باید
                              مشخص کنه کدوم ترم رو می‌خواد
       /auditlog نام‌ترم   -> فعالیت‌های همون ترم (فقط اگه اجازه‌ی مدیریتش رو داشته باشه)"""
    user_id = update.effective_user.id
    roles = dbmod.get_admin_roles(user_id)
    if not roles:
        return
    is_super = any(r["role"] == "SUPER_ADMIN" for r in roles)
    parts = (update.message.text or "").split(maxsplit=1)
    term_filter = parts[1].strip() if len(parts) > 1 else None

    if not is_super:
        my_terms = [r["term"] for r in roles if r["role"] == "TERM_ADMIN" and r["term"]]
        if term_filter:
            if term_filter not in my_terms:
                await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{term_filter}» رو ندارید.")
                return
        elif len(my_terms) == 1:
            term_filter = my_terms[0]
        else:
            # ادمینِ چند ترمه و مشخص نکرده کدوم رو می‌خواد.
            await update.message.reply_text(
                "شما ادمینِ چند ترم هستید؛ لطفاً نام ترم رو هم بفرستید:\n"
                "/auditlog نام‌ترم\n\nترم‌های شما: " + "، ".join(my_terms)
            )
            return

    entries = dbmod.get_audit_log(limit=20, term=term_filter)
    if not entries:
        await update.message.reply_text("چیزی توی لاگ فعالیت‌ها نیست.")
        return
    lines = [f"📋 لاگ فعالیت‌ها{f' ({term_filter})' if term_filter else ''}:\n"]
    for e in entries:
        lines.append(
            f"[{e['created_at']}] admin={e['admin_id']} {e['action']}"
            f"{' term=' + e['term'] if e['term'] else ''}"
            f"{' -- ' + e['detail'] if e['detail'] else ''}"
        )
    await update.message.reply_text("\n".join(lines))


async def syncmembers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/syncmembers -- سوپرادمین: کاربرهایی که قبلاً joined_channel=True شدن رو
    یکی‌یکی با تلگرام چک می‌کنه؛ هرکی الان واقعاً عضو کانال نیست (یعنی از قبلِ
    فعال‌شدنِ ChatMemberHandler لفت داده بوده و بات هیچ‌وقت خبردار نشده)
    فلگش False می‌شه و پیامِ «لفت دادی» براش می‌ره -- دقیقاً همون رفتاری که
    برای لفت‌دادنِ از این‌به‌بعد داریم، ولی این‌بار برای گذشته."""
    if not is_super_admin(update.effective_user.id):
        return

    users = load_users()
    candidates = [
        (uid_str, entry) for uid_str, entry in users.items() if entry.get("joined_channel")
    ]
    if not candidates:
        await update.message.reply_text("هیچ کاربری با فلگِ عضویتِ کانال ثبت‌شده پیدا نشد.")
        return

    status_msg = await update.message.reply_text(
        f"⏳ در حال بررسیِ {len(candidates)} کاربر... این ممکنه چند دقیقه طول بکشه."
    )

    checked = 0
    left_count = 0
    notified_count = 0
    error_count = 0

    for uid_str, _entry in candidates:
        checked += 1
        try:
            user_id = int(uid_str)
        except ValueError:
            continue
        try:
            member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
            is_member_now = member.status in ("member", "administrator", "creator")
        except Exception:
            # اگه کاربر هیچ‌وقت با بات چت خصوصی نداشته یا هر خطای دیگه‌ای، فقط
            # می‌شمریمش و رد می‌شیم -- بدونِ اطلاعاتِ قطعی نباید فلگش رو دستکاری کنیم.
            error_count += 1
            await asyncio.sleep(0.05)
            continue

        if not is_member_now:
            left_count += 1
            unmark_joined_channel(user_id)
            try:
                buttons = [
                    [InlineKeyboardButton("📢 عضویت مجدد در کانال", url=CHANNEL_LINK)],
                    [InlineKeyboardButton("✅ عضو شدم", callback_data="check_membership")],
                ]
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "😕 متوجه شدیم که از کانالمون لفت دادی.\n\n"
                        "برای اینکه بتونی دوباره از امکانات ربات (دروس، فایل‌ها، برنامه‌ها و بقیه‌ی بخش‌ها) "
                        "استفاده کنی، باید دوباره عضو کانال بشی:\n"
                        f"{CHANNEL_LINK}\n\n"
                        "بعد از عضویت، دکمه‌ی «✅ عضو شدم» رو بزن."
                    ),
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                notified_count += 1
            except Exception:
                logger.info("نتونستیم به user_id=%s درباره‌ی لفت‌دادنِ قدیمی خبر بدیم.", user_id, exc_info=True)

        # برای رعایتِ محدودیتِ نرخِ API تلگرام، بین هر چک یه مکثِ کوچیک می‌ذاریم.
        await asyncio.sleep(0.05)

        if checked % 25 == 0:
            try:
                await status_msg.edit_text(
                    f"⏳ {checked}/{len(candidates)} بررسی شد... ({left_count} لفت‌داده تا الان)"
                )
            except Exception:
                pass

    await status_msg.edit_text(
        f"✅ بررسی تموم شد.\n\n"
        f"👥 کلِ کاربرانِ بررسی‌شده: {checked}\n"
        f"🚪 لفت‌داده (فلگشون ریست شد): {left_count}\n"
        f"📩 پیامِ خبررسانی موفق: {notified_count}\n"
        f"⚠️ خطا در بررسی (رد شدن): {error_count}"
    )
    dbmod.log_action(
        update.effective_user.id, "SYNC_MEMBERSHIP",
        detail=f"checked={checked} left={left_count} notified={notified_count} errors={error_count}",
    )


# ============================================================
# دستورهای عمومی
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    track_user(user_id)

    # payload را قبل از گیت عضویت نگه می‌داریم تا اگر کاربر ابتدا مجبور شد عضو کانال شود،
    # بعد از زدن «عضو شدم» همان فایل برایش باز شود و لینک گم نشود.
    payload = context.args[0].strip() if context.args else ""
    if payload.startswith("file_"):
        context.user_data["pending_file_deeplink"] = payload[5:].strip()

    if not has_joined_channel(user_id):
        await send_join_prompt(context, update.effective_chat.id)
        return

    if payload.startswith("file_"):
        fid = payload[5:].strip()
        context.user_data.pop("pending_file_deeplink", None)
        if not fid or not await deliver_file_by_fid(context, update.effective_chat.id, user_id, fid):
            await update.message.reply_text("این فایل پیدا نشد یا دیگه در دسترس نیست.")
        return

    users = load_users()
    entry = users.get(str(user_id), {})
    if not entry.get("nickname_asked"):
        users.setdefault(str(user_id), {})["nickname_asked"] = True
        save_users(users)
        await update.message.reply_text(NICKNAME_PROMPT_TEXT, reply_markup=nickname_buttons())
        return

    await send_welcome_or_ask_program(context, update.effective_chat.id, user_id, update.effective_user.first_name)


async def set_nickname_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("لقب جدیدت رو انتخاب کن:", reply_markup=nickname_buttons(with_skip=False))


async def pick_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    nickname = query.data.split(":", 1)[1]
    user_id = update.effective_user.id
    users = load_users()
    entry = users.setdefault(str(user_id), {})
    entry["nickname"] = nickname
    entry["nickname_asked"] = True
    save_users(users)
    await query.edit_message_text(f"باشه، از این به بعد صدات می‌کنم: {nickname} 😊")
    await send_welcome_or_ask_program(context, query.message.chat_id, user_id, update.effective_user.first_name)


async def pick_nickname_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_nickname"] = True
    await query.edit_message_text("باشه، لقب دلخواهت رو بنویس و بفرست:")


async def pick_nickname_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    users = load_users()
    entry = users.setdefault(str(user_id), {})
    entry["nickname"] = None
    entry["nickname_asked"] = True
    save_users(users)
    await query.edit_message_text("باشه، مشکلی نیست 🙂")
    await send_welcome_or_ask_program(context, query.message.chat_id, user_id, update.effective_user.first_name)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    n_courses = sum(len(data.get(p, {})) for p in get_program_names())
    n_files = sum(
        len(cat.get("files", []))
        for p in get_program_names()
        for c in data.get(p, {}).values()
        for cat in c.get("categories", {}).values()
    )
    buttons = [[InlineKeyboardButton("📖 راهنمای استفاده از بات", callback_data="usage_guide")]]
    await update.message.reply_text(
        "ℹ️ درباره MedVerse\n\n"
        "MedVerse یه کتابخونه‌ی دیجیتال برای دانشجوهای پزشکیه؛ جایی برای دسترسی راحت به "
        "جزوه‌ها، پاورپوینت‌ها، نمونه‌سؤال‌ها، نکات امتحانی و تجربه‌های دانشجوها 📚🩺\n"
        "اینجا می‌تونی منابع درسی رو پیدا کنی، فایل‌های مفیدت رو با بقیه به اشتراک بذاری، "
        "تجربه‌ی امتحانت رو ثبت کنی و حتی برای مطالعه‌ات برنامه‌ریزی کنی.\n"
        "با هم MedVerse رو کامل‌تر می‌کنیم 🤍\n\n"
        f"در حال حاضر: {len(get_program_names())} برنامه، {n_courses} درس، {n_files} فایل\n\n"
        f"برای خبردار شدن از فایل‌های جدید، دکمه‌ی «{MENU_NOTIFY}» رو بزن."
        "\n\nبرای راهنمای کامل استفاده از بات، دکمه‌ی زیر رو بزن 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def usage_guide_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فایل ثابتِ «راهنمای استفاده از بات» رو می‌فرسته (نگاه کن به USAGE_GUIDE_FILE)."""
    query = update.callback_query
    await query.answer()
    if not os.path.exists(USAGE_GUIDE_FILE):
        await query.message.reply_text(
            "⚠️ فایل راهنما هنوز روی سرور آپلود نشده. بعداً دوباره امتحان کن."
        )
        return
    with open(USAGE_GUIDE_FILE, "rb") as f:
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=f,
            filename=os.path.basename(USAGE_GUIDE_FILE),
            caption="📖 راهنمای استفاده از بات MedVerse",
        )


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text

    if text == MENU_COURSES:
        track_section("courses")
        await list_programs(update, context)
        return

    if text == MENU_ABOUT:
        track_section("about")
        await about_command(update, context)
        return

    if text == MENU_NOTIFY:
        track_section("notify")
        user_id = update.effective_user.id
        await update.message.reply_text(
            build_notify_menu_text(user_id), reply_markup=build_notify_menu_markup(user_id)
        )
        return

    if text == MENU_PARTICIPATE:
        track_section("participate")
        buttons = [
            [InlineKeyboardButton("📝 ارسال تجربه امتحان", callback_data="submit_exam_start")],
            [InlineKeyboardButton("📚 ارسال فایل و خلاصه", callback_data="submit_file_start")],
            [InlineKeyboardButton("💡 درخواست موردی که نیست", callback_data="submit_request_start")],
        ]
        await update.message.reply_text(
            "📩 مشارکت دانشجویان\nیکی رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if text == MENU_SCHEDULE:
        track_section("schedule")
        buttons = [
            [InlineKeyboardButton("🏫 برنامه کلاس‌ها (رسمی)", callback_data="class_schedule_menu")],
            [InlineKeyboardButton("📝 برنامه امتحانات", callback_data="exam_schedule_menu")],
            [InlineKeyboardButton("📖 برنامه مطالعه شخصی", callback_data="personal_schedule_menu")],
        ]
        await update.message.reply_text("📅 برنامه‌ریزی\nکدوم بخش؟", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if text == MENU_CHAT:
        track_section("chat")
        buttons = [
            [InlineKeyboardButton("📚 گپ امتحانی", callback_data="chat_exam_root")],
            [InlineKeyboardButton("☕ گپ دوستانه", callback_data="chat_casual_start")],
        ]
        await update.message.reply_text(
            "💬 گپ دانشجویی\nیکی رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if text == MENU_SUPPORT:
        track_section("support")
        user_id = update.effective_user.id
        supporter = dbmod.is_supporter(user_id)
        if supporter:
            text_msg = (
                "⭐ حامی MedVerse\n\n"
                "ممنون که برای روشن موندن بات و ادامه‌ی سرویس به دانشجوها کنارمون بودی. "
                "نشان حامی کنار اسمت فعاله. اگر خواستی دوباره هم می‌تونی از MedVerse حمایت کنی. 💛"
            )
        else:
            text_msg = (
                "🤍 حمایت مالی از MedVerse\n\n"
                "برای تأمین هزینه‌ی سرور و روشن نگه داشتن بات، حمایت شما کاملاً داوطلبانه است.\n\n"
                "مبلغ دلخواهت رو از گزینه‌های زیر انتخاب کن؛ پرداخت از طریق زرین‌پال انجام می‌شه و "
                "بعد از تأیید موفق، نشان حامی به‌صورت خودکار برای حسابت فعال می‌شه. 🌱"
            )

        buttons = []
        if payment.is_configured():
            amount_buttons = []
            for label, amount_rial in payment.SUPPORT_AMOUNTS:
                amount_buttons.append(
                    InlineKeyboardButton(
                        label,
                        url=payment.build_personalized_start_url(user_id, amount_rial),
                    )
                )
            # دو دکمه در هر ردیف تا روی موبایل فشرده و خوانا بماند.
            for i in range(0, len(amount_buttons), 2):
                buttons.append(amount_buttons[i:i + 2])
        else:
            # سازگاری عقب‌رو: تا وقتی زرین‌پال تنظیم نشده، لینک دستی قدیمی همچنان کار می‌کند.
            support_link = dbmod.get_support_link()
            if support_link:
                buttons.append([InlineKeyboardButton("🤍 حمایت از MedVerse", url=support_link)])
            else:
                text_msg += (
                    "\n\n✅ در حال حاضر درگاه/لینک حمایت فعال نیست؛ یعنی فراخوان فعلی "
                    "پایان یافته یا موقتاً بسته شده."
                )

        await update.message.reply_text(
            text_msg, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )
        return

    if text == MENU_STUDY_GROUP:
        track_section("study_group")
        await update.message.reply_text(
            "👥 مطالعه گروهی\n\n"
            "به اتاق مطالعه‌ی MedVerse توی اپلیکیشن درسیتا بپیوند و همزمان با بقیه‌ی دانشجوها درس بخون:\n"
            f"{STUDY_ROOM_LINK}"
        )
        return


async def on_channel_membership_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """آپدیتِ chat_member رو از کانال می‌گیره تا بفهمیم کاربری لفت داده/بن شده.
    قبلاً has_joined_channel فقط یه فلگِ یک‌بارمصرف بود که هیچ‌وقت دوباره چک
    نمی‌شد؛ یعنی کسی که بعد از عضویت لفت می‌داد، همچنان از دیدِ بات "عضو" بود
    و امکانات رو در اختیار داشت. اینجا با گوش‌دادن به تغییرِ وضعیتِ عضویت توی
    خودِ کانال، هم فلگش رو برمی‌گردونیم False و هم بهش خبر می‌دیم."""
    cmu = update.chat_member
    if cmu is None:
        return

    chat = cmu.chat
    channel_uname = CHANNEL_USERNAME.lstrip("@").lower()
    if (chat.username or "").lower() != channel_uname:
        return  # مربوط به یه چت/کانال دیگه‌ست، کاری بهش نداریم

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    was_member = old_status in ("member", "administrator", "creator")
    is_member_now = new_status in ("member", "administrator", "creator")

    if not (was_member and not is_member_now):
        return  # فقط لحظه‌ی "لفت‌دادن/حذف‌شدن" برامون مهمه

    user = cmu.new_chat_member.user
    if user.is_bot:
        return

    was_flagged = has_joined_channel(user.id)
    unmark_joined_channel(user.id)
    if not was_flagged:
        return  # اصلاً از دیدِ بات عضو نبوده (یا هیچ‌وقت /start نزده)، پیامی لازم نیست

    try:
        buttons = [
            [InlineKeyboardButton("📢 عضویت مجدد در کانال", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ عضو شدم", callback_data="check_membership")],
        ]
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "😕 متوجه شدیم که از کانالمون لفت دادی.\n\n"
                "برای اینکه بتونی دوباره از امکانات ربات (دروس، فایل‌ها، برنامه‌ها و بقیه‌ی بخش‌ها) "
                "استفاده کنی، باید دوباره عضو کانال بشی:\n"
                f"{CHANNEL_LINK}\n\n"
                "بعد از عضویت، دکمه‌ی «✅ عضو شدم» رو بزن."
            ),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        # مثلاً کاربر قبلاً بات رو بلاک کرده یا هیچ‌وقت چت خصوصی باهاش شروع نشده؛
        # مهم نیست پیام نره، مهم اینه فلگش False شد و گیتِ عضویت جلوش رو می‌گیره.
        logger.info("نتونستیم به user_id=%s درباره‌ی لفت‌دادن از کانال پیام بدیم.", user.id, exc_info=True)


# ============================================================
# گیت‌های ورودی: عضویت کانال + ساعت فعالیت (باید قبل از همه اجرا بشن)
# ============================================================

async def offline_gate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        track_user(user.id)

    if user and is_admin(user.id):
        return

    # /start خودش گیت عضویت رو مدیریت می‌کنه، نباید اینجا بلاک بشه
    if update.message and update.message.text and update.message.text.startswith("/start"):
        return

    if user and not has_joined_channel(user.id):
        await send_join_prompt(context, update.effective_chat.id)
        raise ApplicationHandlerStop

    if is_active_hours():
        return

    if update.message:
        await update.message.reply_text(OFFLINE_MESSAGE)
    raise ApplicationHandlerStop


async def offline_gate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    query = update.callback_query

    if user and is_admin(user.id):
        return

    if query and query.data == "check_membership":
        return

    if user and not has_joined_channel(user.id):
        await query.answer("اول باید عضو کانال بشی 👆", show_alert=True)
        await send_join_prompt(context, query.message.chat_id)
        raise ApplicationHandlerStop

    if is_active_hours():
        return

    await query.answer(OFFLINE_MESSAGE, show_alert=True)
    raise ApplicationHandlerStop


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "دستورهای عمومی:\n/start - شروع\n/darsha - مرور برنامه‌ها و دروس\n"
        "/setnickname - تغییر لقب انتخابی‌ات\n"
        "/saved - فایل‌های ذخیره‌شده‌ی (علاقه‌مندی) تو\n"
    )
    if is_admin(update.effective_user.id):
        text += (
            "\nدستورهای ادمین:\n"
            "/addcourse - اضافه کردن یک درس جدید به یکی از برنامه‌ها\n"
            "/editcourse - ویرایش اسمِ یه درسِ موجود یا حذفِ کاملش\n"
            "/editcategories - ساخت/ویرایش‌نام/حذف/کپیِ دسته‌های یه درس (مستقل از درس‌های دیگه)\n"
            "/upload - آپلود فایل برای یه درس (اگه اون بخش گروه/پوشه داشته باشه، مقصد رو هم می‌پرسه)\n"
            "/filegroups - ساخت/مدیریت گروه‌ها یا پوشه‌های داخل یه بخش (نوت‌گذاری، ترتیب، تغییرنام و...)\n"
            "/addguide - افزودن/ویرایش راهنمای مطالعه‌ی یه درس\n"
            "/movefile - جابه‌جا کردن یک یا چند فایل بین دسته‌بندی‌ها (بدون حذف و آپلود دوباره)\n"
            "/rename - تغییر نام واقعی یه فایل روی تلگرام (بدون آپلود دستی مجدد)\n"
            "/editdesc - ویرایش توضیحِ (کپشنِ تلگرامیِ) یه فایل موجود\n"
            "/setbadge - نشان کیفیت برای همه‌ی ترم‌ها؛ مسیر کوتاه و ID-based (🏆/⭐️)\n"
            "/filelink - پیدا کردن و کپی لینک مستقیم هر فایل؛ همچنین دکمه 🔗 کنار فایل‌ها برای ادمین\n"
            "/delete - حذف یه فایل\n"
            "/requests - دیدن درخواست‌های ثبت‌شده‌ی دانشجوها\n"
            "/classschedule - مدیریت سریع برنامه کلاس‌ها؛ برای افزودن فقط «ساعت | نام درس» کافیه؛ بعد با دکمه‌های 👥 گروه و 📍 مکان تنظیمش کن\n"
            "  ↳ گروه سریع: داخل «👥 گروه» فقط اسم گروه را بده؛ روز و ساعت از خود کلاس برداشته می‌شود\n"
            "  ↳ مکان سریع: داخل «📍 مکان» فقط اسم مکان را بنویس\n"
            "  ↳ لغو سریع: «🚫 لغو جلسه بعد» با یک کلیک؛ برای تاریخ خاص از «📅 تاریخ دیگر» استفاده کن\n"
            "/examschedule - ثبت/حذف برنامه امتحانات\n"
            "/fixedevents - ثبت/حذف رویداد ثابت؛ یادآوری غذا و دوشنبه فرهنگی متن کوتاه اختصاصی دارند\n"
            "/chatadmin - نظارت بر گپ دانشجویی (گزارش‌ها، پیش‌نویس تجربه، بستنِ دوره)\n"
            "/auditlog - لاگ فعالیت‌های ادمین (ترمِ خودت)\n"
        )
    if is_super_admin(update.effective_user.id):
        text += (
            "\nدستورهای مخصوص سوپرادمین:\n"
            "/addsemester - ساخت ترم/برنامه‌ی جدید (مثلاً فیزیوپات ۲ یا یه ترم زیرِ علوم پایه)\n"
            "/editterm - ویرایش اسمِ یه ترمِ موجود یا حذفِ کاملش\n"
            "/editsections - ساخت بخشِ جدید (مثلاً کارآموزی/کارورزی) یا ویرایش‌نام/حذفِ یه بخشِ موجود\n"
            "/supportlink LINK - فعال/تغییر دادن لینک موقت حمایت مالی\n"
            "/supportoff - برداشتن لینک حمایت مالی بعد از تأمین هزینه\n"
            "/addsupporter USER_ID - دادن نشان ⭐ حامی به کاربر\n"
            "/removesupporter USER_ID - برداشتن نشان حامی از کاربر\n"
            "/backupserver - گرفتن بکاپ کامل و ارسال مستقیم فایل برای سوپرادمین\n"
            "/stats - آمار استفاده، دانلود و فایل‌های پرطرفدار\n"
            "/addadmin USER_ID [نام‌ترم] - افزودن سوپرادمین یا ادمین ترم\n"
            "/removeadmin USER_ID - حذف دسترسی ادمین یه کاربر\n"
            "/listadmins - لیست همه‌ی ادمین‌ها\n"
            "/syncmembers - بررسی و همگام‌سازیِ عضویتِ کاربران در کانال\n"
        )
    await update.message.reply_text(text)


# ============================================================
# سوپرادمین: بکاپ مستقیم داده‌های سرور
# ============================================================

async def backup_server_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """از دیتابیس و JSONهای Runtime یک بکاپ امن می‌سازد و مستقیم برای سوپرادمین می‌فرستد.

    فایل موقت بعد از ارسال پاک می‌شود؛ داده‌های اصلی داخل Volume هیچ تغییری نمی‌کنند.
    """
    if not is_super_admin(update.effective_user.id):
        return

    status = await update.message.reply_text("📦 در حال ساخت بکاپ امن از داده‌های فعلی سرور...")

    # روی Railway معمولاً این‌ها /data و /data/medverse.db هستند؛ روی VPS هم از
    # Environment Variableهای فعلی پروژه پیروی می‌کنیم تا دستور قابل‌حمل بماند.
    data_dir = MEDVERSE_DATA_DIR or os.path.dirname(dbmod.DB_FILE) or "."
    db_file = dbmod.DB_FILE

    if not os.path.exists(db_file):
        await status.edit_text(f"❌ فایل دیتابیس پیدا نشد:\n{db_file}")
        return

    import shutil
    import tarfile
    import tempfile

    temp_root = tempfile.mkdtemp(prefix="medverse_backup_")
    try:
        stamp = now_tehran().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(temp_root, f"medverse_backup_{stamp}")
        os.makedirs(backup_dir, exist_ok=True)

        # SQLite Backup API: حتی اگر بات هم‌زمان در حال نوشتن باشد، کپی سازگار می‌ماند.
        backupmod.backup_sqlite(db_file, os.path.join(backup_dir, "medverse.db"))

        copied = []
        for name in backupmod.RUNTIME_JSON_FILES:
            src = os.path.join(data_dir, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(backup_dir, name))
                copied.append(name)

        archive_path = os.path.join(temp_root, f"medverse_backup_{stamp}.tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(backup_dir, arcname=os.path.basename(backup_dir))

        size_mb = os.path.getsize(archive_path) / (1024 * 1024)
        caption = (
            "✅ بکاپ MedVerse آماده است.\n"
            f"🗄 دیتابیس: medverse.db\n"
            f"📄 JSONهای موجود: {len(copied)}\n"
            f"📦 حجم: {size_mb:.2f} MB\n\n"
            "این فایل را در یک جای امن نگه دار؛ برای انتقال سرور همین آرشیو لازم می‌شود."
        )

        with open(archive_path, "rb") as archive_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=archive_file,
                filename=os.path.basename(archive_path),
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )
        await status.edit_text("✅ بکاپ ساخته و ارسال شد. فایل بالا را روی کامپیوترت ذخیره کن.")
        dbmod.log_action(update.effective_user.id, "SERVER_BACKUP")
    except Exception as exc:
        logger.exception("ساخت/ارسال بکاپ سرور ناموفق بود")
        await status.edit_text(
            "❌ ساخت یا ارسال بکاپ ناموفق بود.\n"
            f"خطا: {type(exc).__name__}: {exc}"
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


# ============================================================
# سوپرادمین: حمایت مالی (لینک موقت + نشان حامی)
# ============================================================

async def support_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_super_admin(update.effective_user.id):
        return
    if not context.args:
        current = dbmod.get_support_link()
        await update.message.reply_text(
            f"لینک فعلی حمایت:\n{current}" if current else "در حال حاضر لینک حمایت مالی فعال نیست."
        )
        return
    url = context.args[0].strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        await update.message.reply_text("لینک باید با http:// یا https:// شروع بشه.")
        return
    dbmod.set_support_link(url)
    dbmod.log_action(update.effective_user.id, "SET_SUPPORT_LINK", detail=url)
    await update.message.reply_text("لینک موقت حمایت مالی فعال شد ✅")


async def support_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_super_admin(update.effective_user.id):
        return
    dbmod.set_support_link(None)
    dbmod.log_action(update.effective_user.id, "CLEAR_SUPPORT_LINK")
    await update.message.reply_text("لینک حمایت مالی برداشته شد ✅")


async def add_supporter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_super_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت: /addsupporter USER_ID")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID باید عددی باشه.")
        return
    dbmod.set_supporter(user_id, True)
    dbmod.log_action(update.effective_user.id, "ADD_SUPPORTER", detail=str(user_id))
    await update.message.reply_text(f"کاربر {user_id} به‌عنوان حامی ثبت شد ⭐")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⭐ ممنون از حمایتت از MedVerse! از این به بعد نشان حامی کنار اسمت نمایش داده می‌شه. 💛",
        )
    except Exception:
        pass


async def remove_supporter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_super_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت: /removesupporter USER_ID")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID باید عددی باشه.")
        return
    dbmod.set_supporter(user_id, False)
    dbmod.log_action(update.effective_user.id, "REMOVE_SUPPORTER", detail=str(user_id))
    await update.message.reply_text(f"نشان حامی از کاربر {user_id} برداشته شد.")


# ============================================================
# ادمین: ساخت ترم/برنامه جدید (فیزیوپات ۴، ترم ۶ و ...)
# ============================================================

async def add_semester_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ساخت ترم جدید یه عملیات ساختاریه -- فقط سوپرادمین می‌تونه انجامش بده
    if not is_super_admin(update.effective_user.id):
        return
    clear_admin_flow_state(context)
    sections = dbmod.get_groups_with_ids()
    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"addsem_grp:{s['id']}")] for s in sections]
    if not buttons:
        await update.message.reply_text(
            "هیچ بخشی توی سیستم نیست. اول با /editsections یه بخش بساز (مثلاً «علوم پایه»)."
        )
        return
    await update.message.reply_text(
        "ترم/برنامه‌ی جدید رو به کدوم بخش اضافه کنم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def pick_group_for_semester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بعد از /addsemester، ادمین اول بخش (مثلاً فیزیوپات یا علوم پایه) رو انتخاب می‌کنه.
    توجه: بخش‌ها دیگه هاردکد نیستن -- از دیتابیس (dbmod.get_groups_with_ids) خونده می‌شن،
    پس هر بخشی که با /editsections ساخته بشه همین‌جا هم قابل‌انتخابه."""
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    group_id = int(query.data.split(":", 1)[1])
    section = dbmod.get_group_by_id(group_id)
    if section is None:
        await query.edit_message_text("این بخش دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    group_name = section["name"]
    clear_admin_flow_state(context)
    context.user_data["awaiting_semester_group"] = group_name
    context.user_data["awaiting_semester_name"] = True
    await query.edit_message_text(
        f"باشه، بخش «{group_name}». حالا اسم ترم/برنامه‌ی جدید رو بنویس (مثلاً «فیزیوپات ۴» یا «ترم ۶»).\n"
        "ساختار خالی (دسته‌بندی‌های استاندارد) خودکار براش ساخته می‌شه و بعدش با /addcourse می‌تونی درس بهش اضافه کنی."
    )


# ============================================================
# ادمین: ساخت درس
# ============================================================

def _addcourse_prog_picker_text_markup(user_id: int):
    allowed = admin_allowed_programs(user_id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"pick_prog_addcourse:{p}")] for p in allowed]
    return "این درس رو به کدوم برنامه اضافه کنم؟", InlineKeyboardMarkup(buttons)


async def add_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    text, markup = _addcourse_prog_picker_text_markup(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=markup)


async def addcourse_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting_course_name_for_term", None)
    text, markup = _addcourse_prog_picker_text_markup(update.effective_user.id)
    await query.edit_message_text(text, reply_markup=markup)


async def pick_program_for_addcourse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_course_name_for_term"] = program
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="addc_back")]])
    await query.edit_message_text(
        f"باشه. حالا اسم درس جدید رو برای «{program}» بنویس و بفرست.", reply_markup=back_btn
    )


async def course_edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌ی «✏️ ویرایش نام» زیر پیام تأیید افزودن درس. از course_id استفاده می‌کنه
    (نه اسم) هم برای رعایتِ محدودیتِ ۶۴ بایتیِ callback_data تلگرام، هم برای این‌که
    عملیاتِ ویرایش بعداً با شناسه‌ی ثابت انجام بشه و ریسکِ گم‌شدن/تکرارِ فایل نداشته باشه."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_course_rename"] = {"course_id": course_id}
    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecourse_pick:{course_id}")]]
    )
    await query.edit_message_text(
        f"باشه، اسم جدید رو برای «{course['name']}» ({course['term_name']}) بنویس و بفرست.",
        reply_markup=back_btn,
    )


async def course_delete_confirm_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌ی «🗑 حذف درس» زیر پیام تأیید افزودن درس -- می‌ره سراغ تأیید نهایی."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    file_count = dbmod.count_course_files_by_id(course_id)
    warn = f"\n⚠️ این درس {file_count} فایل داره که همه‌شون هم پاک می‌شن." if file_count else ""
    buttons = [
        [
            InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"crs_del_yes:{course_id}"),
            InlineKeyboardButton("❌ نه، بی‌خیال", callback_data="crs_del_no"),
        ]
    ]
    await query.edit_message_text(
        f"⚠️ مطمئنی می‌خوای درس «{course['name']}» رو از «{course['term_name']}» حذف کنی؟{warn}\n"
        "همه‌ی فایل‌ها، راهنمای مطالعه و اطلاعات این درس هم پاک می‌شن. این کار برگشت‌ناپذیره.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def course_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.answer()
        await query.edit_message_text("این درس قبلاً حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    await query.answer()
    ok, course_name, term_name = dbmod.delete_course_by_id(course_id)
    if ok:
        _delete_course_schedule_references(term_name, course_id, course_name)
        dbmod.log_action(update.effective_user.id, "DELETE_COURSE", term=term_name, detail=course_name)
        await query.edit_message_text(f"درس «{course_name}» از «{term_name}» حذف شد ✅")
    else:
        await query.edit_message_text("این درس قبلاً حذف شده.")


async def course_delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("باشه، درس حذف نشد.")


# ============================================================
# ادمین: ویرایش/حذفِ هر درسِ موجود (نه فقط همون لحظه‌ی افزودن)
# ============================================================

async def editcourse_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/editcourse -- اگه اسم درسی رو اشتباه وارد کردی یا می‌خوای حذفش کنی."""
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ecourse_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "می‌خوای اسمِ کدوم درسو ویرایش کنی یا کدوم درسو حذف کنی؟ اول ترم رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def editcourse_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ecourse_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "می‌خوای اسمِ کدوم درسو ویرایش کنی یا کدوم درسو حذف کنی؟ اول ترم رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def editcourse_pick_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» هنوز درسی نداره.")
        return
    # از course_id استفاده می‌کنیم (نه اسم) تا محدودیتِ ۶۴ بایتیِ callback_data تلگرام رد نشه.
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"ecourse_pick:{c['id']}")] for c in courses]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ecourse_root")])
    await query.edit_message_text(f"کدوم درسِ «{program}» رو می‌خوای ویرایش کنی؟", reply_markup=InlineKeyboardMarkup(buttons))


async def editcourse_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting_course_rename", None)
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    manage_text, manage_markup = _course_manage_text_markup(course_id, course)
    await query.edit_message_text(manage_text, reply_markup=manage_markup)


def _course_manage_text_markup(course_id: int, course: dict):
    """متن + کیبوردِ صفحه‌ی مدیریتِ یک درس (ecourse_pick) -- جدا شده تا هم از
    editcourse_pick_course هم از هندلرِ فعال‌سازیِ کورس (که به همین صفحه
    برمی‌گرده) قابلِ استفاده باشه."""
    status = dbmod.get_course_activation_info(course_id)
    status_line = ""
    active_btn_row = [
        InlineKeyboardButton("✅ این کورس الان فعاله", callback_data=f"crs_active:{course_id}")
    ]
    if status and status["is_active"]:
        status_line = "\n\n🟢 این کورس الان به‌عنوانِ کورسِ جاریِ این ترم علامت خورده."
        active_btn_row = [
            InlineKeyboardButton("↩️ برداشتنِ نشانِ «فعال»", callback_data=f"crs_deactivate:{course_id}")
        ]
    elif status and status["finished_at"]:
        status_line = "\n\n⚪️ این کورس قبلاً فعال بوده و الان تمام‌شده حساب می‌شه."
    buttons = [
        active_btn_row,
        [InlineKeyboardButton("👥 مدیریتِ گروه‌بندی", callback_data=f"crs_groups:{course_id}")],
        [
            InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"crs_edit:{course_id}"),
            InlineKeyboardButton("🗑 حذف درس", callback_data=f"crs_del:{course_id}"),
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecourse_prog:{course['term_name']}")],
    ]
    text = f"درس «{course['name']}» ({course['term_name']}) رو چیکار کنم؟{status_line}"
    return text, InlineKeyboardMarkup(buttons)


async def course_set_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✅ این کورس الان فعاله -- علامت‌زنیِ دستیِ کورسِ جاریِ چرخشی (مثلاً اطفال/
    جراحیِ فیزیوپات). اگه کورسِ دیگه‌ای توی همون ترم فعال بود، خودکار «تمام‌شده»
    حساب می‌شه. این کورسِ فعال بعداً برای لینک‌شدنِ خودکارِ منابع به کلاس‌هایی که
    فقط برچسبِ عمومیِ «کورس» دارن استفاده می‌شه (resolve_course_id_for_class_entry)."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    result = dbmod.set_active_course(course_id)
    if result is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    dbmod.log_action(
        update.effective_user.id, "SET_ACTIVE_COURSE", term=course["term_name"], detail=course["name"]
    )
    await query.answer("علامت خورد ✅", show_alert=False)
    manage_text, manage_markup = _course_manage_text_markup(course_id, course)
    await query.edit_message_text(manage_text, reply_markup=manage_markup)


async def course_unset_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """↩️ برداشتنِ نشانِ «فعال» -- بدونِ اینکه کورسِ دیگه‌ای رو جایگزینش کنه (برای
    وقتی ادمین اشتباهی زده، یا موقتاً هیچ کورسی فعال نیست)."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    dbmod.unset_active_course(course_id)
    dbmod.log_action(
        update.effective_user.id, "UNSET_ACTIVE_COURSE", term=course["term_name"], detail=course["name"]
    )
    manage_text, manage_markup = _course_manage_text_markup(course_id, course)
    await query.edit_message_text(manage_text, reply_markup=manage_markup)


# ============================================================
# ادمین: مدیریتِ گروه‌بندیِ عملی/کلاسیِ یه درس (فاز ۱۳)
# ============================================================

def _course_groups_text_markup(course_id: int, course: dict):
    has_split = dbmod.get_has_group_split_by_id(course_id)
    groups = dbmod.get_course_groups(course_id) if has_split else []
    lines = [f"👥 گروه‌بندیِ درسِ «{course['name']}» ({course['term_name']})"]
    buttons = []
    if not has_split:
        lines.append("\nاین درس هنوز گروهی نداره. برای شروع، مستقیم گروه اول رو بساز؛ فعال‌سازی خودکار انجام می‌شه.")
        buttons.append(
            [InlineKeyboardButton("➕ ساخت اولین گروه", callback_data=f"crs_group_add:{course_id}")]
        )
    else:
        if groups:
            lines.append("")
            for g in groups:
                lines.append(f"• {g['name']} — {g['day']} {g['start']}-{g['end']}")
        else:
            lines.append("\nهنوز هیچ گروهی تعریف نشده.")
        for g in groups:
            buttons.append(
                [InlineKeyboardButton(f"🗑 حذفِ «{g['name']}»", callback_data=f"crs_group_del:{g['id']}")]
            )
        buttons.append([InlineKeyboardButton("➕ افزودنِ گروه", callback_data=f"crs_group_add:{course_id}")])
        buttons.append(
            [InlineKeyboardButton("⛔️ غیرفعال‌کردنِ گروه‌بندی", callback_data=f"crs_groups_toggle:{course_id}")]
        )
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecourse_pick:{course_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def course_groups_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    await query.edit_message_text(*_course_groups_text_markup(course_id, course))


async def course_groups_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فعال/غیرفعال‌کردنِ گروه‌بندی برای یه درس. غیرفعال‌کردن، خودِ گروه‌های
    ثبت‌شده و انتخابِ دانشجوها رو پاک نمی‌کنه (اگه دوباره فعال بشه، همون‌ها
    برمی‌گردن) -- فقط یعنی «الان» این درس گروه‌بندی‌شده حساب نمی‌شه (مثلاً تو
    منوی «گروه‌های عملیِ من» دیگه نشون داده نمی‌شه)."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    new_value = not dbmod.get_has_group_split_by_id(course_id)
    dbmod.set_has_group_split(course_id, new_value)
    dbmod.log_action(
        update.effective_user.id,
        "TOGGLE_GROUP_SPLIT",
        term=course["term_name"],
        detail=f"{course['name']} -> {new_value}",
    )
    await query.edit_message_text(*_course_groups_text_markup(course_id, course))


async def course_group_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    clear_admin_flow_state(context)
    context.user_data["pending_course_group"] = {"course_id": course_id}
    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"crs_groups:{course_id}")]]
    )
    await query.edit_message_text(
        f"باشه، گروهِ جدید برای «{course['name']}» رو به این فرم بفرست:\n"
        "اسمِ گروه | روز | ساعتِ‌شروع-ساعتِ‌پایان\n"
        "مثال: گروه ۱ | چهارشنبه | 10:00-12:00",
        reply_markup=back_btn,
    )


async def course_group_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":", 1)[1])
    group = dbmod.get_course_group_by_id(group_id)
    if group is None:
        await query.answer("قبلاً حذف شده بود.", show_alert=True)
        return
    course = dbmod.get_course_by_id(group["course_id"])
    if course is None:
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    dbmod.delete_course_group(group_id)
    dbmod.log_action(
        update.effective_user.id, "DELETE_COURSE_GROUP", term=course["term_name"],
        detail=f"{course['name']}: {group['name']}",
    )
    await query.answer("حذف شد ✅")
    await query.edit_message_text(*_course_groups_text_markup(course["id"], course))


# ============================================================
# ادمین: مدیریت ترم‌ها (ویرایش نام / حذف) -- فقط سوپرادمین، چون تغییر
# ساختاری روی ترمه (مشابه ساختِ ترم که همون محدودیت رو داره).
# ============================================================

def _editterm_list_text_markup():
    terms = dbmod.get_terms_with_ids()
    buttons = [
        [InlineKeyboardButton(f"{t['name']} ({t['group_name']})", callback_data=f"eterm_pick:{t['id']}")]
        for t in terms
    ]
    return "کدوم ترم رو می‌خوای ویرایش یا حذف کنی؟", InlineKeyboardMarkup(buttons)


async def editterm_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/editterm -- ویرایشِ اسمِ یه ترم یا حذفِ کاملش (فقط سوپرادمین)."""
    if not is_super_admin(update.effective_user.id):
        return
    text, markup = _editterm_list_text_markup()
    await update.message.reply_text(text, reply_markup=markup)


async def editterm_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    text, markup = _editterm_list_text_markup()
    await query.edit_message_text(text, reply_markup=markup)


async def editterm_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    term_id = int(query.data.split(":", 1)[1])
    term = dbmod.get_term_by_id(term_id)
    if term is None:
        await query.edit_message_text("این ترم دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    buttons = [
        [
            InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"eterm_ren:{term_id}"),
            InlineKeyboardButton("🗑 حذف ترم", callback_data=f"eterm_del:{term_id}"),
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="eterm_root")],
    ]
    await query.edit_message_text(
        f"ترم «{term['name']}» (گروه: {term['group_name']}) رو چیکار کنم؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def editterm_rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    term_id = int(query.data.split(":", 1)[1])
    term = dbmod.get_term_by_id(term_id)
    if term is None:
        await query.edit_message_text("این ترم دیگه وجود نداره.")
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_term_rename"] = {"term_id": term_id}
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"eterm_pick:{term_id}")]])
    await query.edit_message_text(
        f"باشه، اسم جدید رو برای ترم «{term['name']}» بنویس و بفرست.", reply_markup=back_btn
    )


async def editterm_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    term_id = int(query.data.split(":", 1)[1])
    term = dbmod.get_term_by_id(term_id)
    if term is None:
        await query.edit_message_text("این ترم دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    course_count = dbmod.count_term_courses_by_id(term_id)
    file_count = dbmod.count_term_files_by_id(term_id)
    warn = ""
    if course_count:
        warn = f"\n⚠️ این ترم {course_count} درس داره ({file_count} فایل) که همه‌شون هم پاک می‌شن."
    buttons = [
        [
            InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"eterm_del_yes:{term_id}"),
            InlineKeyboardButton("❌ نه، بی‌خیال", callback_data=f"eterm_pick:{term_id}"),
        ]
    ]
    await query.edit_message_text(
        f"مطمئنی می‌خوای ترم «{term['name']}» رو کامل حذف کنی؟{warn}\nاین کار قابل بازگشت نیست.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def editterm_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_super_admin(update.effective_user.id):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    term_id = int(query.data.split(":", 1)[1])
    term = dbmod.get_term_by_id(term_id)
    if term is None:
        await query.answer()
        await query.edit_message_text("این ترم قبلاً حذف شده.")
        return
    await query.answer()
    ok, term_name = dbmod.delete_term_by_id(term_id)
    if ok:
        _delete_schedule_references(term_name)
        dbmod.log_action(update.effective_user.id, "DELETE_TERM", term=term_name)
        await query.edit_message_text(f"ترم «{term_name}» به همراه همه‌ی درس‌ها/فایل‌هاش حذف شد ✅")
    else:
        await query.edit_message_text("این ترم قبلاً حذف شده.")


# ============================================================
# ادمین: مدیریت «بخش»ها (Section) -- ایجاد/ویرایش‌نام/حذف با id.
# قبلاً بخش‌ها (علوم پایه/فیزیوپات) توی کدِ addsemester هاردکد شده بودن؛
# حالا از جدولِ groups خونده می‌شن، پس هر بخشِ جدیدی (کارآموزی، کارورزی، ...)
# که اینجا ساخته بشه، خودکار همه‌جای دیگه (مثل /addsemester) هم می‌بینتش.
# عملیاتِ ساختاریه -- فقط سوپرادمین.
# ============================================================

def _editsection_list_text_markup():
    sections = dbmod.get_groups_with_ids()
    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"esec_pick:{s['id']}")] for s in sections]
    buttons.append([InlineKeyboardButton("➕ بخش جدید", callback_data="esec_add")])
    return "بخش‌های فعلی (برای ویرایش/حذف انتخاب کن، یا بخشِ جدید بساز):", InlineKeyboardMarkup(buttons)


async def editsection_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/editsections -- ساخت بخش جدید، ویرایشِ اسم یا حذفِ کاملِ یه بخش (فقط سوپرادمین)."""
    if not is_super_admin(update.effective_user.id):
        return
    clear_admin_flow_state(context)
    # اگر سوپرادمین قبلاً داخل گپ دانشجویی بوده، متنِ نامِ بخش نباید توسط گپ مصرف بشه.
    context.user_data.pop("active_chat_room", None)
    text, markup = _editsection_list_text_markup()
    await update.message.reply_text(text, reply_markup=markup)


async def editsection_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    text, markup = _editsection_list_text_markup()
    await query.edit_message_text(text, reply_markup=markup)


async def editsection_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    clear_admin_flow_state(context)
    # همان باگِ بالا: ویزاردِ ساخت بخش باید بر حالت گپ اولویت داشته باشد.
    context.user_data.pop("active_chat_room", None)
    context.user_data["awaiting_section_name"] = True
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="esec_root")]])
    await query.edit_message_text(
        "اسمِ بخشِ جدید رو بنویس و بفرست (مثلاً «کارآموزی» یا «کارورزی»).", reply_markup=back_btn
    )


async def editsection_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    group_id = int(query.data.split(":", 1)[1])
    section = dbmod.get_group_by_id(group_id)
    if section is None:
        await query.edit_message_text("این بخش دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    term_count = dbmod.count_group_terms_by_id(group_id)
    buttons = [
        [
            InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"esec_ren:{group_id}"),
            InlineKeyboardButton("🗑 حذف بخش", callback_data=f"esec_del:{group_id}"),
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="esec_root")],
    ]
    await query.edit_message_text(
        f"بخشِ «{section['name']}» ({term_count} ترم) رو چیکار کنم؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def editsection_rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    group_id = int(query.data.split(":", 1)[1])
    section = dbmod.get_group_by_id(group_id)
    if section is None:
        await query.edit_message_text("این بخش دیگه وجود نداره.")
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_section_rename"] = {"group_id": group_id}
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"esec_pick:{group_id}")]])
    await query.edit_message_text(
        f"باشه، اسم جدید رو برای بخشِ «{section['name']}» بنویس و بفرست.", reply_markup=back_btn
    )


async def editsection_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    group_id = int(query.data.split(":", 1)[1])
    section = dbmod.get_group_by_id(group_id)
    if section is None:
        await query.edit_message_text("این بخش دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    term_count = dbmod.count_group_terms_by_id(group_id)
    warn = (
        f"\n⚠️ این بخش {term_count} ترم داره (به همراه همه‌ی درس‌ها/فایل‌هاشون) که همه‌شون هم پاک می‌شن."
        if term_count
        else ""
    )
    buttons = [
        [
            InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"esec_del_yes:{group_id}"),
            InlineKeyboardButton("❌ نه، بی‌خیال", callback_data=f"esec_pick:{group_id}"),
        ]
    ]
    await query.edit_message_text(
        f"مطمئنی می‌خوای بخشِ «{section['name']}» رو کامل حذف کنی؟{warn}\nاین کار قابل بازگشت نیست.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def editsection_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_super_admin(update.effective_user.id):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    group_id = int(query.data.split(":", 1)[1])
    section = dbmod.get_group_by_id(group_id)
    if section is None:
        await query.answer()
        await query.edit_message_text("این بخش قبلاً حذف شده.")
        return
    await query.answer()
    ok, group_name = dbmod.delete_group_by_id(group_id)
    if ok:
        dbmod.log_action(update.effective_user.id, "DELETE_SECTION", detail=group_name)
        await query.edit_message_text(f"بخشِ «{group_name}» به همراه همه‌ی ترم‌ها/درس‌ها/فایل‌هاش حذف شد ✅")
    else:
        await query.edit_message_text("این بخش قبلاً حذف شده.")


# ============================================================
# ادمین: مدیریت دسته‌های هر درس (ایجاد/ویرایش‌نام/حذف/کپی) -- کاملاً id-based
# و مستقل بینِ درس‌ها. نگاه کن به db.migrate_categories_per_course_v3: از اونجا
# به بعد هر درس ردیف‌های categories کاملاً جداگانه‌ی خودش رو داره، حتی اگه اسمِ
# دسته با درسِ دیگه یکی باشه. دسترسی مثلِ بقیه‌ی عملیاتِ سطح‌درس: can_manage_term.
# ============================================================

async def editcategories_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    clear_admin_flow_state(context)
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ecat_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "می‌خوای دسته‌های کدوم برنامه رو مدیریت کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def ecat_back_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ecat_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "می‌خوای دسته‌های کدوم برنامه رو مدیریت کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def ecat_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"ecat_course:{c['id']}")] for c in courses]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ecat_back_progs")])
    await query.edit_message_text(f"دسته‌های کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def _render_ecat_course(query, course_id: int) -> None:
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    cats = dbmod.get_categories_for_course(course_id)
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"ecat_pick:{course_id}:{c['id']}")] for c in cats]
    buttons.append([InlineKeyboardButton("➕ دسته جدید", callback_data=f"ecat_add:{course_id}")])
    buttons.append([InlineKeyboardButton("📋 کپی دسته از یه درسِ دیگه", callback_data=f"ecat_copy:{course_id}")])
    if len(cats) >= 2:
        buttons.append([InlineKeyboardButton("🗑 حذف چندتایی", callback_data=f"ecat_bulk_start:{course_id}")])
    mode = dbmod.get_course_mode(course_id)
    if mode == CUSTOM_MODE:
        mode_label = "🔀 حالتِ درس: سفارشی (فقط همینِ دسته‌های بالا -- تبدیل به استاندارد"
    else:
        mode_label = "🔀 حالتِ درس: استاندارد (۱۱ دسته‌ی معمول -- تبدیل به سفارشی/رفرنس"
    buttons.append([InlineKeyboardButton(f"{mode_label})", callback_data=f"ecat_mode:{course_id}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_prog:{course['term_name']}")])
    mode_note = (
        "\n\n🔀 این درس الان در حالتِ «سفارشی»ه: دقیقاً همین دسته‌های بالا (با هر اسمی) "
        "به دانشجو و در /upload نشون داده می‌شن، بدونِ ۱۱ دسته‌ی استاندارد."
        if mode == CUSTOM_MODE else ""
    )
    await query.edit_message_text(
        f"دسته‌های «{course['name']}» ({course['term_name']}):{mode_note}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ecat_show_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is not None and not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    await _render_ecat_course(query, course_id)


async def ecat_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_id_str = query.data.split(":", 2)
    course_id, category_id = int(course_id_str), int(cat_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    category = dbmod.get_category_by_id(category_id)
    if category is None or category["course_id"] != course_id:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    buttons = [
        [
            InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"ecat_ren:{course_id}:{category_id}"),
            InlineKeyboardButton("🗑 حذف دسته", callback_data=f"ecat_del:{course_id}:{category_id}"),
        ],
        [
            InlineKeyboardButton("⬆️ بالاتر", callback_data=f"ecat_move:{course_id}:{category_id}:up"),
            InlineKeyboardButton("⬇️ پایین‌تر", callback_data=f"ecat_move:{course_id}:{category_id}:down"),
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_course:{course_id}")],
    ]
    await query.edit_message_text(
        f"دسته‌ی «{category['name']}» از «{course['name']}» رو چیکار کنم؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ecat_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ترتیبِ نمایشِ یک دسته رو با همسایه‌ش (بالا/پایین) عوض می‌کنه -- درخواستِ
    «ترتیب نمایش» توی مدیریتِ دسته‌بندی‌ها."""
    query = update.callback_query
    _, course_id_str, cat_id_str, direction = query.data.split(":", 3)
    course_id, category_id = int(course_id_str), int(cat_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.answer()
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.answer()
        await deny_term_access(query, course["term_name"])
        return
    ok = dbmod.move_category_order(category_id, direction)
    await query.answer("جابه‌جا شد ✅" if ok else "دیگه نمی‌شه این‌ور جابه‌جاش کرد.")
    await ecat_pick_category(update, context)


async def ecat_toggle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """جابه‌جاییِ حالتِ یه درس بینِ استاندارد (۱۱ دسته‌ی معمول) و سفارشی (هر دسته‌ی
    واقعی‌ای که در دیتابیس هست، دقیقاً همون‌جوری -- برای ترم‌هایی مثل «رفرنس» که
    ساختارِ دلخواهِ خودشون رو دارن). این تغییرِ حالت هیچ دسته/فایلی رو پاک نمی‌کنه."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    new_mode = STANDARD_MODE if dbmod.get_course_mode(course_id) == CUSTOM_MODE else CUSTOM_MODE
    dbmod.set_course_mode(course_id, new_mode)
    dbmod.log_action(
        update.effective_user.id, "TOGGLE_COURSE_MODE", term=course["term_name"],
        detail=f"course={course['name']} mode={new_mode}",
    )
    await _render_ecat_course(query, course_id)


async def ecat_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_category_add"] = {"course_id": course_id}
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_course:{course_id}")]])
    await query.edit_message_text(
        f"اسمِ دسته‌ی جدید رو برای «{course['name']}» بنویس و بفرست.", reply_markup=back_btn
    )


async def ecat_rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_id_str = query.data.split(":", 2)
    course_id, category_id = int(course_id_str), int(cat_id_str)
    course = dbmod.get_course_by_id(course_id)
    category = dbmod.get_category_by_id(category_id)
    if course is None or category is None or category["course_id"] != course_id:
        await query.edit_message_text("این دسته دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_category_rename"] = {"course_id": course_id, "category_id": category_id}
    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_pick:{course_id}:{category_id}")]]
    )
    await query.edit_message_text(
        f"باشه، اسم جدید رو برای دسته‌ی «{category['name']}» (فقط توی «{course['name']}») بنویس و بفرست.",
        reply_markup=back_btn,
    )


async def ecat_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_id_str = query.data.split(":", 2)
    course_id, category_id = int(course_id_str), int(cat_id_str)
    course = dbmod.get_course_by_id(course_id)
    category = dbmod.get_category_by_id(category_id)
    if course is None or category is None or category["course_id"] != course_id:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    # force=False چیزی رو حذف نمی‌کنه اگه دسته خالی نباشه -- فقط برای گرفتنِ تعدادِ
    # فایل/زیردسته به‌عنوانِ پیش‌نمایش صداش می‌زنیم.
    preview = dbmod.delete_category(category_id, force=False)
    if preview.get("ok"):
        dbmod.log_action(
            update.effective_user.id, "DELETE_CATEGORY", term=course["term_name"],
            detail=f"{course['name']} / {category['name']}",
        )
        await query.edit_message_text(f"دسته‌ی «{category['name']}» (خالی بود) از «{course['name']}» حذف شد ✅")
        return
    files_n, groups_n = preview.get("files", 0), preview.get("groups", 0)
    buttons = [
        [
            InlineKeyboardButton(
                "✅ بله، با همه‌ی محتواش حذف کن", callback_data=f"ecat_del_yes:{course_id}:{category_id}"
            ),
            InlineKeyboardButton("❌ نه، بی‌خیال", callback_data=f"ecat_pick:{course_id}:{category_id}"),
        ]
    ]
    await query.edit_message_text(
        f"دسته‌ی «{category['name']}» از «{course['name']}» خالی نیست.\n"
        f"⚠️ {files_n} فایل و {groups_n} زیردسته داره که همه‌شون هم پاک می‌شن.\nمطمئنی؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ecat_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, course_id_str, cat_id_str = query.data.split(":", 2)
    course_id, category_id = int(course_id_str), int(cat_id_str)
    course = dbmod.get_course_by_id(course_id)
    category = dbmod.get_category_by_id(category_id)
    if course is None or category is None or category["course_id"] != course_id:
        await query.answer()
        await query.edit_message_text("این دسته قبلاً حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    await query.answer()
    result = dbmod.delete_category(category_id, force=True)
    dbmod.log_action(
        update.effective_user.id, "DELETE_CATEGORY_FORCE", term=course["term_name"],
        detail=f"{course['name']} / {category['name']} ({result.get('deleted_files', 0)} فایل)",
    )
    await query.edit_message_text(
        f"دسته‌ی «{category['name']}» و {result.get('deleted_files', 0)} فایل / "
        f"{result.get('deleted_groups', 0)} زیردسته‌ش از «{course['name']}» حذف شد ✅"
    )


async def ecat_copy_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """شروعِ کپیِ یه دسته از یه درسِ دیگه به این درس (فقط تعریفِ دسته -- نه فایل‌هاش)."""
    query = update.callback_query
    await query.answer()
    target_course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(target_course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ecopy_prog:{target_course_id}:{p}")] for p in allowed]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_course:{target_course_id}")])
    await query.edit_message_text(
        f"می‌خوای دسته رو از کدوم برنامه کپی کنی، تا بره توی «{course['name']}»؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ecopy_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, target_course_id_str, program = query.data.split(":", 2)
    target_course_id = int(target_course_id_str)
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"ecopy_course:{target_course_id}:{c['id']}")]
        for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_copy:{target_course_id}")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def ecopy_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, target_course_id_str, src_course_id_str = query.data.split(":", 2)
    target_course_id, src_course_id = int(target_course_id_str), int(src_course_id_str)
    src_course = dbmod.get_course_by_id(src_course_id)
    if src_course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    cats = dbmod.get_categories_for_course(src_course_id)
    if not cats:
        await query.edit_message_text(f"«{src_course['name']}» هیچ دسته‌ای نداره.")
        return
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"ecopy_exec:{target_course_id}:{c['id']}")] for c in cats
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_copy:{target_course_id}")])
    await query.edit_message_text(
        f"کدوم دسته‌ی «{src_course['name']}» رو کپی کنم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def ecopy_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, target_course_id_str, src_cat_id_str = query.data.split(":", 2)
    target_course_id, src_category_id = int(target_course_id_str), int(src_cat_id_str)
    target_course = dbmod.get_course_by_id(target_course_id)
    src_category = dbmod.get_category_by_id(src_category_id)
    if target_course is None or src_category is None:
        await query.answer()
        await query.edit_message_text("این درس یا دسته دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, target_course["term_name"]):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    await query.answer()
    existing_before = dbmod.get_category_id_for_course(target_course_id, src_category["name"])
    new_id = dbmod.copy_category(src_category_id, target_course_id)
    if new_id is None:
        await query.edit_message_text("کپی انجام نشد (اسمِ خالی؟).")
        return
    dbmod.log_action(
        update.effective_user.id, "COPY_CATEGORY", term=target_course["term_name"],
        detail=f"{src_category['name']} -> {target_course['name']}",
    )
    if existing_before is not None:
        await query.edit_message_text(
            f"«{target_course['name']}» از قبل دسته‌ای به اسمِ «{src_category['name']}» داشت؛ چیز جدیدی اضافه نشد."
        )
    else:
        await query.edit_message_text(f"دسته‌ی «{src_category['name']}» به «{target_course['name']}» اضافه شد ✅")


async def _render_ecat_bulk(query, context: ContextTypes.DEFAULT_TYPE, course_id: int) -> None:
    """چک‌لیستِ انتخابِ چندتاییِ دسته‌ها برای حذفِ همزمان -- انتخاب‌ها توی
    context.user_data نگه داشته می‌شن (نه توی callback_data)، پس فقط باید همون
    یک session که این چک‌لیست رو باز کرده معتبر باشه."""
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        context.user_data.pop("ecat_bulk", None)
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    cats = dbmod.get_categories_for_course(course_id)
    state = context.user_data.setdefault("ecat_bulk", {"course_id": course_id, "selected": set()})
    if state.get("course_id") != course_id:
        state = {"course_id": course_id, "selected": set()}
        context.user_data["ecat_bulk"] = state
    selected = state["selected"]
    # هر id ای که دیگه توی لیستِ واقعیِ دسته‌های این درس نیست (مثلاً یه‌جای دیگه حذف
    # شده) رو از انتخاب پاک می‌کنیم تا هیچ‌وقت روی چیزِ نامعتبر کار نکنیم.
    valid_ids = {c["id"] for c in cats}
    selected &= valid_ids

    buttons = []
    for c in cats:
        mark = "☑️" if c["id"] in selected else "⬜️"
        buttons.append(
            [InlineKeyboardButton(f"{mark} {c['name']}", callback_data=f"ecat_bulk_toggle:{course_id}:{c['id']}")]
        )
    footer = []
    if selected:
        footer.append(
            InlineKeyboardButton(f"🗑 حذفِ {len(selected)} تا", callback_data=f"ecat_bulk_confirm:{course_id}")
        )
    buttons.append(footer) if footer else None
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ecat_course:{course_id}")])
    await query.edit_message_text(
        f"هر کدوم از دسته‌های «{course['name']}» رو که می‌خوای حذف بشه، بزن تا تیک بخوره "
        "(می‌تونی چندتا رو هم‌زمان انتخاب کنی). این کار فقط روی همین درس اثر می‌ذاره؛ "
        "درس‌ها/ترم‌های دیگه دست‌نخورده می‌مونن.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ecat_bulk_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    context.user_data["ecat_bulk"] = {"course_id": course_id, "selected": set()}
    await _render_ecat_bulk(query, context, course_id)


async def ecat_bulk_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_id_str = query.data.split(":", 2)
    course_id, category_id = int(course_id_str), int(cat_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        context.user_data.pop("ecat_bulk", None)
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    state = context.user_data.setdefault("ecat_bulk", {"course_id": course_id, "selected": set()})
    if state.get("course_id") != course_id:
        state = {"course_id": course_id, "selected": set()}
        context.user_data["ecat_bulk"] = state
    selected = state["selected"]
    if category_id in selected:
        selected.discard(category_id)
    else:
        # چک اینکه این دسته واقعاً مالِ همین درسه -- صرفاً برای دفاع در برابرِ
        # callback_data قدیمی/دستکاری‌شده؛ اگه مالِ این درس نبود، بی‌صدا رد می‌شه.
        category = dbmod.get_category_by_id(category_id)
        if category is not None and category["course_id"] == course_id:
            selected.add(category_id)
    await _render_ecat_bulk(query, context, course_id)


async def ecat_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        context.user_data.pop("ecat_bulk", None)
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    state = context.user_data.get("ecat_bulk") or {}
    selected = list(state.get("selected") or [])
    if state.get("course_id") != course_id or not selected:
        await _render_ecat_bulk(query, context, course_id)
        return
    # force=False چیزی رو حذف نمی‌کنه مگه اینکه همه‌ی دسته‌های انتخابی از قبل خالی
    # باشن -- در اون صورت خودِ همین تابع (دقیقاً مثلِ delete_category تک‌تایی)
    # بلافاصله حذفشون می‌کنه و ok=True برمی‌گردونه. برای همین اینجا اگه ok=True شد،
    # یعنی حذف *همین الان* انجام شده -- نباید دوباره صداش بزنیم (وگرنه چون دیگه
    # چیزی برای حذف نمونده، اشتباهاً می‌گه «معتبر نیست»).
    preview = dbmod.delete_categories_bulk(course_id, selected, force=False)
    if preview.get("ok"):
        context.user_data.pop("ecat_bulk", None)
        names = preview.get("deleted_categories") or []
        names_txt = "، ".join(f"«{n}»" for n in names)
        dbmod.log_action(
            update.effective_user.id, "DELETE_CATEGORIES_BULK", term=course["term_name"],
            detail=f"{course['name']}: {names_txt} (خالی بودن)",
        )
        await query.edit_message_text(
            f"{len(names)} دسته ({names_txt}) که خالی بودن از «{course['name']}» حذف شد ✅\n"
            "(فقط همین درس -- درس‌ها و ترم‌های دیگه دست‌نخورده موندن)"
        )
        return
    names = preview.get("category_names") or []
    if preview.get("reason") == "no_valid_categories":
        context.user_data.pop("ecat_bulk", None)
        await query.edit_message_text("هیچ‌کدوم از انتخاب‌هات دیگه معتبر نیستن (شاید قبلاً حذف شدن).")
        return
    files_n, groups_n = preview.get("files", 0), preview.get("groups", 0)
    names_txt = "، ".join(f"«{n}»" for n in names)
    buttons = [
        [
            InlineKeyboardButton("✅ بله، همه رو با محتواشون حذف کن", callback_data=f"ecat_bulk_yes:{course_id}"),
            InlineKeyboardButton("❌ نه، بی‌خیال", callback_data=f"ecat_bulk_start:{course_id}"),
        ]
    ]
    await query.edit_message_text(
        f"{len(names)} دسته از «{course['name']}» انتخاب شده: {names_txt}\n"
        f"⚠️ روی هم {files_n} فایل و {groups_n} زیردسته دارن که همه‌شون پاک می‌شن.\n"
        "این کار فقط روی همین درس اثر می‌ذاره. مطمئنی؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ecat_bulk_execute_core(
    update: Update, context: ContextTypes.DEFAULT_TYPE, course_id: int, selected: list
) -> None:
    query = update.callback_query
    course = dbmod.get_course_by_id(course_id)
    result = dbmod.delete_categories_bulk(course_id, selected, force=True)
    context.user_data.pop("ecat_bulk", None)
    if not result.get("ok"):
        await query.edit_message_text("هیچ‌کدوم از انتخاب‌هات دیگه معتبر نیستن (شاید قبلاً حذف شدن).")
        return
    names = result.get("deleted_categories") or []
    names_txt = "، ".join(f"«{n}»" for n in names)
    dbmod.log_action(
        update.effective_user.id, "DELETE_CATEGORIES_BULK", term=course["term_name"] if course else None,
        detail=f"{course['name'] if course else course_id}: {names_txt} "
               f"({result.get('deleted_files', 0)} فایل، {result.get('deleted_groups', 0)} زیردسته)",
    )
    await query.edit_message_text(
        f"{len(names)} دسته ({names_txt}) به همراه {result.get('deleted_files', 0)} فایل / "
        f"{result.get('deleted_groups', 0)} زیردسته از «{course['name'] if course else course_id}» حذف شد ✅\n"
        "(فقط همین درس -- درس‌ها و ترم‌های دیگه کاملاً دست‌نخورده موندن)"
    )


async def ecat_bulk_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        context.user_data.pop("ecat_bulk", None)
        await query.answer()
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    await query.answer()
    state = context.user_data.get("ecat_bulk") or {}
    selected = list(state.get("selected") or [])
    if state.get("course_id") != course_id or not selected:
        context.user_data.pop("ecat_bulk", None)
        await query.edit_message_text("چیزی برای حذف انتخاب نشده بود.")
        return
    await ecat_bulk_execute_core(update, context, course_id, selected)


def _upload_prog_picker_text_markup(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    allowed = admin_allowed_programs(user_id)
    buttons = []
    last = context.user_data.get("last_upload_path")
    if last and last["term"] in allowed:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"🔁 همون‌جای قبلی: {last['course']} - {last['category']}",
                    callback_data="continue_last_upload",
                )
            ]
        )
    buttons += [[InlineKeyboardButton(p, callback_data=f"pick_prog_upload:{p}")] for p in allowed]
    return "می‌خوای برای کدوم برنامه آپلود کنی؟", InlineKeyboardMarkup(buttons)


def upload_progress_keyboard() -> InlineKeyboardMarkup:
    """زیر پیام تأیید هر فایل، تا وقتی سشن آپلود چندفایلی باز باشه نشون داده می‌شه."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ پایان آپلود", callback_data="upfin_end")],
            [InlineKeyboardButton("🔙 تغییر مسیر", callback_data="upfin_change")],
        ]
    )


def build_post_upload_keyboard(term_name: str, course_name: str, course_id) -> InlineKeyboardMarkup:
    """بعد از آپلود موفقِ یه فایل، این کیبورد رو نشون می‌ده تا ادمین بدون
    برگشتن به منوی اصلی بتونه بخش دیگه‌ای از همین درس، درسِ دیگه‌ای از همون
    برنامه، یا برنامه‌ی دیگه‌ای رو انتخاب کنه (یا کلاً از سشن بیاد بیرون)."""
    buttons = []
    if course_id is not None:
        buttons += [
            [InlineKeyboardButton(cat, callback_data=f"pick_cat_upload:{course_id}:{i}")]
            for i, cat in categories_for(term_name, course_name)
        ]
    buttons.append([InlineKeyboardButton("📚 انتخاب درس دیگر", callback_data=f"pick_prog_upload:{term_name}")])
    buttons.append([InlineKeyboardButton("↩️ بازگشت", callback_data="up_back_progs")])
    buttons.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="upfin_home")])
    buttons.append([InlineKeyboardButton("✅ پایان آپلود", callback_data="upfin_end")])
    return InlineKeyboardMarkup(buttons)


async def upload_post_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🏠 منوی اصلی -- از سشنِ آپلودِ چندفایلی خارج می‌شه و منوی اصلی رو نشون می‌ده."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_upload", None)
    context.user_data.pop("upload_queue", None)
    context.user_data.pop("awaiting_upload_new_name", None)
    await query.edit_message_text("باشه، از سشن آپلود اومدی بیرون.")
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="از منوی پایین ادامه بده 👇", reply_markup=main_menu_keyboard()
    )


async def add_course_start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    # لایه‌ی محافظ: شروع تازه‌ی /upload همیشه باید با وضعیت تمیز باشه. اگه به هر دلیلی
    # (مثلاً یه باگ دیگه یا کرش وسط کار) صفِ فایل‌های تأییدنشده از سشنِ قبلی جا مونده
    # باشه، اینجا پاکش می‌کنیم تا قاطیِ آپلود جدید نشه.
    context.user_data.pop("pending_upload", None)
    context.user_data.pop("upload_queue", None)
    context.user_data.pop("awaiting_upload_new_name", None)
    text, markup = _upload_prog_picker_text_markup(context, update.effective_user.id)
    await update.message.reply_text(text, reply_markup=markup)


async def upload_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔙 بازگشت از لیستِ درس‌ها به لیستِ برنامه‌ها (ابتدای مسیر آپلود)."""
    query = update.callback_query
    await query.answer()
    text, markup = _upload_prog_picker_text_markup(context, update.effective_user.id)
    await query.edit_message_text(text, reply_markup=markup)


async def upload_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✅ پایان آپلود -- سشن چندفایلی رو کامل می‌بنده.
    نکته‌ی مهم: باید علاوه بر pending_upload، صفِ فایل‌های تأییدنشده (upload_queue) و
    فلگِ در-انتظارِ-تغییرنامِ (awaiting_upload_new_name) رو هم پاک کنیم؛ وگرنه فایل‌های
    توی صف که هنوز تأیید نشدن، برای همیشه توی حافظه‌ی همون ادمین می‌مونن و سشن‌های
    بعدیِ /upload رو هم آلوده می‌کنن (حتی بعد از /start، چون این دیتا اونجا پاک نمی‌شه)."""
    query = update.callback_query
    await query.answer("سشن آپلود بسته شد ✅")
    leftover = len(context.user_data.get("upload_queue") or [])
    context.user_data.pop("pending_upload", None)
    context.user_data.pop("upload_queue", None)
    context.user_data.pop("awaiting_upload_new_name", None)
    extra = f"\n({leftover} فایلِ تأییدنشده‌ی توی صف هم لغو شد.)" if leftover else ""
    await query.edit_message_text(f"✅ پایان آپلود. هر وقت خواستی، دوباره با /upload شروع کن.{extra}")


async def upload_change_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔙 تغییر مسیر -- سشن فعلی رو می‌بنده و برمی‌گرده به انتخاب برنامه/درس/دسته‌بندیِ جدید،
    بدون این‌که کاربر مجبور باشه دوباره /upload رو دستی بزنه."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_upload", None)
    # مثل upload_finish: صفِ فایل‌های تأییدنشده و فلگِ تغییرنام هم باید پاک بشن، وگرنه
    # با انتخاب مسیر جدید، فایل‌های قدیمیِ توی صف قاطی سشنِ جدید می‌شن.
    context.user_data.pop("upload_queue", None)
    context.user_data.pop("awaiting_upload_new_name", None)
    text, markup = _upload_prog_picker_text_markup(context, update.effective_user.id)
    await query.edit_message_text(text, reply_markup=markup)


async def continue_last_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    last = context.user_data.get("last_upload_path")
    if not last:
        await query.edit_message_text("مسیر قبلی پیدا نشد. از اول انتخاب کن.")
        return
    if not can_manage_term(update.effective_user.id, last["term"]):
        await deny_term_access(query, last["term"])
        return
    context.user_data["pending_upload"] = last
    await query.edit_message_text(
        f"باشه، فایل بعدی رو برای «{last['course']} - {last['category']}» بفرست.\n"
        "می‌تونی چند فایل پشت سر هم بفرستی؛ وقتی تموم شد «✅ پایان آپلود» رو بزن.",
        reply_markup=upload_progress_keyboard(),
    )


async def pick_program_for_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» هنوز درسی نداره. اول با /addcourse درس اضافه کن.")
        return
    # از course_id به‌جای نام کامل درس توی callback_data استفاده می‌کنیم:
    # تلگرام callback_data رو به ۶۴ بایت محدود می‌کنه و اسم خیلی از دروس
    # (مخصوصاً بیرون از فیزیوپات ۱) از این حد رد می‌شه -> کل دکمه‌ها فیل می‌شدن.
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"pick_course_upload:{c['id']}")] for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="up_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def pick_course_for_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده. دوباره با /upload شروع کن.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"pick_cat_upload:{course_id}:{i}")]
        for i, cat in categories_for(course["term_name"], course["name"])
    ]
    buttons.append(
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"pick_prog_upload:{course['term_name']}")]
    )
    await query.edit_message_text(
        f"فایل رو توی کدوم بخشِ «{course['name']}» بذارم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def pick_category_for_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده. دوباره با /upload شروع کن.")
        return
    program = course["term_name"]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    await _render_upload_destination_chooser(query, course_id, cat_idx)


async def _render_upload_destination_chooser(query, course_id: int, cat_idx: int) -> None:
    """بعد از انتخابِ دسته‌بندی توی /upload، اگه اون دسته‌بندی از قبل File Group
    (ساخته‌شده با /filegroups) داشته باشه، اول می‌پرسه فایل رو کجای اون دسته بذاره:
    مستقیم زیرِ دسته (بدون‌گروه)، توی یکی از گروه‌های موجود، یا توی یه گروهِ جدید.
    این‌طوری /upload دیگه مجبور نیست کاربر رو بفرسته سراغ جریانِ جداگانه‌ی /filegroups."""
    course = dbmod.get_course_by_id(course_id)
    course_name = course["name"]
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    groups = dbmod.get_file_groups(course_id, category_id) if category_id else []

    buttons = []
    if groups:
        buttons.append(
            [InlineKeyboardButton("📄 مستقیم زیر این بخش (بدون‌گروه)", callback_data=f"pick_updest:{course_id}:{cat_idx}:none")]
        )
        for g in groups:
            buttons.append(
                [InlineKeyboardButton(f"📁 {g['name']}", callback_data=f"pick_updest:{course_id}:{cat_idx}:{g['id']}")]
            )
    buttons.append([InlineKeyboardButton("➕ گروه جدید", callback_data=f"pick_upnewgrp:{course_id}:{cat_idx}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"pick_course_upload:{course_id}")])
    buttons.append([InlineKeyboardButton("🔙 تغییر مسیر", callback_data="upfin_change")])

    if groups:
        text = f"«{course_name} - {category}» چند تا گروه/پوشه داره. فایل رو کجاش بذارم؟"
    else:
        text = (
            f"فایل رو مستقیم زیرِ «{course_name} - {category}» بذارم، یا اول یه گروه/پوشه بسازم؟\n"
            "(اگه لازم نیست فایل‌ها دسته‌بندیِ ریزتری داشته باشن، «➕ گروه جدید» رو نزن و از بالا رد شو -- "
            "فقط با /upload معمولی هم می‌تونی مستقیم بفرستی.)"
        )
        buttons.insert(
            0,
            [InlineKeyboardButton("📄 مستقیم زیر این بخش (بدون‌گروه)", callback_data=f"pick_updest:{course_id}:{cat_idx}:none")],
        )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


def _set_pending_upload_and_prompt_text(course_name: str, category: str, group_name: str = None) -> str:
    dest = f"{course_name} - {category}" + (f" → {group_name}" if group_name else "")
    return (
        f"حالا فایل‌ها رو برای «{dest}» بفرست.\n"
        "توی کپشن فایل می‌تونی توضیح کوتاه هم بنویسی (مثلاً «نمونه‌سوال میان‌ترم»).\n\n"
        "می‌تونی چند فایل رو پشت سر هم بفرستی؛ وقتی تموم شد «✅ پایان آپلود» رو بزن."
    )


async def pick_upload_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بعد از چوزرِ مقصد (_render_upload_destination_chooser): «بدون‌گروه» یا یکی از گروه‌های موجود."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, dest = query.data.split(":", 3)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده. دوباره با /upload شروع کن.")
        return
    program, course_name = course["term_name"], course["name"]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return

    if dest == "none":
        context.user_data["pending_upload"] = {"term": program, "course": course_name, "category": category}
        back_cb = f"pick_cat_upload:{course_id}:{cat_idx}"
        prompt = _set_pending_upload_and_prompt_text(course_name, category)
    else:
        group_id = int(dest)
        group = dbmod.get_file_group(group_id)
        if group is None:
            await query.edit_message_text("این گروه دیگه پیدا نشد؛ شاید حذف شده.")
            return
        group_name = group["name"]
        context.user_data["pending_upload"] = {
            "term": program, "course": course_name, "category": category,
            "group_id": group_id, "group_name": group_name,
        }
        back_cb = f"pick_cat_upload:{course_id}:{cat_idx}"
        prompt = _set_pending_upload_and_prompt_text(course_name, category, group_name)

    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 بازگشت", callback_data=back_cb)],
            [InlineKeyboardButton("🔙 تغییر مسیر", callback_data="upfin_change")],
        ]
    )
    await query.edit_message_text(prompt, reply_markup=buttons)


async def pick_upload_new_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌ی «➕ گروه جدید» توی چوزرِ مقصدِ آپلود -- اسمِ گروه رو می‌پرسه و بعد از
    ساختنش، بلافاصله همون‌جا (بدون رفتن سراغ /filegroups) آماده‌ی گرفتنِ فایل می‌شه."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.edit_message_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_new_group_name"] = {"course_id": course_id, "cat_idx": cat_idx, "then_upload": True}
    await query.edit_message_text(
        "اسمِ گروه/پوشه‌ی جدید رو بفرست (مثلاً «دکتر احمدی» یا «جلسات اول»).\nبرای انصراف /cancel رو بفرست."
    )


async def pick_group_for_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مشابه pick_category_for_upload، ولی مقصدِ آپلود یه گروهِ مشخص زیرِ دسته‌بندیه
    (از منوی /filegroups صدا زده می‌شه، نه از مسیر آپلودِ عادی)."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course["term_name"], course["name"]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    group = dbmod.get_file_group(group_id)
    group_name = group["name"] if group else "گروه"
    context.user_data["pending_upload"] = {
        "term": program, "course": course_name, "category": category,
        "group_id": group_id, "group_name": group_name,
    }
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_group:{course_id}:{cat_idx}:{group_id}")]]
    )
    await query.edit_message_text(
        f"حالا فایل‌ها رو برای «{course_name} - {category} → {group_name}» بفرست.\n"
        "توی کپشن فایل می‌تونی توضیح کوتاه هم بنویسی.\n\n"
        "می‌تونی چند فایل رو پشت سر هم بفرستی؛ وقتی تموم شد «✅ پایان آپلود» رو بزن.",
        reply_markup=buttons,
    )


# ============================================================
# دانشجو: مشارکت -> ارسال تجربه امتحان
# ============================================================

async def submit_menu_exam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [[InlineKeyboardButton(p, callback_data=f"subexam_prog:{p}")] for p in get_program_names()]
    await query.edit_message_text("تجربه امتحان مربوط به کدوم برنامه‌ست؟", reply_markup=InlineKeyboardMarkup(buttons))


async def submit_exam_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    # از course_id استفاده می‌کنیم (نه اسم) تا محدودیتِ ۶۴ بایتیِ callback_data تلگرام رد نشه.
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"subexam_course:{c['id']}")] for c in courses
    ]
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def submit_exam_start_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده. دوباره از منوی «📩 مشارکت دانشجویان» امتحان کن.")
        return
    program, course_name = course["term_name"], course["name"]
    context.user_data["exam_form"] = {"term": program, "course": course_name, "step": 0, "answers": {}}
    await query.edit_message_text(
        f"باشه، بریم برای «{course_name}» ({program}):\n\n"
        "⚠️ لطفاً محترمانه و بدونِ الفاظِ نامناسب بنویس؛ تجربه‌هایی که رعایت نکنن رد می‌شن.\n\n"
        f"{EXAM_FIELDS[0][1]}"
    )


async def handle_exam_form_text(update: Update, context: ContextTypes.DEFAULT_TYPE, exam_form: dict) -> None:
    step = exam_form["step"]
    field_key, _ = EXAM_FIELDS[step]
    exam_form["answers"][field_key] = update.message.text.strip()
    step += 1
    exam_form["step"] = step

    if step < len(EXAM_FIELDS):
        context.user_data["exam_form"] = exam_form
        await update.message.reply_text(EXAM_FIELDS[step][1])
        return

    context.user_data.pop("exam_form", None)
    context.user_data["exam_form_pending_anon"] = exam_form
    buttons = [
        [
            InlineKeyboardButton("🙈 ناشناس", callback_data="examanon:yes"),
            InlineKeyboardButton("🙋 با لقبم", callback_data="examanon:no"),
        ]
    ]
    await update.message.reply_text(
        "آخرین قدم: می‌خوای این تجربه ناشناس ثبت بشه یا با لقبت؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def exam_confirm_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    anonymous = query.data.split(":", 1)[1] == "yes"
    exam_form = context.user_data.pop("exam_form_pending_anon", None)
    if not exam_form:
        await query.edit_message_text("یه مشکلی پیش اومد، دوباره از منوی «📩 مشارکت دانشجویان» امتحان کن.")
        return

    sender = update.effective_user
    display = get_display_name(sender.id, sender.first_name)
    pending = load_pending()
    sub_id = next_pending_id(pending)
    entry = {
        "id": sub_id,
        "type": "exam",
        "term": exam_form["term"],
        "course": exam_form["course"],
        "answers": exam_form["answers"],
        "anonymous": anonymous,
        "sender_id": sender.id,
        "sender_display": display,
    }
    pending.append(entry)
    save_pending(pending)

    await query.edit_message_text("ممنون! تجربه‌ات برای بررسی ارسال شد، بعد از تأیید مدیر منتشر می‌شه ✅")
    await notify_admin_pending(context, entry)


# ============================================================
# دانشجو: مشارکت -> ارسال فایل و خلاصه
# ============================================================

async def submit_menu_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [[InlineKeyboardButton(p, callback_data=f"subfile_prog:{p}")] for p in get_program_names()]
    await query.edit_message_text("فایلت مربوط به کدوم برنامه‌ست؟", reply_markup=InlineKeyboardMarkup(buttons))


async def submit_file_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    # از course_id استفاده می‌کنیم (نه اسم) تا محدودیتِ ۶۴ بایتیِ callback_data تلگرام رد نشه.
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"subfile_course:{c['id']}")] for c in courses
    ]
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def submit_file_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده. دوباره از منوی «📩 مشارکت دانشجویان» امتحان کن.")
        return
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"subfile_cat:{course_id}:{i}")]
        for i, (label, _cat_idx) in enumerate(SUBMIT_CATEGORIES)
    ]
    await query.edit_message_text("این فایل چه نوعیه؟", reply_markup=InlineKeyboardMarkup(buttons))


async def submit_file_pick_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, idx_str = query.data.split(":", 2)
    course = dbmod.get_course_by_id(int(course_id_str))
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده. دوباره از منوی «📩 مشارکت دانشجویان» امتحان کن.")
        return
    program, course_name = course["term_name"], course["name"]
    _label, cat_idx = SUBMIT_CATEGORIES[int(idx_str)]
    category = CATEGORIES[cat_idx]
    context.user_data["file_submission_pending"] = {
        "term": program,
        "course": course_name,
        "category": category,
    }
    buttons = [
        [
            InlineKeyboardButton("🙈 ناشناس", callback_data="subfileanon:yes"),
            InlineKeyboardButton("🙋 با لقبم", callback_data="subfileanon:no"),
        ]
    ]
    await query.edit_message_text("می‌خوای ناشناس بفرستی یا با لقبت؟", reply_markup=InlineKeyboardMarkup(buttons))


async def submit_file_confirm_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    anonymous = query.data.split(":", 1)[1] == "yes"
    pending = context.user_data.pop("file_submission_pending", None)
    if not pending:
        await query.edit_message_text("یه مشکلی پیش اومد، دوباره از منوی «📩 مشارکت دانشجویان» امتحان کن.")
        return
    pending["anonymous"] = anonymous
    context.user_data["file_submission"] = pending
    await query.edit_message_text("باشه، حالا فایل (سند یا عکس) رو بفرست.")


async def handle_file_submission(update: Update, context: ContextTypes.DEFAULT_TYPE, sub: dict) -> None:
    message = update.message
    if message.document:
        file_id = message.document.file_id
        default_caption = message.document.file_name
    elif message.photo:
        file_id = message.photo[-1].file_id
        default_caption = "تصویر"
    else:
        await message.reply_text("این نوع فایل پشتیبانی نمی‌شه. سند یا عکس بفرست.")
        return

    # کپشنی که دانشجو موقع ارسال فایل تایپ می‌کنه صرفاً یه توضیحه، نه اسم فایل.
    # قبلاً caption (اسم نمایشی) با همین توضیح جایگزین می‌شد و اسم واقعی فایل گم می‌شد؛
    # الان caption همیشه اسم فایل می‌مونه و توضیح جدا توی description ذخیره می‌شه.
    caption = default_caption
    description = (message.caption or "").strip()
    sender = update.effective_user
    display = get_display_name(sender.id, sender.first_name)

    pending = load_pending()
    sub_id = next_pending_id(pending)
    entry = {
        "id": sub_id,
        "type": "file",
        "term": sub["term"],
        "course": sub["course"],
        "category": sub["category"],
        "file_id": file_id,
        "caption": caption,
        "description": description,
        "anonymous": sub["anonymous"],
        "sender_id": sender.id,
        "sender_display": display,
    }
    pending.append(entry)
    save_pending(pending)
    context.user_data.pop("file_submission", None)

    await message.reply_text("ممنون! فایلت برای بررسی ارسال شد، بعد از تأیید مدیر منتشر می‌شه ✅")
    await notify_admin_pending(context, entry)


# ============================================================
# ادمین: تأیید/رد ارسال‌های دانشجویان
# ============================================================

async def approve_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    sub_id = int(query.data.split(":", 1)[1])
    pending = load_pending()
    entry = next((p for p in pending if p["id"] == sub_id), None)
    if not entry:
        await query.edit_message_text("این مورد قبلاً پردازش شده.")
        return
    if not can_manage_term(update.effective_user.id, entry["term"]):
        await deny_term_access(query, entry["term"])
        return
    pending.remove(entry)
    save_pending(pending)

    data = load_data()
    course = data.setdefault(entry["term"], {}).setdefault(entry["course"], new_course())
    ensure_course_shape(course)

    if entry["type"] == "file":
        caption = entry["caption"]
        if not entry["anonymous"]:
            caption += f" (ارسال: {entry['sender_display']})"
        course["categories"].setdefault(entry["category"], {"files": []})["files"].append(
            {
                "type": "file",
                "file_id": entry["file_id"],
                "caption": caption,
                "description": entry.get("description", ""),
                "downloads": 0,
            }
        )
        category_label = entry["category"]
        broadcast_caption = caption
    else:
        text_content = format_exam_submission(entry)
        category_label = "🩺 تجربه ارسالی دانشجویان"
        broadcast_caption = f"تجربه امتحان - {entry['answers'].get('exam_date', '')}"
        course["categories"].setdefault(category_label, {"files": []})["files"].append(
            {"type": "text", "content": text_content, "caption": broadcast_caption, "downloads": 0}
        )

    save_data(data)
    dbmod.log_action(
        update.effective_user.id, "APPROVE_SUBMISSION", term=entry["term"],
        detail=f"course={entry['course']} category={category_label}",
    )
    await query.edit_message_text("تأیید و منتشر شد ✅")

    try:
        await context.bot.send_message(
            chat_id=entry["sender_id"],
            text=(
                f"مطلبت («{entry['term']} - {entry['course']} - {category_label}») تأیید و منتشر شد ✅\n"
                "ممنون بابت مشارکتت 🙏"
            ),
        )
    except Exception:
        pass

    await broadcast_new_file(context, entry["term"], entry["course"], category_label, broadcast_caption)


async def reject_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return

    is_bad_language = query.data.startswith("reject_sub_bad:")
    sub_id = int(query.data.split(":", 1)[1])
    pending = load_pending()
    entry = next((p for p in pending if p["id"] == sub_id), None)
    if not entry:
        await query.edit_message_text("این مورد قبلاً پردازش شده.")
        return
    if not can_manage_term(update.effective_user.id, entry["term"]):
        await deny_term_access(query, entry["term"])
        return
    pending.remove(entry)
    save_pending(pending)
    dbmod.log_action(
        update.effective_user.id, "REJECT_SUBMISSION", term=entry["term"],
        detail=f"course={entry['course']}" + (" reason=الفاظ نامناسب" if is_bad_language else ""),
    )

    await query.edit_message_text("رد شد ❌ (الفاظِ نامناسب)" if is_bad_language else "رد شد ❌")
    try:
        reason_line = (
            "\nدلیل: رعایت‌نکردنِ ادب/استفاده از الفاظِ نامناسب."
            if is_bad_language
            else ""
        )
        await context.bot.send_message(
            chat_id=entry["sender_id"],
            text=f"متأسفانه مطلبی که فرستادی توسط مدیر تأیید نشد.{reason_line}",
        )
    except Exception:
        pass


# ============================================================
# ادمین: راهنمای مطالعه‌ی هر درس
# ============================================================

async def add_guide_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"pick_prog_guide:{p}")] for p in allowed]
    await update.message.reply_text(
        "راهنمای مطالعه رو برای کدوم برنامه می‌خوای بنویسی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def pick_program_for_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_guide", None)
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    data = load_data()
    courses = data.get(program, {})
    if not courses:
        await query.edit_message_text(f"«{program}» هنوز درسی نداره.")
        return
    buttons = [
        [InlineKeyboardButton(c, callback_data=f"pick_course_guide:{program}:{c}")] for c in courses.keys()
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="guide_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def guide_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_guide", None)
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"pick_prog_guide:{p}")] for p in allowed]
    await query.edit_message_text(
        "راهنمای مطالعه رو برای کدوم برنامه می‌خوای بنویسی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def pick_course_for_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, program, course_name = query.data.split(":", 2)
    context.user_data["pending_guide"] = {"term": program, "course": course_name}
    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"pick_prog_guide:{program}")]]
    )
    await query.edit_message_text(
        f"باشه، حالا متن راهنمای مطالعه‌ی «{course_name}» رو بنویس و بفرست.\n"
        "می‌تونی مثلاً بگی از کجا شروع کنن، چی مهم‌تره، چه ترتیبی بخونن.",
        reply_markup=back_btn,
    )


async def show_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course_ref = dbmod.get_course_by_id(course_id)
    if course_ref is None:
        await query.message.reply_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course_ref["term_name"], course_ref["name"]
    data = load_data()
    guide = data.get(program, {}).get(course_name, {}).get("guide", "")
    if not guide:
        await query.message.reply_text("راهنمایی برای این درس ثبت نشده.")
        return
    await query.message.reply_text(f"📖 راهنمای مطالعه‌ی «{course_name}»:\n\n{guide}")


# ============================================================
# ادمین: جابه‌جایی فایل بین دسته‌بندی‌ها (بدون حذف و آپلود دوباره)
# ============================================================

async def move_file_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"mv_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "فایلِ کدوم برنامه رو می‌خوای جابه‌جا کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def move_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"mv_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "فایلِ کدوم برنامه رو می‌خوای جابه‌جا کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def move_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    # از course_id استفاده می‌کنیم (نه اسم کامل درس) تا محدودیتِ ۶۴ بایتیِ
    # callback_data تلگرام -- که با اسمِ فارسیِ طولانی رد می‌شد و کل کیبورد
    # رندر نمی‌شد (دقیقاً همین باعث می‌شد جابه‌جاییِ فایل توی فیزیوپات ۲ و ۳ کار نکنه) -- رد نشه.
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"mv_course:{c['id']}")] for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="mv_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def move_pick_source_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"mv_cat:{course_id}:{i}")]
        for i, cat in categories_for(course["term_name"], course["name"])
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"mv_prog:{course['term_name']}")])
    await query.edit_message_text(
        f"فایل از کدوم بخشِ «{course['name']}» جابه‌جا بشه؟ (بخشِ فعلیِ فایل رو انتخاب کن)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _render_move_selection(course_id: int, cat_idx: int, files: list, selected: set) -> InlineKeyboardMarkup:
    """کیبورد چندانتخابیِ جابه‌جایی: هر فایل یه دکمه‌ی Toggle داره (☑️/⬜️) -- دقیقاً
    همون منطقِ چندانتخابیِ حذف فایل (_render_delete_selection)."""
    buttons = [
        [
            InlineKeyboardButton(
                f"{'☑️' if i in selected else '⬜️'} {display_caption(f)}",
                callback_data=f"mvsel:{course_id}:{cat_idx}:{i}",
            )
        ]
        for i, f in enumerate(files)
    ]
    if selected:
        n = len(selected)
        buttons.append(
            [InlineKeyboardButton(f"➡️ انتقال {n} موردِ انتخاب‌شده", callback_data=f"mvgo:{course_id}:{cat_idx}")]
        )
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"mv_course:{course_id}")])
    return InlineKeyboardMarkup(buttons)


async def move_pick_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = (
        data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    )
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"mv_course:{course_id}")]
    if not files:
        await query.edit_message_text(f"«{category}» هیچ فایلی نداره.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return
    # هر بار که تازه وارد این بخش می‌شیم، انتخاب قبلی (اگه بود) پاک می‌شه.
    context.user_data["mv_selection"] = {"course_id": course_id, "cat_idx": cat_idx, "indices": set()}
    await query.edit_message_text(
        f"چه فایل‌هایی از «{category}» جابه‌جا بشن؟ (می‌تونی چند فایل انتخاب کنی)",
        reply_markup=_render_move_selection(course_id, cat_idx, files, set()),
    )


async def move_toggle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """با هر بار زدنِ یه فایل، انتخاب/عدم‌انتخابش رو toggle می‌کنه (بدون جابه‌جاییِ واقعی)."""
    query = update.callback_query
    _, course_id_str, cat_idx_str, idx_str = query.data.split(":", 3)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    idx = int(idx_str)

    sel = context.user_data.get("mv_selection")
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx:
        sel = {"course_id": course_id, "cat_idx": cat_idx, "indices": set()}
        context.user_data["mv_selection"] = sel

    if idx in sel["indices"]:
        sel["indices"].discard(idx)
    else:
        sel["indices"].add(idx)
    await query.answer()

    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    if not files:
        await query.edit_message_text(f"«{category}» هیچ فایلی نداره.")
        return
    await query.edit_message_text(
        f"چه فایل‌هایی از «{category}» جابه‌جا بشن؟ (می‌تونی چند فایل انتخاب کنی)",
        reply_markup=_render_move_selection(course_id, cat_idx, files, sel["indices"]),
    )


async def move_pick_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بعد از انتخابِ فایل‌ها با «➡️ انتقال n موردِ انتخاب‌شده»، دسته‌ی مقصد رو می‌پرسه."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    src_cat_idx = int(cat_idx_str)

    sel = context.user_data.get("mv_selection")
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != src_cat_idx or not sel.get("indices"):
        await query.edit_message_text("چیزی برای جابه‌جایی انتخاب نشده.")
        return

    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    n = len(sel["indices"])
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"mvdst:{course_id}:{src_cat_idx}:{i}")]
        for i, cat in categories_for(course["term_name"], course["name"])
        if i != src_cat_idx
    ]
    buttons.append(
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"mv_cat:{course_id}:{src_cat_idx}")]
    )
    await query.edit_message_text(
        f"این {n} فایل کجا منتقل بشن؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def move_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """جابه‌جاییِ نهاییِ همه‌ی فایل‌های انتخاب‌شده به دسته‌ی مقصد."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, src_cat_idx_str, dest_cat_idx_str = query.data.split(":", 3)
    course_id = int(course_id_str)
    src_cat_idx = int(src_cat_idx_str)
    dest_cat_idx = int(dest_cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    program = course["term_name"]
    course_name = course["name"]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    sel = context.user_data.pop("mv_selection", None)
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != src_cat_idx or not sel.get("indices"):
        await query.edit_message_text("چیزی برای جابه‌جایی انتخاب نشده.")
        return

    src_category = category_name_by_id(src_cat_idx)
    dest_category = category_name_by_id(dest_cat_idx)
    if src_category is None or dest_category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return

    data = load_data()
    src_files = data.get(program, {}).get(course_name, {}).get("categories", {}).get(src_category, {}).get("files", [])
    # از آخر به اول pop می‌کنیم که ایندکس‌های باقی‌مونده جابه‌جا نشن.
    indices = sorted((i for i in sel["indices"] if i < len(src_files)), reverse=True)
    if not indices:
        await query.edit_message_text("فایل‌های انتخاب‌شده دیگه در دسترس نیستن.")
        return

    dest_files = data[program][course_name]["categories"].setdefault(dest_category, {"files": []}).setdefault(
        "files", []
    )
    # پاپ از آخر به اول (برای امنیتِ ایندکس) ولی append به ترتیبِ اصلیِ فایل‌ها،
    # تا توی مقصد هم ترتیبِ انتخاب‌شده حفظ بشه.
    popped = {}
    for i in indices:
        popped[i] = src_files.pop(i)
    moved_names = []
    for i in sorted(popped):
        file_info = popped[i]
        moved_names.append(display_caption(file_info))
        dest_files.append(file_info)
    save_data(data)
    n = len(moved_names)
    dbmod.log_action(
        update.effective_user.id, "MOVE_FILE", term=program,
        detail=f"course={course_name} {src_category}->{dest_category} count={n} captions={', '.join(moved_names)}",
    )

    listing = "\n".join(f"• {name}" for name in moved_names)
    await query.edit_message_text(
        f"{n} فایل از «{src_category}» به «{dest_category}» منتقل شد ✅\n\n{listing}"
    )


# ============================================================
# ادمین: حذف فایل
# ============================================================

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"del_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "می‌خوای از کدوم برنامه فایل حذف کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def delete_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"del_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "می‌خوای از کدوم برنامه فایل حذف کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def delete_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    # از course_id استفاده می‌کنیم (نه اسم کامل درس) تا محدودیتِ ۶۴ بایتیِ
    # callback_data تلگرام -- که با اسمِ فارسیِ طولانی یا فایل زیاد رد می‌شد -- دیگه رد نشه.
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"del_course:{c['id']}")] for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="del_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def delete_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"del_cat:{course_id}:{i}")]
        for i, cat in categories_for(course["term_name"], course["name"])
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"del_prog:{course['term_name']}")])
    await query.edit_message_text(
        f"از کدوم بخشِ «{course['name']}» فایل حذف کنم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


def _render_delete_selection(course_id: int, cat_idx: int, files: list, selected: set) -> InlineKeyboardMarkup:
    """کیبورد چندانتخابیِ حذف: هر فایل یه دکمه‌ی Toggle داره (☑️/⬜️)."""
    buttons = [
        [
            InlineKeyboardButton(
                f"{'☑️' if i in selected else '⬜️'} {display_caption(f)}",
                callback_data=f"delsel:{course_id}:{cat_idx}:{i}",
            )
        ]
        for i, f in enumerate(files)
    ]
    if selected:
        n = len(selected)
        buttons.append(
            [InlineKeyboardButton(f"🗑 حذف فایل‌های انتخاب‌شده ({n})", callback_data=f"delgo:{course_id}:{cat_idx}")]
        )
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"del_course:{course_id}")])
    return InlineKeyboardMarkup(buttons)


async def delete_pick_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = (
        data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    )
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"del_course:{course_id}")]
    if not files:
        await query.edit_message_text(f"«{category}» هیچ فایلی نداره.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return
    # هر بار که تازه وارد این بخش می‌شیم، انتخاب قبلی (اگه بود) پاک می‌شه.
    context.user_data["del_selection"] = {"course_id": course_id, "cat_idx": cat_idx, "indices": set()}
    await query.edit_message_text(
        f"چه فایل‌هایی از «{category}» حذف بشن؟ (می‌تونی چند فایل انتخاب کنی)",
        reply_markup=_render_delete_selection(course_id, cat_idx, files, set()),
    )


async def delete_toggle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """با هر بار زدنِ یه فایل، انتخاب/عدم‌انتخابش رو toggle می‌کنه (بدون حذف واقعی)."""
    query = update.callback_query
    _, course_id_str, cat_idx_str, idx_str = query.data.split(":", 3)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    idx = int(idx_str)

    sel = context.user_data.get("del_selection")
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx:
        sel = {"course_id": course_id, "cat_idx": cat_idx, "indices": set()}
        context.user_data["del_selection"] = sel

    if idx in sel["indices"]:
        sel["indices"].discard(idx)
    else:
        sel["indices"].add(idx)
    await query.answer()

    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    if not files:
        await query.edit_message_text(f"«{category}» هیچ فایلی نداره.")
        return
    await query.edit_message_text(
        f"چه فایل‌هایی از «{category}» حذف بشن؟ (می‌تونی چند فایل انتخاب کنی)",
        reply_markup=_render_delete_selection(course_id, cat_idx, files, sel["indices"]),
    )


async def delete_confirm_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🗑 حذف فایل‌های انتخاب‌شده -- قبل از حذف واقعی، یه تأیید نهایی می‌گیره."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)

    sel = context.user_data.get("del_selection")
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx or not sel.get("indices"):
        await query.edit_message_text("چیزی برای حذف انتخاب نشده.")
        return

    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    indices = sorted(i for i in sel["indices"] if i < len(files))
    if not indices:
        await query.edit_message_text("فایل‌های انتخاب‌شده دیگه در دسترس نیستن.")
        return

    n = len(indices)
    listing = "\n".join(f"❌ {display_caption(files[i])}" for i in indices)
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ بله، {n} فایل حذف بشه", callback_data=f"delyes:{course_id}:{cat_idx}")],
            [InlineKeyboardButton("↩️ انصراف", callback_data=f"del_cat:{course_id}:{cat_idx}")],
        ]
    )
    await query.edit_message_text(
        f"مطمئنی می‌خوای این {n} فایل از «{category}» حذف بشه؟\n\n{listing}",
        reply_markup=buttons,
    )


async def delete_execute_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✅ بله -- حذف نهاییِ همه‌ی فایل‌های انتخاب‌شده."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    program = course["term_name"]
    course_name = course["name"]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return

    sel = context.user_data.pop("del_selection", None)
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx or not sel.get("indices"):
        await query.edit_message_text("چیزی برای حذف انتخاب نشده.")
        return

    data = load_data()
    files = data.get(program, {}).get(course_name, {}).get("categories", {}).get(category, {}).get("files", [])
    # از آخر به اول حذف می‌کنیم که ایندکس‌های باقی‌مونده جابه‌جا نشن.
    indices = sorted((i for i in sel["indices"] if i < len(files)), reverse=True)
    if not indices:
        await query.edit_message_text("فایل‌های انتخاب‌شده دیگه در دسترس نیستن.")
        return

    removed_names = []
    for i in indices:
        removed = files.pop(i)
        removed_names.append(removed.get("caption", ""))
    save_data(data)
    dbmod.log_action(
        update.effective_user.id, "DELETE_FILE", term=program,
        detail=f"course={course_name} category={category} count={len(removed_names)} captions={', '.join(removed_names)}",
    )
    n = len(removed_names)
    listing = "\n".join(f"• {name}" for name in reversed(removed_names))
    await query.edit_message_text(f"{n} فایل حذف شد ✅\n\n{listing}")


# ============================================================
# ادمین: نشان کیفیت (MedVerse Verified)
# ============================================================
# نکته: تمام callbackهای این بخش فقط با ID/FID کوتاه ساخته می‌شن.
# نام فارسی ترم/درس داخل callback_data قرار نمی‌گیره تا از سقف ۶۴ بایت تلگرام رد نشه.

def _badge_term_buttons(user_id: int) -> list:
    buttons = []
    for term_name in admin_allowed_programs(user_id):
        term_id = dbmod.get_term_id_by_name(term_name)
        if term_id is None:
            continue
        buttons.append([InlineKeyboardButton(term_name, callback_data=f"bdg_prog:{term_id}")])
    return buttons


async def badge_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    buttons = _badge_term_buttons(update.effective_user.id)
    await update.message.reply_text(
        "می‌خوای به فایلِ کدوم برنامه نشان کیفیت بدی؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def badge_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = _badge_term_buttons(update.effective_user.id)
    await query.edit_message_text(
        "می‌خوای به فایلِ کدوم برنامه نشان کیفیت بدی؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def badge_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]

    # فرمت جدید: bdg_prog:{term_id}
    # فرمت قدیمی با نام ترم هم برای دکمه‌های بازِ قبلی پشتیبانی می‌شود.
    if token.isdigit():
        term = dbmod.get_term_by_id(int(token))
        if term is None:
            await query.edit_message_text("این ترم دیگه وجود نداره.")
            return
        program = term["name"]
    else:
        program = token

    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return

    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"bdg_course:{c['id']}")]
        for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="bdg_back_progs")])
    await query.edit_message_text(
        f"کدوم درسِ «{program}»؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def badge_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    payload = query.data.split(":", 1)[1]

    # فرمت جدید: bdg_course:{course_id}
    if payload.isdigit():
        course = dbmod.get_course_by_id(int(payload))
        if course is None:
            await query.edit_message_text("این درس دیگه وجود نداره.")
            return
        course_id = course["id"]
        program = course["term_name"]
        course_name = course["name"]
    else:
        # سازگاری با callback قدیمی: bdg_course:{program}:{course_name}
        try:
            program, course_name = payload.split(":", 1)
        except ValueError:
            await query.edit_message_text("درخواست نامعتبره؛ دوباره از /setbadge وارد شو.")
            return
        course_id = dbmod.get_course_id_by_name(program, course_name)
        if course_id is None:
            await query.edit_message_text("این درس دیگه وجود نداره.")
            return

    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    cats = categories_for(program, course_name)
    buttons = [
        [InlineKeyboardButton(cat_name, callback_data=f"bdg_cat:{course_id}:{cat_id}")]
        for cat_id, cat_name in cats
    ]
    term_id = dbmod.get_term_id_by_name(program)
    back_data = f"bdg_prog:{term_id}" if term_id is not None else "bdg_back_progs"
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back_data)])
    await query.edit_message_text(
        f"فایل کدوم بخشِ «{course_name}»؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def badge_pick_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")

    # فرمت جدید: bdg_cat:{course_id}:{category_id}
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        course_id = int(parts[1])
        category_id = int(parts[2])
        course = dbmod.get_course_by_id(course_id)
        cat = dbmod.get_category_by_id(category_id)
        if course is None or cat is None or cat["course_id"] != course_id:
            await query.edit_message_text("این درس یا بخش دیگه وجود نداره.")
            return
        program = course["term_name"]
        course_name = course["name"]
        category = cat["name"]
    else:
        # سازگاری با فرمت قدیمی: bdg_cat:{program}:{course}:{category_id}
        if len(parts) < 4:
            await query.edit_message_text("درخواست نامعتبره؛ دوباره از /setbadge وارد شو.")
            return
        program = parts[1]
        course_name = parts[2]
        try:
            category_id = int(parts[3])
        except ValueError:
            await query.edit_message_text("بخش نامعتبره.")
            return
        course_id = dbmod.get_course_id_by_name(program, course_name)
        cat = dbmod.get_category_by_id(category_id)
        if course_id is None or cat is None:
            await query.edit_message_text("این درس یا بخش دیگه وجود نداره.")
            return
        category = cat["name"]

    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    data = load_data()
    files = (
        data.get(program, {})
        .get(course_name, {})
        .get("categories", {})
        .get(category, {})
        .get("files", [])
    )
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"bdg_course:{course_id}")]
    if not files:
        await query.edit_message_text(
            f"«{category}» هیچ فایلی نداره.",
            reply_markup=InlineKeyboardMarkup([back_btn]),
        )
        return

    buttons = []
    for f in files:
        fid = (f.get("fid") or "").strip()
        if not fid:
            continue
        cb = f"bdg_file:{fid}:{category_id}"
        if validate_callback_data(cb, context_label="badge_pick_file/fid"):
            buttons.append([
                InlineKeyboardButton(
                    display_caption(f) or f.get("caption", "") or "بدون نام",
                    callback_data=cb,
                )
            ])
    buttons.append(back_btn)
    await query.edit_message_text(
        f"کدوم فایل از «{category}» نشان بگیره؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def badge_pick_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")

    # فرمت جدید: bdg_file:{fid}:{category_id}
    if len(parts) == 3:
        _, fid, category_id_str = parts
        data = load_data()
        found = find_file_by_fid(data, fid)
        if not found:
            await query.edit_message_text("این فایل دیگه در دسترس نیست.")
            return
        program, course_name, _category, _index, _file_info = found
        if not can_manage_term(update.effective_user.id, program):
            await deny_term_access(query, program)
            return
        course_id = dbmod.get_course_id_by_name(program, course_name)
        if course_id is None:
            await query.edit_message_text("درس این فایل پیدا نشد.")
            return
        buttons = [
            [InlineKeyboardButton(f"{emoji} {key}", callback_data=f"bdg_set:{fid}:{key}")]
            for key, emoji in QUALITY_BADGES.items()
        ]
        buttons.append([InlineKeyboardButton("🚫 حذف نشان", callback_data=f"bdg_set:{fid}:none")])
        buttons.append([
            InlineKeyboardButton(
                "🔙 بازگشت",
                callback_data=f"bdg_cat:{course_id}:{int(category_id_str)}",
            )
        ])
        await query.edit_message_text("کدوم نشان؟", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # فرمت قدیمی: bdg_file:{program}:{course}:{cat}:{index}
    if len(parts) == 5:
        _, program, course_name, cat_idx_str, index_str = parts
        category = category_name_by_id(int(cat_idx_str))
        data = load_data()
        files = (
            data.get(program, {})
            .get(course_name, {})
            .get("categories", {})
            .get(category or "", {})
            .get("files", [])
        )
        try:
            file_info = files[int(index_str)]
        except (ValueError, IndexError):
            await query.edit_message_text("این فایل دیگه در دسترس نیست.")
            return
        fid = file_info.get("fid")
        if not fid:
            await query.edit_message_text("این فایل شناسه‌ی معتبر نداره.")
            return
        query.data = f"bdg_file:{fid}:{cat_idx_str}"
        await badge_pick_value(update, context)
        return

    await query.edit_message_text("درخواست نشان نامعتبره؛ دوباره از /setbadge وارد شو.")


async def badge_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")

    # فرمت جدید: bdg_set:{fid}:{badge_key}
    if len(parts) == 3:
        _, fid, badge_key = parts
        data = load_data()
        found = find_file_by_fid(data, fid)
        if not found:
            await query.edit_message_text("این فایل دیگه در دسترس نیست.")
            return

        program, course_name, _category, _index, file_info = found
        if not can_manage_term(update.effective_user.id, program):
            await deny_term_access(query, program)
            return
        if badge_key != "none" and badge_key not in QUALITY_BADGES:
            await query.edit_message_text("نشان انتخاب‌شده معتبر نیست.")
            return

        if badge_key == "none":
            file_info.pop("badge", None)
            action = "REMOVE_BADGE"
            message = "نشان برداشته شد ✅"
            detail = f"course={course_name} fid={fid}"
        else:
            file_info["badge"] = badge_key
            action = "SET_BADGE"
            message = f"نشان {QUALITY_BADGES.get(badge_key, '')} اضافه شد ✅"
            detail = f"course={course_name} fid={fid} badge={badge_key}"

        save_data(data)
        dbmod.log_action(
            update.effective_user.id,
            action,
            term=program,
            detail=detail,
        )
        await query.edit_message_text(message)
        return

    # فرمت قدیمی: bdg_set:{program}:{course}:{cat}:{index}:{badge}
    if len(parts) == 6:
        _, program, course_name, cat_idx_str, index_str, badge_key = parts
        category = category_name_by_id(int(cat_idx_str))
        data = load_data()
        files = (
            data.get(program, {})
            .get(course_name, {})
            .get("categories", {})
            .get(category or "", {})
            .get("files", [])
        )
        try:
            file_info = files[int(index_str)]
        except (ValueError, IndexError):
            await query.edit_message_text("این فایل دیگه در دسترس نیست.")
            return
        fid = file_info.get("fid")
        if not fid:
            await query.edit_message_text("این فایل شناسه‌ی معتبر نداره.")
            return
        query.data = f"bdg_set:{fid}:{badge_key}"
        await badge_apply(update, context)
        return

    await query.edit_message_text("درخواست نامعتبره؛ دوباره از /setbadge وارد شو.")


# ============================================================
# ادمین: لینک مستقیم فایل
# ============================================================

def _direct_file_link(bot_username: str, fid: str) -> str:
    username = (bot_username or "MedVersegoums_bot").lstrip("@")
    return f"https://t.me/{username}?start=file_{fid}"


async def filelink_show_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """برای ادمین لینک مستقیم یک فایل را نشان می‌دهد."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        await query.answer("این بخش فقط برای ادمینه.", show_alert=True)
        return

    _, fid = query.data.split(":", 1)
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await query.edit_message_text("این فایل دیگه پیدا نشد.")
        return

    program, course_name, _category, _index, file_info = found
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    link = _direct_file_link(getattr(context.bot, "username", None), fid)
    caption = display_caption(file_info) or file_info.get("caption", "") or "فایل"
    buttons = [
        [InlineKeyboardButton("🔗 باز کردن لینک", url=link)],
    ]
    await query.message.reply_text(
        f"🔗 لینک مستقیم «{caption}»:\n\n{link}\n\n"
        "برای کپی، روی خود لینک لمس/کلیک طولانی کن.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def filelink_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مسیر مستقل برای پیدا کردن لینک فایل: ترم ← درس ← بخش ← فایل."""
    if not is_admin(update.effective_user.id):
        return
    buttons = []
    for term_name in admin_allowed_programs(update.effective_user.id):
        term_id = dbmod.get_term_id_by_name(term_name)
        if term_id is not None:
            buttons.append([InlineKeyboardButton(term_name, callback_data=f"fl_term:{term_id}")])
    await update.message.reply_text(
        "🔗 لینک مستقیم فایل\nترم رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def filelink_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    term_id = int(query.data.split(":", 1)[1])
    term = dbmod.get_term_by_id(term_id)
    if term is None:
        await query.edit_message_text("این ترم دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, term["name"]):
        await deny_term_access(query, term["name"])
        return
    courses = dbmod.get_courses_with_ids(term["name"])
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"fl_course:{c['id']}")]
        for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="fl_back_terms")])
    await query.edit_message_text(
        f"درسِ «{term['name']}» رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def filelink_back_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = []
    for term_name in admin_allowed_programs(update.effective_user.id):
        term_id = dbmod.get_term_id_by_name(term_name)
        if term_id is not None:
            buttons.append([InlineKeyboardButton(term_name, callback_data=f"fl_term:{term_id}")])
    await query.edit_message_text(
        "🔗 لینک مستقیم فایل\nترم رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def filelink_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return
    cats = categories_for(course["term_name"], course["name"])
    buttons = [
        [InlineKeyboardButton(cat_name, callback_data=f"fl_cat:{course_id}:{cat_id}")]
        for cat_id, cat_name in cats
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"fl_term:{course['term_id']}")])
    await query.edit_message_text(
        f"بخشِ «{course['name']}» رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def filelink_pick_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, category_id_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    category_id = int(category_id_str)
    course = dbmod.get_course_by_id(course_id)
    cat = dbmod.get_category_by_id(category_id)
    if course is None or cat is None or cat["course_id"] != course_id:
        await query.edit_message_text("این درس یا بخش دیگه وجود نداره.")
        return
    if not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"])
        return

    data = load_data()
    files = (
        data.get(course["term_name"], {})
        .get(course["name"], {})
        .get("categories", {})
        .get(cat["name"], {})
        .get("files", [])
    )
    buttons = []
    for f in files:
        fid = (f.get("fid") or "").strip()
        if not fid:
            continue
        buttons.append([
            InlineKeyboardButton(
                display_caption(f) or f.get("caption", "") or "بدون نام",
                callback_data=f"filelink_show:{fid}",
            )
        ])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"fl_course:{course_id}")])
    await query.edit_message_text(
        f"کدوم فایل از «{cat['name']}»؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# ادمین: تغییر نام واقعیِ فایل روی تلگرام (بدون آپلود دستی مجدد)
# ============================================================
# روش کار: فایل با file_id فعلی از سرور تلگرام دانلود می‌شه، دوباره با
# نام جدید آپلود می‌شه (محتوا عیناً همون محتواست)، و file_id تازه جایگزین
# قبلی توی دیتابیس می‌شه. تلگرام اجازه‌ی تغییر نام مستقیم روی file_id
# قدیمی رو نمی‌ده، این تنها راهیه که بدون دخالت دستی ادمین کار می‌کنه.
# محدودیت تلگرام: دانلود فایل توسط ربات فقط تا ۲۰ مگابایت ممکنه.

async def rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ren_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "می‌خوای فایل کدوم برنامه رو تغییر نام بدی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def rename_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_rename", None)
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"ren_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "می‌خوای فایل کدوم برنامه رو تغییر نام بدی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def rename_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_rename", None)
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    # از course_id استفاده می‌کنیم (نه اسم کامل درس) تا محدودیتِ ۶۴ بایتیِ
    # callback_data تلگرام رد نشه -- همون مشکلی که توی movefile داشتیم، اینجا با
    # اضافه‌شدنِ fid به callback حتی زودتر رد می‌شد (به همین خاطر rename بیشتر
    # از موارد دیگه fail می‌کرد).
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"ren_course:{c['id']}")] for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ren_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def rename_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"ren_cat:{course_id}:{i}")]
        for i, cat in categories_for(course["term_name"], course["name"])
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ren_prog:{course['term_name']}")])
    await query.edit_message_text(
        f"فایل کدوم بخشِ «{course['name']}»؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def rename_pick_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    # فقط فایل‌های واقعیِ ذخیره‌شده روی تلگرام قابل تغییر نامن (نه لینک یا متن)
    renamable = [f for f in files if f.get("type") == "file" and f.get("fid")]
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"ren_course:{course_id}")]
    if not renamable:
        await query.edit_message_text(f"«{category}» فایل قابل تغییر نامی نداره.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return
    buttons = [
        [InlineKeyboardButton(display_caption(f), callback_data=f"ren_file:{f['fid']}:{course_id}:{cat_idx}")]
        for f in renamable
    ]
    buttons.append(back_btn)
    await query.edit_message_text(
        f"کدوم فایل از «{category}» تغییر نام بگیره؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def rename_ask_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    fid = parts[1]
    # مسیر برگشت (course_id:cat_idx) اگه توی callback بود استفاده می‌شه؛
    # برای سازگاری با callback_dataهای قدیمی‌تر (بدون این بخش) هم کار می‌کنه.
    back_markup = None
    if len(parts) >= 4:
        back_course_id, back_cat_idx = parts[2], parts[3]
        back_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"ren_cat:{back_course_id}:{back_cat_idx}")]]
        )
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await query.edit_message_text("این فایل دیگه در دسترس نیست.")
        return
    file_program, _, _, _, file_info = found
    if not can_manage_term(update.effective_user.id, file_program):
        await deny_term_access(query, file_program)
        return
    context.user_data["pending_rename"] = {"fid": fid}
    await query.edit_message_text(
        f"اسم فعلی: «{display_caption(file_info)}»\n\n"
        "اسم جدید فایل رو بفرست (لازم نیست پسوند رو بنویسی؛ اگه ننویسی، پسوندِ فایلِ فعلی خودش حفظ می‌شه).\n"
        "برای انصراف /cancel رو بفرست.",
        reply_markup=back_markup,
    )


async def rename_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگه منتظر اسم جدید برای rename بودیم، اینجا اجراش می‌کنه.
    خروجی True یعنی این پیام مصرف شد و receive_text نباید ادامه بده."""
    pending = context.user_data.get("pending_rename")
    if not pending:
        return False

    context.user_data.pop("pending_rename", None)
    new_name = update.message.text.strip()

    if new_name == "/cancel":
        await update.message.reply_text("تغییر نام لغو شد.")
        return True
    if not new_name:
        await update.message.reply_text("اسم نامعتبره؛ دوباره با /rename امتحان کن.")
        return True

    fid = pending["fid"]
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await update.message.reply_text("این فایل دیگه در دسترس نیست.")
        return True

    file_program, _, _, _, file_info = found
    if not can_manage_term(update.effective_user.id, file_program):
        await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{file_program}» رو ندارید.")
        return True
    if file_info.get("type") != "file":
        await update.message.reply_text("این مورد فایل واقعیِ تلگرامی نیست و قابل تغییر نام نیست.")
        return True

    old_file_id = file_info["file_id"]
    old_caption = file_info.get("caption", "")
    # پسوند رو خودمون از اسمِ فعلی (که همیشه پسوندِ درست رو داره) می‌گیریم، تا کاربر
    # مجبور نباشه پسوند رو دوباره تایپ کنه.
    new_name = apply_original_extension(new_name, old_caption)
    status_msg = await update.message.reply_text("⏳ در حال دانلود و آپلود مجدد فایل با نام جدید...")

    try:
        tg_file = await context.bot.get_file(old_file_id)
        buffer = io.BytesIO()
        await tg_file.download_to_memory(out=buffer)
        buffer.seek(0)
        sent = await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=buffer,
            filename=new_name,
        )
        new_file_id = sent.document.file_id
    except Exception as exc:
        logger.exception("خطا توی تغییر نام فایل (fid=%s)", fid)
        await status_msg.edit_text(
            "❌ تغییر نام انجام نشد. تلگرام فقط اجازه‌ی دانلود فایل‌های تا ۲۰ مگابایت رو به ربات می‌ده؛ "
            f"اگه فایل بزرگ‌تره، احتمالاً دلیلش همینه.\nخطا: {exc}"
        )
        return True

    # ممکنه بین این مدت دیتا عوض شده باشه، دوباره تازه‌ش می‌کنیم
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await status_msg.edit_text(
            "⚠️ فایل حین آپلود مجدد از دیتابیس حذف شده بود. نسخه‌ی جدید بالا با نام تازه موجوده، "
            "ولی به‌صورت خودکار جایگزین نشد."
        )
        return True

    file_program2, _, _, _, file_info = found
    file_info["file_id"] = new_file_id
    file_info["caption"] = new_name
    save_data(data)
    dbmod.log_action(
        update.effective_user.id, "RENAME_FILE", term=file_program2,
        detail=f"از «{old_caption}» به «{new_name}»",
    )

    await status_msg.edit_text(
        f"✅ نام فایل تغییر کرد.\nقبلی: «{old_caption}»\nجدید: «{new_name}»\n\n"
        "پیام آپلود مجدد بالا رو می‌تونی پاک کنی؛ فایل توی ربات همچنان کامل در دسترسه (همون محتوا، با نام جدید)."
    )
    return True


# ============================================================
# ادمین: ویرایش توضیحِ (کپشنِ تلگرامیِ) یه فایل
# ============================================================
# توضیح = همون متنی که ادمین موقع فرستادنِ فایل به‌عنوان کپشنِ تلگرامی تایپ می‌کنه
# (ستونِ description توی دیتابیس) -- جدا از caption که اسمِ نمایشیِ فایله (/rename).
# برخلافِ /rename، اینجا نیازی به دانلود/آپلودِ مجددِ فایل نیست؛ فقط متنِ توضیح
# مستقیم توی دیتابیس عوض می‌شه (همون فلسفه‌ی id-based مثلِ edit_note).

async def editdesc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"desc_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "می‌خوای توضیحِ فایلِ کدوم برنامه رو ویرایش کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def editdesc_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_desc_edit", None)
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"desc_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "می‌خوای توضیحِ فایلِ کدوم برنامه رو ویرایش کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def editdesc_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_desc_edit", None)
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    buttons = [
        [InlineKeyboardButton(c["name"], callback_data=f"desc_course:{c['id']}")] for c in courses
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="desc_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def editdesc_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"desc_cat:{course_id}:{i}")]
        for i, cat in categories_for(course["term_name"], course["name"])
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"desc_prog:{course['term_name']}")])
    await query.edit_message_text(
        f"توضیحِ فایل کدوم بخشِ «{course['name']}» ویرایش بشه؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def editdesc_pick_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    cat_idx = int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    data = load_data()
    files = data.get(course["term_name"], {}).get(course["name"], {}).get("categories", {}).get(category, {}).get("files", [])
    # فقط فایل‌های واقعیِ آپلودی (نه لینک/نوت) کپشنِ تلگرامی (description) دارن.
    editable = [f for f in files if f.get("type") == "file" and f.get("fid")]
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"desc_course:{course_id}")]
    if not editable:
        await query.edit_message_text(f"«{category}» فایل قابل‌ویرایشی نداره.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return
    buttons = [
        [InlineKeyboardButton(display_caption(f), callback_data=f"desc_file:{f['fid']}:{course_id}:{cat_idx}")]
        for f in editable
    ]
    buttons.append(back_btn)
    await query.edit_message_text(
        f"توضیحِ کدوم فایل از «{category}» ویرایش بشه؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def editdesc_ask_new_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    fid = parts[1]
    # مسیر برگشت (course_id:cat_idx) اگه توی callback بود استفاده می‌شه؛ برای سازگاری
    # با callback_dataهای قدیمی‌تر (بدون این بخش) هم کار می‌کنه.
    back_markup = None
    if len(parts) >= 4:
        back_course_id, back_cat_idx = parts[2], parts[3]
        back_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"desc_cat:{back_course_id}:{back_cat_idx}")]]
        )
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await query.edit_message_text("این فایل دیگه در دسترس نیست.")
        return
    file_program, _, _, _, file_info = found
    if not can_manage_term(update.effective_user.id, file_program):
        await deny_term_access(query, file_program)
        return
    context.user_data["pending_desc_edit"] = {"fid": fid}
    current = file_info.get("description") or "—"
    await query.edit_message_text(
        f"فایل: «{display_caption(file_info)}»\nتوضیحِ فعلی: «{current}»\n\n"
        "توضیحِ جدید رو بفرست.\nبرای پاک کردنِ توضیح، فقط یه خط‌تیره (-) بفرست.\n"
        "برای انصراف /cancel رو بفرست.",
        reply_markup=back_markup,
    )


async def editdesc_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگه منتظر متنِ جدیدِ توضیح بودیم، اینجا اجراش می‌کنه.
    خروجی True یعنی این پیام مصرف شد و receive_text نباید ادامه بده."""
    pending = context.user_data.get("pending_desc_edit")
    if not pending:
        return False

    context.user_data.pop("pending_desc_edit", None)
    new_text = update.message.text.strip()

    if new_text == "/cancel":
        await update.message.reply_text("ویرایشِ توضیح لغو شد.")
        return True

    fid = pending["fid"]
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await update.message.reply_text("این فایل دیگه در دسترس نیست.")
        return True

    file_program, _, _, _, file_info = found
    if not can_manage_term(update.effective_user.id, file_program):
        await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{file_program}» رو ندارید.")
        return True
    if file_info.get("type") != "file":
        await update.message.reply_text("این مورد فایل واقعیِ آپلودی نیست و توضیح‌پذیر نیست.")
        return True

    new_description = "" if new_text == "-" else new_text
    if not dbmod.edit_file_description(fid, new_description):
        await update.message.reply_text("این فایل دیگه در دسترس نیست.")
        return True

    dbmod.log_action(
        update.effective_user.id, "EDIT_FILE_DESCRIPTION", term=file_program,
        detail=f"caption={display_caption(file_info)}",
    )
    shown = new_description or "(بدون توضیح)"
    await update.message.reply_text(
        f"✅ توضیحِ فایل «{display_caption(file_info)}» به این تغییر کرد:\n«{shown}»\n\n"
        "⚠️ توجه: این تغییر فقط روی دانلودهای بعدی اثر می‌ذاره. اگه این فایل رو قبلاً "
        "برای کسی فرستاده بودی، همون پیامِ قدیمی توی چتش با کپشنِ قدیمی می‌مونه (تلگرام "
        "اجازه نمی‌ده پیامِ قبلاً ارسال‌شده ویرایش بشه) -- این عادیه و ربطی به دیتابیس نداره."
    )
    return True


# ============================================================
# ادمین: مدیریت گروه‌ها و نوت‌ها (فاز ۷ -- لایه‌ی زیرِ دسته‌بندیِ فعلی)
#
# مسیر: /filegroups -> برنامه -> درس -> دسته‌بندی -> (ایجاد گروه / گروه‌های
# موجود / مدیریتِ فایل‌های بدون‌گروه) -> داخل هر گروه: افزودن فایل، افزودن
# نوت، مشاهده‌ی محتوا (با امکانِ برگردوندنِ هر آیتم به «بدون‌گروه»)، ویرایشِ
# نام، حذف، جابه‌جاییِ ترتیب.
#
# آدرس‌دهی همه‌جا با course_id/cat_idx/group_id/fid انجام می‌شه (نه اسمِ
# فارسیِ درس/دسته) -- همون دلیلِ محدودیتِ ۶۴ بایتیِ callback_data تلگرام که
# توی build_course_category_buttons و rename_pick_course توضیح داده شده.
# ============================================================

async def filegroups_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"fgm_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "می‌خوای گروه/نوتِ کدوم برنامه رو مدیریت کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def fgm_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"fgm_course:{c['id']}")] for c in courses]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="fgm_back_progs")])
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def fgm_back_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"fgm_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "می‌خوای گروه/نوتِ کدوم برنامه رو مدیریت کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def fgm_pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course["term_name"], course["name"]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    buttons = [
        [InlineKeyboardButton(cat, callback_data=f"fgm_cat:{course_id}:{i}")]
        for i, cat in categories_for(program, course_name)
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_prog:{program}")])
    await query.edit_message_text(
        f"کدوم بخشِ «{course_name}»؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _render_fgm_category(query, course_id: int, cat_idx: int) -> None:
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    course_name = course["name"]
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    groups = dbmod.get_file_groups(course_id, category_id)

    buttons = []
    for g in groups:
        buttons.append([InlineKeyboardButton(f"📁 {g['name']}", callback_data=f"fgm_group:{course_id}:{cat_idx}:{g['id']}")])
    buttons.append([InlineKeyboardButton("➕ گروه جدید", callback_data=f"fgm_newgroup:{course_id}:{cat_idx}")])
    buttons.append([InlineKeyboardButton("📄 مدیریت فایل‌های بدون‌گروه", callback_data=f"fgm_ungrouped:{course_id}:{cat_idx}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_course:{course_id}")])
    await query.edit_message_text(
        f"«{course_name} → {category}» — گروه‌ها:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def fgm_show_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    await _render_fgm_category(query, int(course_id_str), int(cat_idx_str))


async def fgm_prompt_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.edit_message_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_new_group_name"] = {"course_id": course_id, "cat_idx": cat_idx}
    await query.edit_message_text(
        "اسمِ گروهِ جدید رو بفرست (مثلاً «دکتر احمدی» یا «جلسات اول»).\nبرای انصراف /cancel رو بفرست."
    )


async def fgm_show_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    await _render_fgm_group(query, course_id, cat_idx, group_id)


async def _render_fgm_group(query, course_id: int, cat_idx: int, group_id: int) -> None:
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    group = dbmod.get_file_group(group_id)
    if group is None:
        await query.edit_message_text("این گروه دیگه پیدا نشد؛ شاید حذف شده.")
        return
    content = dbmod.get_group_content(course_id, category_id, group_id)

    buttons = [
        [InlineKeyboardButton("➕ افزودن فایل", callback_data=f"fgm_upload:{course_id}:{cat_idx}:{group_id}")],
        [InlineKeyboardButton("📝 افزودن نوت", callback_data=f"fgm_addnote:{course_id}:{cat_idx}:{group_id}")],
        [InlineKeyboardButton(f"📂 محتوای گروه ({len(content)})", callback_data=f"fgm_content:{course_id}:{cat_idx}:{group_id}")],
        [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"fgm_ren:{course_id}:{cat_idx}:{group_id}")],
        [
            InlineKeyboardButton("⬆️", callback_data=f"fgm_up:{course_id}:{cat_idx}:{group_id}"),
            InlineKeyboardButton("⬇️", callback_data=f"fgm_down:{course_id}:{cat_idx}:{group_id}"),
        ],
        [InlineKeyboardButton("🗑 حذف گروه", callback_data=f"fgm_del:{course_id}:{cat_idx}:{group_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_cat:{course_id}:{cat_idx}")],
    ]
    await query.edit_message_text(
        f"«{course['name']} → {category} → {group['name']}»:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def fgm_upload_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await pick_group_for_upload(update, context)


async def fgm_prompt_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.edit_message_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_note_text"] = {"course_id": course_id, "cat_idx": cat_idx, "group_id": group_id}
    await query.edit_message_text(
        "متنِ نوت رو بفرست (مثلاً «جلسه‌ی سوم توسط این استاد ارائه نشده است»).\nبرای انصراف /cancel رو بفرست."
    )


async def fgm_show_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    group = dbmod.get_file_group(group_id)
    if group is None:
        await query.edit_message_text("این گروه دیگه پیدا نشد؛ شاید حذف شده.")
        return
    content = dbmod.get_group_content(course_id, category_id, group_id)
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_group:{course_id}:{cat_idx}:{group_id}")]
    if not content:
        await query.edit_message_text(f"«{group['name']}» هنوز چیزی نداره.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return

    buttons = []
    for item in content:
        label = ("📝 " + (item.get("caption") or "نوت")) if item["type"] == "note" else ("📄 " + (item.get("caption") or ""))
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"fgm_item:{item['fid']}:{course_id}:{cat_idx}:{group_id}")])
    buttons.append(back_btn)
    await query.edit_message_text(f"محتوای «{group['name']}» — یه مورد رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))


async def fgm_show_item_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, fid, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 4)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    data = load_data()
    found = find_file_by_fid(data, fid)
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_content:{course_id}:{cat_idx}:{group_id}")]
    if not found:
        await query.edit_message_text("این مورد دیگه پیدا نشد؛ شاید حذف شده.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return
    _, _, _, _, item = found
    is_note = item.get("type") == "note"
    buttons = [[InlineKeyboardButton("➡️ انتقال به بدون‌گروه", callback_data=f"fgm_ungroupitem:{fid}:{course_id}:{cat_idx}:{group_id}")]]
    if is_note:
        buttons.append([InlineKeyboardButton("✏️ ویرایش متن نوت", callback_data=f"fgm_editnote:{fid}:{course_id}:{cat_idx}:{group_id}")])
        buttons.append([InlineKeyboardButton("🗑 حذف نوت", callback_data=f"fgm_delnote:{fid}:{course_id}:{cat_idx}:{group_id}")])
    buttons.append(back_btn)
    label = item.get("content") if is_note else display_caption(item)
    await query.edit_message_text(f"«{label}»\nچیکار کنم؟", reply_markup=InlineKeyboardMarkup(buttons))


async def fgm_ungroup_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, fid, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 4)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await query.answer("این مورد دیگه پیدا نشد.", show_alert=True)
        return
    program, _, _, _, _ = found
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    dbmod.move_file_to_group(fid, None)
    dbmod.log_action(update.effective_user.id, "UNGROUP_ITEM", term=program, detail=f"fid={fid}")
    await query.answer("به «بدون‌گروه» منتقل شد ✅")
    await _render_fgm_group(query, course_id, cat_idx, int(group_id_str))


async def fgm_prompt_edit_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, fid, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 4)
    clear_admin_flow_state(context)
    context.user_data["awaiting_note_edit"] = {
        "fid": fid, "course_id": int(course_id_str), "cat_idx": int(cat_idx_str), "group_id": int(group_id_str),
    }
    await query.edit_message_text("متنِ جدیدِ نوت رو بفرست.\nبرای انصراف /cancel رو بفرست.")


async def fgm_delete_note_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, fid, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 4)
    buttons = [
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"fgm_delnote_yes:{fid}:{course_id_str}:{cat_idx_str}:{group_id_str}")],
        [InlineKeyboardButton("🔙 انصراف", callback_data=f"fgm_item:{fid}:{course_id_str}:{cat_idx_str}:{group_id_str}")],
    ]
    await query.edit_message_text("مطمئنی این نوت حذف بشه؟", reply_markup=InlineKeyboardMarkup(buttons))


async def fgm_delete_note_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, fid, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 4)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    data = load_data()
    found = find_file_by_fid(data, fid)
    if found:
        program = found[0]
        if not can_manage_term(update.effective_user.id, program):
            await deny_term_access(query, program)
            return
        dbmod.delete_note(fid)
        dbmod.log_action(update.effective_user.id, "DELETE_NOTE", term=program, detail=f"fid={fid}")
    await query.answer("نوت حذف شد ✅")
    await _render_fgm_group(query, course_id, cat_idx, group_id)


async def fgm_rename_group_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.edit_message_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return
    clear_admin_flow_state(context)
    context.user_data["awaiting_group_rename"] = {"course_id": course_id, "cat_idx": cat_idx, "group_id": group_id}
    await query.edit_message_text("اسمِ جدیدِ گروه رو بفرست.\nبرای انصراف /cancel رو بفرست.")


async def fgm_move_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    direction = "up" if query.data.startswith("fgm_up:") else "down"
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.answer("دسترسی نداری.", show_alert=True)
        return
    dbmod.move_file_group_order(group_id, direction)
    await _render_fgm_group(query, course_id, cat_idx, group_id)


async def fgm_delete_group_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    group = dbmod.get_file_group(int(group_id_str))
    name = group["name"] if group else "این گروه"
    buttons = [
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"fgm_del_yes:{course_id_str}:{cat_idx_str}:{group_id_str}")],
        [InlineKeyboardButton("🔙 انصراف", callback_data=f"fgm_group:{course_id_str}:{cat_idx_str}:{group_id_str}")],
    ]
    await query.edit_message_text(
        f"مطمئنی «{name}» حذف بشه؟\n(فایل/نوت‌های داخلش حذف نمی‌شن، فقط بدون‌گروه می‌شن.)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def fgm_delete_group_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.edit_message_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return
    dbmod.delete_file_group(group_id)
    dbmod.log_action(update.effective_user.id, "DELETE_GROUP", term=course["term_name"], detail=f"group_id={group_id}")
    await query.answer("گروه حذف شد ✅")
    await _render_fgm_category(query, course_id, cat_idx)


def _render_ungrouped_selection(course_id: int, cat_idx: int, content: list, selected: set) -> InlineKeyboardMarkup:
    """کیبورد چندانتخابیِ انتقال به گروه -- دقیقاً شبیهِ _render_delete_selection، فقط
    به‌جای ایندکس از fid استفاده می‌کنه (چون این لیست فقط بدون‌گروه‌هاست و ممکنه بینِ
    toggleها آیتمی جابه‌جا/کم بشه؛ fid برخلاف ایندکس ثابت می‌مونه)."""
    buttons = []
    for item in content:
        fid = item["fid"]
        label = ("📝 " + (item.get("caption") or "نوت")) if item["type"] == "note" else ("📄 " + (item.get("caption") or ""))
        mark = "☑️" if fid in selected else "⬜️"
        buttons.append([InlineKeyboardButton(f"{mark} {label}"[:64], callback_data=f"fgm_movesel:{course_id}:{cat_idx}:{fid}")])
    if selected:
        n = len(selected)
        buttons.append([InlineKeyboardButton(f"➡️ انتقال {n} موردِ انتخاب‌شده به گروه", callback_data=f"fgm_movego:{course_id}:{cat_idx}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_cat:{course_id}:{cat_idx}")])
    return InlineKeyboardMarkup(buttons)


async def fgm_show_ungrouped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)
    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    content = dbmod.get_group_content(course_id, category_id, None)
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_cat:{course_id}:{cat_idx}")]
    if not content:
        await query.edit_message_text("چیزِ بدون‌گروهی توی این بخش نیست.", reply_markup=InlineKeyboardMarkup([back_btn]))
        return
    # هر بار تازه وارد این صفحه می‌شیم، انتخابِ قبلی (اگه بود) پاک می‌شه.
    context.user_data["fgm_move_selection"] = {"course_id": course_id, "cat_idx": cat_idx, "fids": set()}
    await query.edit_message_text(
        "فایل/نوتِ بدون‌گروه -- می‌تونی چندتا رو با هم انتخاب کنی، بعد مقصد رو بزن:",
        reply_markup=_render_ungrouped_selection(course_id, cat_idx, content, set()),
    )


async def fgm_toggle_move_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """با هر بار زدنِ یه آیتم، انتخاب/عدم‌انتخابش رو toggle می‌کنه (بدون انتقالِ واقعی)."""
    query = update.callback_query
    _, course_id_str, cat_idx_str, fid = query.data.split(":", 3)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)

    sel = context.user_data.get("fgm_move_selection")
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx:
        sel = {"course_id": course_id, "cat_idx": cat_idx, "fids": set()}
        context.user_data["fgm_move_selection"] = sel

    if fid in sel["fids"]:
        sel["fids"].discard(fid)
    else:
        sel["fids"].add(fid)
    await query.answer()

    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    content = dbmod.get_group_content(course_id, category_id, None)
    if not content:
        await query.edit_message_text("چیزِ بدون‌گروهی توی این بخش نیست.")
        return
    await query.edit_message_text(
        "فایل/نوتِ بدون‌گروه -- می‌تونی چندتا رو با هم انتخاب کنی، بعد مقصد رو بزن:",
        reply_markup=_render_ungrouped_selection(course_id, cat_idx, content, sel["fids"]),
    )


async def fgm_pick_target_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """➡️ انتقال موردهای انتخاب‌شده -- لیستِ گروه‌های مقصد رو نشون می‌ده."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    course_id, cat_idx = int(course_id_str), int(cat_idx_str)

    sel = context.user_data.get("fgm_move_selection")
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx or not sel.get("fids"):
        await query.edit_message_text("چیزی انتخاب نشده؛ از /filegroups دوباره امتحان کن.")
        return

    category = category_name_by_id(cat_idx)
    if category is None:
        await query.edit_message_text("این دسته دیگه وجود نداره (شاید قبلاً حذف شده).")
        return
    category_id = cat_idx  # همون id که از callback اومده -- category فقط برای نمایش لازمه
    groups = dbmod.get_file_groups(course_id, category_id)
    back_btn = [InlineKeyboardButton("🔙 بازگشت", callback_data=f"fgm_ungrouped:{course_id}:{cat_idx}")]
    if not groups:
        await query.edit_message_text(
            "هنوز هیچ گروهی توی این بخش نیست؛ اول یه گروه بساز.", reply_markup=InlineKeyboardMarkup([back_btn])
        )
        return

    n = len(sel["fids"])
    buttons = [
        [InlineKeyboardButton(f"📁 {g['name']}", callback_data=f"fgm_moveallto:{course_id}:{cat_idx}:{g['id']}")]
        for g in groups
    ]
    buttons.append(back_btn)
    await query.edit_message_text(f"این {n} مورد به کدوم گروه منتقل بشه؟", reply_markup=InlineKeyboardMarkup(buttons))


async def fgm_execute_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """انتقالِ نهاییِ همه‌ی موردهای انتخاب‌شده به گروهِ مقصد -- یکی‌یکی move_file_to_group صدا زده
    می‌شه (نه dict-diffing)، پس هیچ فایلی دوباره آپلود یا بازساخته نمی‌شه."""
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    course_id, cat_idx, group_id = int(course_id_str), int(cat_idx_str), int(group_id_str)

    sel = context.user_data.pop("fgm_move_selection", None)
    if not sel or sel.get("course_id") != course_id or sel.get("cat_idx") != cat_idx or not sel.get("fids"):
        await query.edit_message_text("چیزی انتخاب نشده؛ از /filegroups دوباره امتحان کن.")
        return

    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await query.edit_message_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return

    moved = 0
    for fid in sel["fids"]:
        if dbmod.move_file_to_group(fid, group_id):
            moved += 1
    dbmod.log_action(
        update.effective_user.id, "MOVE_TO_GROUP_BULK", term=course["term_name"],
        detail=f"group_id={group_id} count={moved}",
    )
    await query.answer(f"{moved} مورد منتقل شد ✅")
    await _render_fgm_group(query, course_id, cat_idx, group_id)


async def filegroups_execute_new_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگه منتظر اسمِ گروهِ جدید بودیم، اینجا می‌سازتش. خروجی True یعنی پیام مصرف شد."""
    pending = context.user_data.get("awaiting_new_group_name")
    if not pending:
        return False
    context.user_data.pop("awaiting_new_group_name", None)
    name = update.message.text.strip()
    if name == "/cancel":
        await update.message.reply_text("ساختِ گروه لغو شد.")
        return True
    if not name:
        await update.message.reply_text("اسم نامعتبره؛ دوباره با /filegroups امتحان کن.")
        return True

    course_id, cat_idx = pending["course_id"], pending["cat_idx"]
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await update.message.reply_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return True
    # نکته: cat_idx اینجا از همون callback_dataیِ fgm_cat:{course_id}:{category_id}
    # اومده -- یعنی خودش مستقیماً category_id واقعیه (نگاه کن به categories_for)،
    # پس نیازی به یه دور دیگه lookup از رویِ CATEGORIES سراسری نیست (که برای دسته‌های
    # سفارشی اصلاً کار نمی‌کرد).
    category_id = cat_idx
    category_row = dbmod.get_category_by_id(category_id)
    if category_row is None or category_row["course_id"] != course_id:
        await update.message.reply_text("این بخش دیگه وجود نداره؛ شاید حذف شده.")
        return True
    group_id = dbmod.add_file_group(course_id, category_id, name)
    dbmod.log_action(update.effective_user.id, "ADD_GROUP", term=course["term_name"], detail=f"name={name}")

    if pending.get("then_upload"):
        # از چوزرِ مقصدِ /upload اومده -- به‌جای برگشتن به منوی /filegroups، همین‌جا
        # آماده‌ی گرفتنِ فایل می‌شیم تا کاربر مجبور نباشه دوباره از اول بره سراغ آپلود.
        context.user_data["pending_upload"] = {
            "term": course["term_name"], "course": course["name"], "category": category_row["name"],
            "group_id": group_id, "group_name": name,
        }
        prompt = _set_pending_upload_and_prompt_text(course["name"], category_row["name"], name)
        await update.message.reply_text(f"گروهِ «{name}» ساخته شد ✅\n\n{prompt}")
        return True

    await update.message.reply_text(f"گروهِ «{name}» ساخته شد ✅")
    return True


async def filegroups_execute_rename_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("awaiting_group_rename")
    if not pending:
        return False
    context.user_data.pop("awaiting_group_rename", None)
    new_name = update.message.text.strip()
    if new_name == "/cancel":
        await update.message.reply_text("تغییرنامِ گروه لغو شد.")
        return True
    if not new_name:
        await update.message.reply_text("اسم نامعتبره.")
        return True
    course = dbmod.get_course_by_id(pending["course_id"])
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await update.message.reply_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return True
    ok = dbmod.rename_file_group(pending["group_id"], new_name)
    if ok:
        dbmod.log_action(update.effective_user.id, "RENAME_GROUP", term=course["term_name"], detail=f"group_id={pending['group_id']} -> {new_name}")
        await update.message.reply_text(f"اسمِ گروه به «{new_name}» تغییر کرد ✅")
    else:
        await update.message.reply_text("این تغییرنام انجام نشد (شاید هم‌نامِ یه گروهِ دیگه‌ست یا گروه پاک شده).")
    return True


async def filegroups_execute_new_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("awaiting_note_text")
    if not pending:
        return False
    context.user_data.pop("awaiting_note_text", None)
    text = update.message.text.strip()
    if text == "/cancel":
        await update.message.reply_text("افزودنِ نوت لغو شد.")
        return True
    if not text:
        await update.message.reply_text("متنِ نوت نمی‌تونه خالی باشه.")
        return True
    course_id, cat_idx, group_id = pending["course_id"], pending["cat_idx"], pending["group_id"]
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await update.message.reply_text("این درس دیگه پیدا نشد یا دسترسی نداری.")
        return True
    category_id = cat_idx  # نگاه کن به توضیحِ مشابه توی filegroups_execute_new_group
    dbmod.add_note(course_id, category_id, group_id, text)
    dbmod.log_action(update.effective_user.id, "ADD_NOTE", term=course["term_name"], detail=f"group_id={group_id}")
    await update.message.reply_text("نوت اضافه شد ✅")
    return True


async def filegroups_execute_edit_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("awaiting_note_edit")
    if not pending:
        return False
    context.user_data.pop("awaiting_note_edit", None)
    text = update.message.text.strip()
    if text == "/cancel":
        await update.message.reply_text("ویرایشِ نوت لغو شد.")
        return True
    if not text:
        await update.message.reply_text("متنِ نوت نمی‌تونه خالی باشه.")
        return True
    data = load_data()
    found = find_file_by_fid(data, pending["fid"])
    if not found:
        await update.message.reply_text("این نوت دیگه پیدا نشد؛ شاید حذف شده.")
        return True
    program = found[0]
    if not can_manage_term(update.effective_user.id, program):
        await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{program}» رو ندارید.")
        return True
    dbmod.edit_note(pending["fid"], text)
    dbmod.log_action(update.effective_user.id, "EDIT_NOTE", term=program, detail=f"fid={pending['fid']}")
    await update.message.reply_text("نوت ویرایش شد ✅")
    return True


# ============================================================
# ادمین: آمار
# ============================================================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # آمار کلی، همه‌ی ترم‌ها رو با هم نشون می‌ده -> فقط سوپرادمین
    if not is_super_admin(update.effective_user.id):
        return

    data = load_data()
    all_files = []
    total_downloads = 0
    for program in get_program_names():
        for course_name, course_data in data.get(program, {}).items():
            for category, cat_data in course_data.get("categories", {}).items():
                for f in cat_data.get("files", []):
                    downloads = f.get("downloads", 0)
                    total_downloads += downloads
                    all_files.append((downloads, program, course_name, category, f["caption"]))

    notify_counts = {key: len(dbmod.get_users_for_notify(key)) for key in dbmod.NOTIFY_KEYS}
    analytics = load_analytics()
    requests_list = load_requests()

    # «فعالِ اخیر»: بر اساسِ last_seen که تازه ثبتش رو شروع کردیم. تا وقتی
    # تاریخچه‌ی کافی جمع نشده، این عدد فقط کسایی رو می‌شمره که واقعاً از
    # لحظه‌ی اضافه‌شدنِ این قابلیت به بعد با ربات تعامل داشتن؛ خیلی کمتر از
    # «تعداد کاربران» (که تجمعیِ همیشگیه) نشون می‌ده و طبیعیه.
    active_7d = dbmod.count_active_users_since(now_tehran().isoformat(), days=7)
    active_30d = dbmod.count_active_users_since(now_tehran().isoformat(), days=30)

    text = (
        "📊 آمار کلی\n"
        f"تعداد کاربران (کل، از اول تا حالا): {len(analytics['users'])}\n"
        f"فعال در ۷ روزِ اخیر: {active_7d}\n"
        f"فعال در ۳۰ روزِ اخیر: {active_30d}\n"
        f"تعداد کل فایل‌ها: {len(all_files)}\n"
        f"مجموع دانلودها: {total_downloads}\n"
        f"درخواست‌های باز: {len(requests_list)}\n\n"
        "🔔 اطلاع‌رسانی:\n"
        f"  📥 فایل جدید (ترمِ خودشون): {notify_counts['new_files']}\n"
        f"  📚 گپ امتحانی (ترمِ خودشون): {notify_counts['exam_chat']}\n"
        f"  ☕ گپ دوستانه: {notify_counts['casual_chat']}\n"
        f"  🟢 روشن‌شدنِ ربات: {notify_counts['startup']}\n\n"
    )

    section_counts = analytics.get("section_counts", {})
    if section_counts:
        text += "📌 استفاده از هر بخش:\n"
        for key, cnt in sorted(section_counts.items(), key=lambda x: -x[1]):
            text += f"  {key}: {cnt}\n"
        text += "\n"

    course_counts = analytics.get("course_counts", {})
    if course_counts:
        text += "🎓 محبوب‌ترین درس‌ها:\n"
        for name, cnt in sorted(course_counts.items(), key=lambda x: -x[1])[:5]:
            text += f"  {cnt} بار — {name}\n"
        text += "\n"

    if all_files:
        all_files.sort(key=lambda x: x[0], reverse=True)
        text += "🏆 پرطرفدارترین فایل‌ها:\n"
        for downloads, program, course_name, category, caption in all_files[:5]:
            text += f"  {downloads} بار — {caption} ({program} - {course_name} - {category})\n"

    await update.message.reply_text(text)


# ============================================================
# ادمین: دریافت متن (اسم درس جدید / راهنما / لینک فایل حجیم)
# دانشجو: لقب دلخواه / فرم تجربه امتحان / متن درخواست
# ============================================================

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # --- حالت ۰-الف: منتظر لقب دلخواه کاربر (هر کسی) ---
    if context.user_data.get("awaiting_nickname"):
        nickname = update.message.text.strip()[:40]
        user_id = update.effective_user.id
        users = load_users()
        entry = users.setdefault(str(user_id), {})
        entry["nickname"] = nickname
        entry["nickname_asked"] = True
        save_users(users)
        context.user_data.pop("awaiting_nickname", None)
        await update.message.reply_text(f"باشه، از این به بعد صدات می‌کنم: {nickname} 😊")
        await send_welcome_or_ask_program(context, update.effective_chat.id, user_id, update.effective_user.first_name)
        return

    # --- حالت ۰-ب: در وسط فرم «ارسال تجربه امتحان» (هر کسی) ---
    exam_form = context.user_data.get("exam_form")
    if exam_form:
        await handle_exam_form_text(update, context, exam_form)
        return

    # --- حالت ۰-ج: منتظر متن یه درخواست جدیدیم (هر کسی) ---
    if context.user_data.get("awaiting_request"):
        text = update.message.text.strip()
        context.user_data.pop("awaiting_request", None)
        sender = update.effective_user
        display = get_display_name(sender.id, sender.first_name)
        requests_list = load_requests()
        req_id = next_request_id(requests_list)
        entry = {"id": req_id, "user_id": sender.id, "sender_display": display, "text": text}
        requests_list.append(entry)
        save_requests(requests_list)
        await update.message.reply_text("ثبت شد، ممنون بابت پیشنهادت 🙏 حتماً بررسیش می‌کنم.")
        buttons = [[InlineKeyboardButton("✅ بررسی شد", callback_data=f"close_request:{req_id}")]]
        for admin_id in get_super_admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"💡 درخواست جدید از {display}:\n{text}",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            except Exception:
                pass
        return

    # --- حالت ۰-د: منتظر بازه‌ی زمانیِ بلوک مطالعه‌ی شخصی‌ایم (هر کسی) ---
    pstudy_new = context.user_data.get("pstudy_new")
    if pstudy_new and "start" not in pstudy_new:
        raw = update.message.text.strip()
        rng = _parse_time_range(raw)
        if rng is None:
            await update.message.reply_text("بازه‌ی ساعت نامعتبره. فرمت درست: 18:00-19:30 و ساعت پایان باید بعد از شروع باشه.")
            return
        start, end = rng
        pstudy_new["start"] = start
        pstudy_new["end"] = end
        context.user_data["pstudy_new"] = pstudy_new
        buttons = _program_buttons("pstudy_prog")
        await update.message.reply_text(
            "این درس مربوط به کدوم برنامه‌ست؟", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # --- حالت ۰-ه: منتظر متنِ جدیدِ ویرایشِ یه پیامِ گپیم (باید قبل از حالتِ کلیِ
    # «داخل اتاق گپیم» چک بشه، چون توی ویرایش هم هنوز داخل همون اتاقیم) ---
    chat_edit_pending = context.user_data.get("chat_edit_pending")
    if chat_edit_pending:
        await handle_chat_msg_edit_text(update, context, chat_edit_pending)
        return

    # --- حالت ۰-ه: وسطِ ویزاردِ ساختِ نظرسنجی‌ایم (اونم باید قبل از حالتِ کلیِ اتاق
    # چک بشه، دقیقاً به همون دلیلِ بالا) ---
    poll_new_state = context.user_data.get("poll_new")
    if poll_new_state:
        await handle_chat_poll_wizard_text(update, context, poll_new_state)
        return

    # --- حالت ۰-ه: داخل یه اتاق گپ دانشجویی‌ایم (هر کسی) ---
    active_room = context.user_data.get("active_chat_room")
    if active_room:
        await handle_chat_room_text(update, context, active_room)
        return

    # --- حالت ۰-و: منتظر متن نهایی ویرایش‌شده‌ی یه پیش‌نویس تجربه‌ایم (فقط ادمین) ---
    awaiting_draft_edit = context.user_data.get("awaiting_draft_edit")
    if awaiting_draft_edit:
        if not is_admin(update.effective_user.id):
            context.user_data.pop("awaiting_draft_edit", None)
            return
        await handle_chatadmin_draft_edit_text(update, context, awaiting_draft_edit)
        return

    # از اینجا به بعد فقط دستورهای مخصوص ادمینه
    if not is_admin(update.effective_user.id):
        return

    # حالت ۰-د-۱: منتظر اسم جدید برای تغییر نام فایل (فقط ادمین)
    if await rename_execute(update, context):
        return

    # حالت ۰-د-۱-ب: منتظر متنِ جدیدِ توضیح (کپشنِ تلگرامیِ) یه فایل (فقط ادمین)
    if await editdesc_execute(update, context):
        return

    # حالت ۰-د-۱-الف: مراحلِ متنیِ مدیریتِ گروه/نوت (فقط ادمین)
    if await filegroups_execute_new_group(update, context):
        return
    if await filegroups_execute_rename_group(update, context):
        return
    if await filegroups_execute_new_note(update, context):
        return
    if await filegroups_execute_edit_note(update, context):
        return

    # حالت ۰-د-۱-ب: منتظر اسم جدید برای فایلِ تازه‌آپلودشده‌ایم (فقط ادمین)
    if await upload_name_execute(update, context):
        return

    # حالت ۰-د-۲: منتظر متن یه کلاس جدید برای برنامه‌ی رسمی‌ایم (فقط ادمین)
    pending_class = context.user_data.get("pending_class_entry")
    if pending_class:
        program, day = pending_class["program"], pending_class["day"]
        if not can_manage_term(update.effective_user.id, program):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{program}» رو ندارید.")
            context.user_data.pop("pending_class_entry", None)
            return

        schedule = load_class_schedule()
        existing = schedule.get(program, {}).get(day, [])
        batch_for_conflict = list(existing)  # بلوک‌های همین پیام هم باید تو چکِ تداخلِ خطِ بعدی لحاظ بشن
        raw_lines = [ln.strip() for ln in update.message.text.split("\n") if ln.strip()]

        added, errors = [], []
        for line_no, raw in enumerate(raw_lines, start=1):
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 2 or len(parts) > 4:
                errors.append(f"خط {line_no}: فرمت نامعتبر («{raw}»)")
                continue
            time_part, course_part = parts[0], parts[1]
            group_part = parts[2] if len(parts) >= 3 else ""
            location_part = parts[3] if len(parts) == 4 else ""
            if not course_part:
                errors.append(f"خط {line_no}: نام درس خالیه.")
                continue
            rng = _parse_time_range(time_part)
            if rng is None:
                errors.append(f"خط {line_no}: ساعت نامعتبره («{time_part}»). فرمت درست: HH:MM-HH:MM")
                continue
            start, end = rng

            course_id = dbmod.get_course_id_by_name(program, course_part)
            group_id = None
            if group_part:
                if course_id is None:
                    errors.append(
                        f"خط {line_no}: چون اسمِ درس («{course_part}») پیدا نشد، نمی‌شه گروهِ "
                        f"«{group_part}» رو بهش وصل کرد."
                    )
                    continue
                candidates = {g["name"]: g["id"] for g in dbmod.get_course_groups(course_id)}
                if group_part not in candidates:
                    known = "، ".join(candidates) if candidates else "(هیچ گروهی برای این درس تعریف نشده)"
                    errors.append(
                        f"خط {line_no}: گروهِ «{group_part}» برای «{course_part}» پیدا نشد. گروه‌های موجود: {known}"
                    )
                    continue
                group_id = candidates[group_part]

            # تشخیصِ تداخل: اگر گروهِ کلاس هنوز معلوم نیست، اجازه می‌دیم بلوک‌های
            # هم‌زمان ثبت بشن تا بعداً ادمین گروهِ هر بلوک رو مشخص کنه. در این حالت
            # رکورد با group_pending=True علامت می‌خوره. «بدون گروه»ی که هیچ تداخلی
            # نداره همچنان کلاسِ عمومی محسوب می‌شه. اگر گروه از قبل مشخص شده باشه،
            # با همان گروه یا با یک کلاسِ عمومیِ قطعی نباید هم‌زمان بشه.
            overlapping = [
                e for e in batch_for_conflict
                if _ranges_overlap(start, end, e["start"], e["end"])
            ]
            conflict = None
            if group_id is not None:
                for existing_entry in overlapping:
                    other_group = existing_entry.get("group_id")
                    if other_group == group_id:
                        conflict = existing_entry
                        break
                    if other_group is None and not existing_entry.get("group_pending"):
                        # کلاسِ بدون‌گروهِ قطعی یعنی برای همه است؛ پس با هر گروهی
                        # مخاطب مشترک دارد و تداخل واقعی محسوب می‌شود.
                        conflict = existing_entry
                        break
            if conflict is not None:
                errors.append(
                    f"خط {line_no}: با «{conflict['course']}» ({conflict['start']}-{conflict['end']}) "
                    "تداخلِ زمانی داره (روی مخاطبِ مشترک) — اضافه نشد."
                )
                continue

            new_entry = {"id": uuid.uuid4().hex[:12], "start": start, "end": end, "course": course_part}
            if group_id is None and overlapping:
                # وقتی بلوکِ تازه بدون گروه با چیزی هم‌زمان است، فعلاً «گروه نامشخص»
                # حسابش می‌کنیم، نه کلاس عمومی. بلوک‌های بدون‌گروهِ قبلیِ همان بازه
                # را هم pending می‌کنیم تا بعداً اشتباهاً عمومی تلقی نشوند.
                new_entry["group_pending"] = True
                for existing_entry in overlapping:
                    if existing_entry.get("group_id") is None:
                        existing_entry["group_pending"] = True
            if course_id is not None:
                # ذخیره‌ی course_id از همون لحظه‌ی ثبت -- تا بعداً نیازی به جستجوی
                # نام‌محور برای ساختِ دکمه‌ی «📚 منابع» نباشه (نگاه کن به
                # resolve_course_id_for_class_entry).
                new_entry["course_id"] = course_id
            if group_id is not None:
                new_entry["group_id"] = group_id
            if location_part:
                new_entry["location"] = location_part

            batch_for_conflict.append(new_entry)
            added.append(new_entry)

        if added:
            schedule.setdefault(program, {}).setdefault(day, []).extend(added)
            save_class_schedule(schedule)
            dbmod.log_action(
                update.effective_user.id, "ADD_CLASS_SCHEDULE", term=program, detail=f"{day} +{len(added)} خط"
            )
            added_summary = "، ".join(f"{e['course']} ({e['start']}-{e['end']})" for e in added)
            await notify_program_schedule_change(
                context, program, f"➕ به «{day}» اضافه شد: {added_summary}"
            )
        context.user_data.pop("pending_class_entry", None)

        report_lines = []
        for e in added:
            extra_bits = []
            if e.get("group_id") is not None:
                grp = dbmod.get_course_group_by_id(e["group_id"])
                if grp:
                    extra_bits.append(f"گروه {grp['name']}")
            elif e.get("group_pending"):
                extra_bits.append("⚠️ گروه هنوز مشخص نشده")
            if e.get("location"):
                extra_bits.append(e["location"])
            suffix = f" — {'، '.join(extra_bits)}" if extra_bits else ""
            warn = ""
            if e.get("course_id") is None and dbmod.get_active_course_id(program) is None:
                warn = " ⚠️ (اسمِ درس پیدا نشد، دکمه‌ی منابع کار نمی‌کنه)"
            report_lines.append(f"✅ {e['start']}-{e['end']} {e['course']}{suffix}{warn}")

        report = ""
        if report_lines:
            report += "به «{}» اضافه شد:\n".format(day) + "\n".join(report_lines)
        if errors:
            if report:
                report += "\n\n"
            report += "⚠️ اضافه نشد:\n" + "\n".join(errors)
        if not report:
            report = "هیچ خطِ معتبری پیدا نشد. فرمت درست: HH:MM-HH:MM | نام‌درس | گروه(اختیاری) | مکان(اختیاری)"
        term_id = term_id_for_program(program)
        reply_markup = None
        if term_id is not None:
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⚙️ تنظیم گروه / مکان کلاس‌ها", callback_data=f"cls_day:{term_id}:{day}")]]
            )
        await update.message.reply_text(report, reply_markup=reply_markup)
        return

    # حالت ۰-د-۲-الف: تعیین سریع مکان کلاس
    pending_location = context.user_data.get("pending_class_location")
    if pending_location:
        location = update.message.text.strip()
        if not location:
            await update.message.reply_text("اسم مکان خالیه؛ مثلاً «کلاس ۲۰۳» بفرست.")
            return
        program = pending_location["program"]
        day = pending_location["day"]
        token = pending_location["token"]
        schedule = load_class_schedule()
        classes = schedule.get(program, {}).get(day, [])
        idx = _find_class_entry_index(classes, token)
        if idx is None:
            context.user_data.pop("pending_class_location", None)
            await update.message.reply_text("این کلاس دیگه وجود نداره.")
            return
        entry = classes[idx]
        entry["location"] = location
        save_class_schedule(schedule)
        context.user_data.pop("pending_class_location", None)
        dbmod.log_action(
            update.effective_user.id,
            "SET_CLASS_LOCATION",
            term=program,
            detail=f"{day} {entry['course']} {entry['start']}-{entry['end']} -> {location}",
        )
        term_id = pending_location["term_id"]
        await update.message.reply_text(
            f"📍 مکان «{entry['course']}» شد: {location} ✅",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ برگشت به برنامه روز", callback_data=f"cls_day:{term_id}:{day}")]]
            ),
        )
        return

    # حالت ۰-د-۲-ب: ساخت سریع گروه با روز/ساعت همین کلاس
    pending_qgroup = context.user_data.get("pending_quick_course_group")
    if pending_qgroup:
        group_name = update.message.text.strip()
        if not group_name:
            await update.message.reply_text("فقط اسم گروه رو بفرست؛ مثلاً «گروه ۱».")
            return
        course_id = pending_qgroup["course_id"]
        course = dbmod.get_course_by_id(course_id)
        if course is None:
            context.user_data.pop("pending_quick_course_group", None)
            await update.message.reply_text("این درس دیگه وجود نداره.")
            return

        # اگر گروه هم‌نام وجود دارد، همان را استفاده می‌کنیم؛ در غیر این صورت می‌سازیم.
        existing_groups = dbmod.get_course_groups(course_id)
        group = next((g for g in existing_groups if g["name"] == group_name), None)
        if group is None:
            dbmod.add_course_group(
                course_id,
                group_name,
                pending_qgroup["day"],
                pending_qgroup["start"],
                pending_qgroup["end"],
            )
            group = next(
                (g for g in dbmod.get_course_groups(course_id) if g["name"] == group_name),
                None,
            )
        if group is None:
            await update.message.reply_text("ساخت گروه انجام نشد؛ دوباره امتحان کن.")
            return

        dbmod.set_has_group_split(course_id, True)

        program = pending_qgroup["program"]
        day = pending_qgroup["day"]
        token = pending_qgroup["token"]
        schedule = load_class_schedule()
        classes = schedule.get(program, {}).get(day, [])
        idx = _find_class_entry_index(classes, token)
        if idx is None:
            context.user_data.pop("pending_quick_course_group", None)
            await update.message.reply_text("گروه ساخته شد، ولی کلاس دیگه پیدا نشد.")
            return
        entry = classes[idx]
        entry["group_id"] = group["id"]
        entry.pop("group_pending", None)
        save_class_schedule(schedule)
        context.user_data.pop("pending_quick_course_group", None)

        dbmod.log_action(
            update.effective_user.id,
            "QUICK_CREATE_AND_ASSIGN_GROUP",
            term=program,
            detail=f"{course['name']}: {group_name} ({day} {entry['start']}-{entry['end']})",
        )
        await update.message.reply_text(
            f"👥 گروه «{group_name}» ساخته شد و به همین کلاس وصل شد ✅",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ برگشت به برنامه روز", callback_data=f"cls_day:{pending_qgroup['term_id']}:{day}")]]
            ),
        )
        return

    # حالت ۰-د-۲-ب: منتظر متن یه گروهِ جدید برای گروه‌بندیِ یه درسیم (فقط ادمین)
    pending_group = context.user_data.get("pending_course_group")
    if pending_group:
        raw = update.message.text.strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3 or not all(parts):
            await update.message.reply_text(
                "فرمت درست: اسمِ گروه | روز | ساعتِ‌شروع-ساعتِ‌پایان\nمثال: گروه ۱ | چهارشنبه | 10:00-12:00"
            )
            return
        name_part, day_part, time_part = parts
        if day_part not in DAYS_OF_WEEK:
            await update.message.reply_text(
                "روز باید یکی از این‌ها باشه: " + "، ".join(DAYS_OF_WEEK)
            )
            return
        rng = _parse_time_range(time_part)
        if rng is None:
            await update.message.reply_text("بازه‌ی ساعت نامعتبره. مثال: 10:00-12:00 و ساعت پایان باید بعد از شروع باشه.")
            return
        start, end = rng

        course_id = pending_group["course_id"]
        course = dbmod.get_course_by_id(course_id)
        if course is None:
            await update.message.reply_text("این درس دیگه وجود نداره.")
            context.user_data.pop("pending_course_group", None)
            return
        if not can_manage_term(update.effective_user.id, course["term_name"]):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{course['term_name']}» رو ندارید.")
            context.user_data.pop("pending_course_group", None)
            return
        dbmod.add_course_group(course_id, name_part, day_part, start, end)
        dbmod.set_has_group_split(course_id, True)
        dbmod.log_action(
            update.effective_user.id, "ADD_COURSE_GROUP", term=course["term_name"],
            detail=f"{course['name']}: {name_part} ({day_part} {start}-{end})",
        )
        context.user_data.pop("pending_course_group", None)
        back_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("👥 مدیریتِ گروه‌بندی", callback_data=f"crs_groups:{course_id}")]]
        )
        await update.message.reply_text(
            f"گروهِ «{name_part}» ({day_part} {start}-{end}) به «{course['name']}» اضافه شد ✅",
            reply_markup=back_btn,
        )
        return

    # حالت ۰-د-۲-ج: منتظر تاریخِ لغوِ یه‌روزه‌ی یه بلوکِ کلاسی‌ام (فقط ادمین)
    pending_exc = context.user_data.get("pending_class_exception")
    if pending_exc:
        raw = update.message.text.strip()
        date_str = parse_flexible_date_to_gregorian(raw)
        if date_str is None:
            await update.message.reply_text(
                "فرمت تاریخ درست نیست. شمسی یا میلادی هردو قبوله، مثلاً: 1405/07/15 یا 2026-10-07"
            )
            return
        display_date = format_gregorian_as_jalali(date_str)
        program, day = pending_exc["program"], pending_exc["day"]
        if not can_manage_term(update.effective_user.id, program):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{program}» رو ندارید.")
            context.user_data.pop("pending_class_exception", None)
            return
        schedule = load_class_schedule()
        classes = schedule.get(program, {}).get(day, [])
        class_id = pending_exc.get("class_id")
        idx = next((i for i, c in enumerate(classes) if class_id and c.get("id") == class_id), None)
        if idx is None:
            legacy_idx = pending_exc.get("idx")
            if isinstance(legacy_idx, int) and legacy_idx < len(classes):
                idx = legacy_idx
        if idx is None:
            await update.message.reply_text("این کلاس دیگه وجود نداره.")
            context.user_data.pop("pending_class_exception", None)
            return
        entry = classes[idx]
        already = is_class_cancelled_on(program, day, entry, date_str)
        if already:
            remove_class_exception(program, day, entry, date_str)
            msg = f"↩️ لغوِ «{entry['course']}» برای {display_date} برداشته شد؛ اون روز دوباره برگزار می‌شه."
            await notify_program_schedule_change(
                context, program, f"↩️ «{entry['course']}» ({day} {entry['start']}-{entry['end']}) در تاریخِ {display_date} دوباره برگزار می‌شه (لغوش برداشته شد)."
            )
        else:
            add_class_exception(program, day, entry, date_str)
            msg = f"🚫 «{entry['course']}» ({day} {entry['start']}-{entry['end']}) برای {display_date} لغو شد."
            await notify_program_schedule_change(
                context, program, f"🚫 «{entry['course']}» ({day} {entry['start']}-{entry['end']}) در تاریخِ {display_date} لغو شد."
            )
        dbmod.log_action(
            update.effective_user.id,
            "TOGGLE_CLASS_EXCEPTION",
            term=program,
            detail=f"{day} {entry['course']} {date_str} -> {'لغو' if not already else 'برگشت'}",
        )
        context.user_data.pop("pending_class_exception", None)
        term_id = term_id_for_program(program)
        reply_markup = None
        if term_id is not None:
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ برگشت به برنامه روز", callback_data=f"cls_day:{term_id}:{day}")]]
            )
        await update.message.reply_text(msg, reply_markup=reply_markup)
        return

    # حالت ۰-د-۳: منتظر متن یه امتحان جدید برای برنامه امتحانات‌ایم (فقط ادمین همون ترم)
    pending_exam = context.user_data.get("pending_exam_entry")
    if pending_exam:
        raw = update.message.text.strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await update.message.reply_text(
                "فرمت درست: نام‌درس | تاریخ | ساعت | مکان(اختیاری)\n"
                "مثال: پاتولوژی | ۱۴۰۴/۰۴/۲۰ | ۱۰:۰۰ | سالن امتحانات ۱"
            )
            return
        course_part = parts[0]
        exam_date = parts[1]
        exam_time = parts[2] if len(parts) > 2 and parts[2] else None
        location = parts[3] if len(parts) > 3 and parts[3] else None

        program = pending_exam["term"]
        context.user_data.pop("pending_exam_entry", None)
        if not can_manage_term(update.effective_user.id, program):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{program}» رو ندارید.")
            return

        # ذخیره‌ی نهایی رو به بعد از انتخابِ نوعِ امتحان (میان‌ترم/پایان‌ترم)
        # موکول می‌کنیم -- نگاه کن به exam_type_pick.
        context.user_data["pending_exam_type_choice"] = {
            "term": program, "course": course_part, "exam_date": exam_date,
            "exam_time": exam_time, "location": location,
        }
        type_buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📘 میان‌ترم", callback_data="examtype_pick:midterm")],
            [InlineKeyboardButton("🏁 پایان‌ترم", callback_data="examtype_pick:final")],
            [InlineKeyboardButton("❔ نامشخص", callback_data="examtype_pick:none")],
        ])
        await update.message.reply_text(
            f"این امتحانِ «{course_part}» میان‌ترمه یا پایان‌ترم؟", reply_markup=type_buttons
        )
        return

    # حالت ۰-د-۲-د: منتظر مقدارِ جدیدِ یکی از فیلدهای بازه‌ی زمانیِ ترم‌ام (فقط ادمین)
    pending_period = context.user_data.get("pending_term_period")
    if pending_period:
        raw = update.message.text.strip()
        term_id, field = pending_period["term_id"], pending_period["field"]
        program = program_for_term_id(term_id)
        context.user_data.pop("pending_term_period", None)
        if program is None:
            await update.message.reply_text("این ترم دیگه پیدا نشد.")
            return
        if not can_manage_term(update.effective_user.id, program):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{program}» رو ندارید.")
            return
        value = None if raw == "-" else raw
        dbmod.set_term_period(program, field, value)
        dbmod.log_action(
            update.effective_user.id, "SET_TERM_PERIOD", term=program,
            detail=f"{field} -> {value or '(پاک شد)'}",
        )
        back_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗓 بازه‌های زمانیِ ترم", callback_data=f"termperiods:{term_id}")]]
        )
        label = _TERM_PERIOD_LABELS.get(field, field)
        msg = f"«{label}» پاک شد ✅" if value is None else f"«{label}» شد: {value} ✅"
        await update.message.reply_text(msg, reply_markup=back_btn)
        return

    # حالت ۰-د-۲-ه: منتظر متن یه رویدادِ ثابتِ هفتگیِ جدیدم (فقط ادمین)
    pending_fe = context.user_data.get("pending_fixed_event")
    if pending_fe:
        parts = [p.strip() for p in update.message.text.split("|")]
        if len(parts) < 3 or not parts[0]:
            await update.message.reply_text(
                "فرمت: نوع | نام | روز | ساعتِ‌شروع-ساعتِ‌پایان | دقیقه(اختیاری)\n"
                "مثال: غذا | رزرو غذا | شنبه | 12:00-13:00 | 30"
            )
            return
        typed = len(parts) >= 4 and normalize_fixed_event_type(parts[0]) != "general" or (len(parts) >= 4 and parts[0].strip().lower() in {"general", "عمومی", "رویداد عمومی", "class", "کلاس"})
        if typed:
            event_type = normalize_fixed_event_type(parts[0])
            name, day, time_part = parts[1], parts[2], parts[3]
            lead_index = 4
        else:
            event_type = "general"
            name, day, time_part = parts[0], parts[1], parts[2]
            lead_index = 3
        if day not in DAYS_OF_WEEK:
            await update.message.reply_text("روز باید یکی از این‌ها باشه: " + "، ".join(DAYS_OF_WEEK))
            return
        rng = _parse_time_range(time_part)
        if rng is None:
            await update.message.reply_text("ساعت نامعتبره. فرمت: HH:MM-HH:MM")
            return
        start, end = rng
        lead_minutes = 15
        if len(parts) > lead_index and parts[lead_index]:
            try:
                lead_minutes = int(parts[lead_index])
            except ValueError:
                await update.message.reply_text("دقیقه‌ی یادآوری باید یه عددِ صحیح باشه.")
                return
        scope = pending_fe["scope"]
        allowed = admin_allowed_programs(update.effective_user.id)
        if scope != "all" and scope not in allowed:
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{scope}» رو ندارید.")
            context.user_data.pop("pending_fixed_event", None)
            return
        events = load_fixed_events()
        events.append(
            {"id": uuid.uuid4().hex[:12], "name": name, "type": event_type, "day": day, "start": start, "end": end, "lead_minutes": lead_minutes, "scope": scope}
        )
        save_fixed_events(events)
        dbmod.log_action(
            update.effective_user.id, "ADD_FIXED_EVENT",
            detail=f"{name} type={event_type} {day} {start}-{end} scope={scope} lead={lead_minutes}",
        )
        context.user_data.pop("pending_fixed_event", None)
        await update.message.reply_text(f"«{name}» ({day} {start}-{end}) اضافه شد ✅")
        return

    # حالت ۰-د: منتظر اسم ترم/برنامه‌ی جدیدیم (فقط سوپرادمین -- ساخت ترم عملیات ساختاریه)
    if context.user_data.get("awaiting_semester_name"):
        semester_name = update.message.text.strip()
        semester_group = context.user_data.pop("awaiting_semester_group", None) or dbmod.PHYSIOPATH_GROUP_NAME
        context.user_data.pop("awaiting_semester_name", None)
        if not is_super_admin(update.effective_user.id):
            await update.message.reply_text("⛔️ فقط سوپرادمین می‌تونه ترم جدید بسازه.")
            return
        if not semester_name:
            await update.message.reply_text("اسم خالی قابل قبول نیست.")
            return
        added = add_program(semester_name, group_name=semester_group)
        if not added:
            await update.message.reply_text("این ترم/برنامه از قبل وجود داره (اسمش تکراریه).")
            return
        dbmod.log_action(update.effective_user.id, "ADD_SEMESTER", term=semester_name)
        await update.message.reply_text(
            f"ترم/برنامه‌ی «{semester_name}» به گروه «{semester_group}» اضافه شد ✅\n"
            "حالا با /addcourse می‌تونی بهش درس اضافه کنی."
        )
        return

    # حالت ۱: منتظر اسم درس جدیدیم
    term_name = context.user_data.get("awaiting_course_name_for_term")
    if term_name:
        context.user_data.pop("awaiting_course_name_for_term", None)
        if not can_manage_term(update.effective_user.id, term_name):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{term_name}» رو ندارید.")
            return
        course_name = update.message.text.strip()
        data = load_data()
        data.setdefault(term_name, {})
        if course_name in data[term_name]:
            await update.message.reply_text("این درس از قبل توی این برنامه هست.")
        else:
            data[term_name][course_name] = new_course()
            save_data(data)
            dbmod.log_action(update.effective_user.id, "ADD_COURSE", term=term_name, detail=course_name)
            new_course_row = dbmod.get_courses_with_ids(term_name)
            new_course_id = next((c["id"] for c in new_course_row if c["name"] == course_name), None)
            buttons = [
                [
                    InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"crs_edit:{new_course_id}"),
                    InlineKeyboardButton("🗑 حذف درس", callback_data=f"crs_del:{new_course_id}"),
                ]
            ]
            await update.message.reply_text(
                f"درس «{course_name}» به «{term_name}» اضافه شد ✅\n"
                "اگه لازمه می‌تونی همین الان اسمش رو ویرایش کنی یا حذفش کنی:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    # حالت ۱-ب: منتظر اسم جدید برای ویرایش نام یه درسیم
    pending_course_rename = context.user_data.get("awaiting_course_rename")
    if pending_course_rename:
        context.user_data.pop("awaiting_course_rename", None)
        course_id = pending_course_rename["course_id"]
        course = dbmod.get_course_by_id(course_id)
        if course is None:
            await update.message.reply_text("این درس دیگه وجود نداره (شاید قبلاً حذف شده).")
            return
        if not can_manage_term(update.effective_user.id, course["term_name"]):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{course['term_name']}» رو ندارید.")
            return
        new_name = update.message.text.strip()
        if not new_name:
            await update.message.reply_text("اسم خالی قابل قبول نیست. دوباره بنویس.")
            context.user_data["awaiting_course_rename"] = pending_course_rename
            return
        try:
            ok, result = dbmod.rename_course_by_id(course_id, new_name)
        except Exception:
            logging.exception(
                "editcourse: rename_course_by_id crashed (course_id=%s, new_name=%r)",
                course_id, new_name,
            )
            await update.message.reply_text(
                "❌ یه خطای داخلی موقعِ تغییرِ اسمِ درس پیش اومد؛ اسم عوض نشد. دوباره امتحان کن."
            )
            return
        if not ok:
            await update.message.reply_text(f"❌ {result}")
            return
        old_name = result  # rename_course_by_id موفق -> اسمِ قبلی رو برمی‌گردونه
        try:
            _rename_course_schedule_references(course["term_name"], course_id, old_name, new_name)
        except Exception:
            logging.exception(
                "editcourse: _rename_course_schedule_references crashed (course_id=%s)", course_id
            )
        dbmod.log_action(
            update.effective_user.id, "RENAME_COURSE", term=course["term_name"],
            detail=f"{old_name} -> {new_name}",
        )
        await update.message.reply_text(f"اسم درس «{old_name}» به «{new_name}» تغییر کرد ✅")
        return

    # حالت ۱-ج: منتظر اسم جدید برای ویرایش نام یه ترمیم (فقط سوپرادمین)
    pending_term_rename = context.user_data.get("awaiting_term_rename")
    if pending_term_rename:
        context.user_data.pop("awaiting_term_rename", None)
        if not is_super_admin(update.effective_user.id):
            await update.message.reply_text("⛔️ فقط سوپرادمین می‌تونه اسمِ ترم رو عوض کنه.")
            return
        term_id = pending_term_rename["term_id"]
        new_name = update.message.text.strip()
        ok, result = dbmod.rename_term_by_id(term_id, new_name)
        if not ok:
            await update.message.reply_text(f"❌ {result}")
            return
        old_name = result  # rename_term_by_id موفق -> اسمِ قبلی رو برمی‌گردونه
        _rename_schedule_references(old_name, new_name)
        dbmod.log_action(update.effective_user.id, "RENAME_TERM", term=new_name, detail=f"{old_name} -> {new_name}")
        await update.message.reply_text(f"اسم ترم «{old_name}» به «{new_name}» تغییر کرد ✅")
        return

    # حالت ۱-د: منتظر اسمِ بخشِ (Section) جدیدیم (فقط سوپرادمین)
    if context.user_data.get("awaiting_section_name"):
        context.user_data.pop("awaiting_section_name", None)
        if not is_super_admin(update.effective_user.id):
            await update.message.reply_text("⛔️ فقط سوپرادمین می‌تونه بخش جدید بسازه.")
            return
        name = update.message.text.strip()
        new_id = dbmod.add_group(name)
        if new_id is None:
            await update.message.reply_text("این اسم قبلاً برای یه بخشِ دیگه استفاده شده یا خالیه.")
            return
        dbmod.log_action(update.effective_user.id, "ADD_SECTION", detail=name)
        await update.message.reply_text(
            f"بخشِ «{name}» ساخته شد ✅\nحالا با /addsemester می‌تونی بهش ترم اضافه کنی."
        )
        return

    # حالت ۱-ه: منتظر اسم جدید برای ویرایشِ نامِ یه بخشیم (فقط سوپرادمین)
    pending_section_rename = context.user_data.get("awaiting_section_rename")
    if pending_section_rename:
        context.user_data.pop("awaiting_section_rename", None)
        if not is_super_admin(update.effective_user.id):
            await update.message.reply_text("⛔️ فقط سوپرادمین می‌تونه اسمِ بخش رو عوض کنه.")
            return
        group_id = pending_section_rename["group_id"]
        new_name = update.message.text.strip()
        ok, result = dbmod.rename_group_by_id(group_id, new_name)
        if not ok:
            await update.message.reply_text(f"❌ {result}")
            return
        old_name = result
        dbmod.log_action(update.effective_user.id, "RENAME_SECTION", detail=f"{old_name} -> {new_name}")
        await update.message.reply_text(f"اسم بخش «{old_name}» به «{new_name}» تغییر کرد ✅")
        return

    # حالت ۱-و: منتظر اسمِ دسته‌ی جدیدیم (برای یه درسِ خاص -- فقط همون درس اثر می‌گیره)
    pending_cat_add = context.user_data.get("awaiting_category_add")
    if pending_cat_add:
        context.user_data.pop("awaiting_category_add", None)
        course_id = pending_cat_add["course_id"]
        course = dbmod.get_course_by_id(course_id)
        if course is None:
            await update.message.reply_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
            return
        if not can_manage_term(update.effective_user.id, course["term_name"]):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{course['term_name']}» رو ندارید.")
            return
        name = update.message.text.strip()
        new_id = dbmod.add_category(course_id, name)
        if new_id is None:
            await update.message.reply_text("این درس از قبل دسته‌ای با این اسم داره (یا اسم خالیه).")
            return
        dbmod.log_action(
            update.effective_user.id, "ADD_CATEGORY", term=course["term_name"], detail=f"{course['name']} / {name}"
        )
        await update.message.reply_text(f"دسته‌ی «{name}» به «{course['name']}» اضافه شد ✅")
        return

    # حالت ۱-ز: منتظر اسم جدید برای ویرایشِ نامِ یه دسته‌ی خاصِ یه درسیم -- فقط
    # همین درس اثر می‌گیره، حتی اگه درسِ دیگه‌ای دسته‌ی هم‌نام داشته باشه.
    pending_cat_rename = context.user_data.get("awaiting_category_rename")
    if pending_cat_rename:
        context.user_data.pop("awaiting_category_rename", None)
        course_id = pending_cat_rename["course_id"]
        category_id = pending_cat_rename["category_id"]
        course = dbmod.get_course_by_id(course_id)
        if course is None:
            await update.message.reply_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
            return
        if not can_manage_term(update.effective_user.id, course["term_name"]):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{course['term_name']}» رو ندارید.")
            return
        new_name = update.message.text.strip()
        ok, result = dbmod.rename_category(category_id, new_name)
        if not ok:
            await update.message.reply_text(f"❌ {result}")
            return
        old_name = result
        dbmod.log_action(
            update.effective_user.id, "RENAME_CATEGORY", term=course["term_name"],
            detail=f"{course['name']}: {old_name} -> {new_name}",
        )
        await update.message.reply_text(
            f"اسمِ دسته‌ی «{old_name}» توی «{course['name']}» به «{new_name}» تغییر کرد ✅ "
            "(فقط همین درس -- درس‌های دیگه دست‌نخورده موندن)"
        )
        return

    # حالت ۲: منتظر متن راهنمای مطالعه‌ایم
    pending_guide = context.user_data.get("pending_guide")
    if pending_guide:
        term_name, course_name = pending_guide["term"], pending_guide["course"]
        context.user_data.pop("pending_guide", None)
        if not can_manage_term(update.effective_user.id, term_name):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{term_name}» رو ندارید.")
            return
        guide_text = update.message.text.strip()
        data = load_data()
        data.setdefault(term_name, {}).setdefault(course_name, new_course())
        data[term_name][course_name]["guide"] = guide_text
        save_data(data)
        dbmod.log_action(update.effective_user.id, "SET_GUIDE", term=term_name, detail=course_name)
        await update.message.reply_text(f"راهنمای مطالعه‌ی «{course_name}» ثبت شد ✅")
        return

    # حالت ۳: منتظر فایلیم، ولی ادمین به‌جای فایل یه لینک فرستاده
    pending_upload = context.user_data.get("pending_upload")
    if pending_upload and "http" in update.message.text:
        term_name = pending_upload["term"]
        course_name = pending_upload["course"]
        category = pending_upload["category"]
        if not can_manage_term(update.effective_user.id, term_name):
            await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{term_name}» رو ندارید.")
            return
        text = update.message.text.strip()
        if "|" in text:
            caption, url = [p.strip() for p in text.split("|", 1)]
        else:
            url, caption = text, "فایل حجیم (لینک خارجی)"

        try:
            data = load_data()
            course = data.setdefault(term_name, {}).setdefault(course_name, new_course())
            ensure_course_shape(course)
            link_entry = {"type": "link", "url": url, "caption": caption, "downloads": 0}
            if pending_upload.get("group_id"):
                link_entry["group_id"] = pending_upload["group_id"]
            course["categories"].setdefault(category, {"files": []})["files"].append(link_entry)
            save_data(data)
        except Exception:
            logger.exception("خطا توی ذخیره‌ی لینک آپلودی (term=%s course=%s)", term_name, course_name)
            await update.message.reply_text(
                "❌ این لینک ذخیره نشد (خطای داخلی). فایل‌های قبلی این سشن سالمن؛ می‌تونی دوباره امتحان کنی.",
                reply_markup=upload_progress_keyboard(),
            )
            return
        dbmod.log_action(update.effective_user.id, "ADD_LINK_FILE", term=term_name, detail=f"{course_name} caption={caption}")
        context.user_data["last_upload_path"] = {"term": term_name, "course": course_name, "category": category}
        await update.message.reply_text(
            f"لینک «{caption}» به «{term_name} - {course_name} - {category}» اضافه شد ✅\n"
            "فایل/لینک بعدی رو هم می‌تونی همین‌جا بفرستی.",
            reply_markup=upload_progress_keyboard(),
        )
        await broadcast_new_file(context, term_name, course_name, category, caption)
        return


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # --- ارسال فایل توسط یه دانشجوی عادی (نه ادمین) ---
    file_submission = context.user_data.get("file_submission")
    if file_submission:
        await handle_file_submission(update, context, file_submission)
        return

    if not is_admin(update.effective_user.id):
        return
    pending = context.user_data.get("pending_upload")
    if not pending:
        return

    term_name, course_name, category = pending["term"], pending["course"], pending["category"]
    if not can_manage_term(update.effective_user.id, term_name):
        await update.message.reply_text(f"⛔️ شما دسترسی مدیریت «{term_name}» رو ندارید.")
        context.user_data.pop("pending_upload", None)
        return

    message = update.message
    if message.document:
        file_id = message.document.file_id
        default_name = message.document.file_name
    elif message.photo:
        file_id = message.photo[-1].file_id
        default_name = "تصویر"
    else:
        await message.reply_text("این نوع فایل پشتیبانی نمی‌شه. سند، عکس یا زیپ بفرست.")
        return

    # همون نکته‌ی handle_file_submission: کپشن تلگرامی فقط توضیحه، جای اسم فایل رو نمی‌گیره.
    description = (message.caption or "").strip()

    # قبل از ذخیره‌ی نهایی، اول اسم واقعیِ فایل رو با ادمین تأیید می‌کنیم. چون ادمین ممکنه
    # چند فایل رو پشت سر هم و بدون صبر کردن برای تأیید هر کدوم بفرسته، اینجا به‌جای یک
    # اسلاتِ تکی از یک صف (upload_queue) استفاده می‌کنیم تا فایل‌ها گم یا بازنویسی نشن.
    # اسم نمایشیِ جدا نداریم -- همون caption (که مستقیماً اسم فایله) با تأیید یا تغییرنامِ
    # ادمین ثبت می‌شه.
    item = {
        "term": term_name,
        "course": course_name,
        "category": category,
        "file_id": file_id,
        "default_name": default_name,
        "description": description,
    }
    if pending.get("group_id"):
        item["group_id"] = pending["group_id"]
        item["group_name"] = pending.get("group_name", "")
    queue = context.user_data.setdefault("upload_queue", [])
    queue.append(item)
    if len(queue) == 1:
        # صف قبلاً خالی بود؛ همین الان دیالوگ تأیید رو نشون بده.
        context.user_data.pop("awaiting_upload_new_name", None)
        await _show_next_upload_confirm(context, message.chat_id)
    else:
        # یه فایل دیگه هنوز منتظر تأییده؛ این یکی فقط توی صف قرار می‌گیره و بعداً پردازش می‌شه.
        await message.reply_text(
            f"📥 فایل «{default_name}» دریافت شد و توی صف قرار گرفت (شماره {len(queue)}).\n"
            "به محض تموم شدن تأیید فایل قبلی، نوبتش می‌رسه."
        )


def _upload_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ بله، همین نام", callback_data="upnm_yes")],
            [InlineKeyboardButton("✏️ تغییر نام", callback_data="upnm_rename")],
        ]
    )


async def _show_next_upload_confirm(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """اگه صف آپلود (upload_queue) خالی نباشه، دیالوگ تأیید نام رو برای فایل جلوی صف نشون می‌ده."""
    queue = context.user_data.get("upload_queue") or []
    if not queue:
        return
    next_item = queue[0]
    remaining = len(queue) - 1
    extra = f"\n\n🕓 {remaining} فایل دیگه توی صف منتظرن." if remaining else ""
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"نام فایل: «{next_item['default_name']}»\nفایل با همین نام قرار بگیره؟{extra}",
        reply_markup=_upload_confirm_markup(),
    )


async def _finalize_pending_upload(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, pending: dict, final_name: str
) -> None:
    """بعد از تأیید (یا تغییرنامِ) اسم فایل، همینجا واقعاً ذخیره‌ش می‌کنه."""
    term_name, course_name, category = pending["term"], pending["course"], pending["category"]
    if not can_manage_term(user_id, term_name):
        await context.bot.send_message(chat_id=chat_id, text=f"⛔️ شما دسترسی مدیریت «{term_name}» رو ندارید.")
        return

    try:
        data = load_data()
        course = data.setdefault(term_name, {}).setdefault(course_name, new_course())
        ensure_course_shape(course)
        file_entry = {
            "type": "file",
            "file_id": pending["file_id"],
            "caption": final_name,
            "description": pending.get("description", ""),
            "downloads": 0,
        }
        if pending.get("group_id"):
            file_entry["group_id"] = pending["group_id"]
        course["categories"].setdefault(category, {"files": []})["files"].append(file_entry)
        save_data(data)
    except Exception:
        # اگه یکی از فایلِ‌های وسط یه سشن چندفایلی خطا بخوره، فایل‌های قبلی که قبلاً
        # با موفقیت ذخیره شدن دست‌نخورده می‌مونن و سشن آپلود هم باز می‌مونه تا فایل بعدی بیاد.
        logger.exception("خطا توی ذخیره‌ی فایل آپلودی (term=%s course=%s)", term_name, course_name)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ این فایل ذخیره نشد (خطای داخلی). فایل‌های قبلی این سشن سالمن؛ می‌تونی فایل بعدی رو بفرستی، "
                "یا با «✅ پایان آپلود» / «🔙 تغییر مسیر» ادامه بدی."
            ),
            reply_markup=upload_progress_keyboard(),
        )
        return

    dbmod.log_action(user_id, "UPLOAD_FILE", term=term_name, detail=f"{course_name} caption={final_name}")

    # سشنِ آپلود عمداً بسته نمی‌شه -- تا وقتی ادمین «✅ پایان آپلود» یا «🔙 تغییر مسیر» رو نزنه،
    # می‌تونه فایل‌های بعدی رو هم پشت سر هم برای همین مسیر بفرسته.
    context.user_data["last_upload_path"] = {"term": term_name, "course": course_name, "category": category}

    courses = dbmod.get_courses_with_ids(term_name)
    course_id = next((c["id"] for c in courses if c["name"] == course_name), None)
    keyboard = build_post_upload_keyboard(term_name, course_name, course_id)

    group_name = pending.get("group_name")
    path_label = (
        f"{term_name} - {course_name} - {category} → {group_name}"
        if group_name else f"{term_name} - {course_name} - {category}"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"فایل «{final_name}» به «{path_label}» اضافه شد ✅\n"
            "فایل بعدی رو هم می‌تونی همین‌جا بفرستی، یا از دکمه‌های زیر بخش دیگه‌ای رو انتخاب کن."
        ),
        reply_markup=keyboard,
    )
    await broadcast_new_file(context, term_name, course_name, category, final_name)


async def upload_name_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✅ بله، همین نام -- فایل با همون اسم واقعیِ فعلیش ثبت می‌شه."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting_upload_new_name", None)
    queue = context.user_data.get("upload_queue") or []
    if not queue:
        await query.edit_message_text("فایلی در انتظار تأیید نیست.")
        return
    pending = queue.pop(0)
    await query.edit_message_text(f"باشه، با نام «{pending['default_name']}» ثبت می‌شه...")
    await _finalize_pending_upload(
        context, query.message.chat_id, update.effective_user.id, pending, pending["default_name"]
    )
    # اگه فایل دیگه‌ای توی صف مونده، دیالوگ تأیید بعدی رو نشون بده.
    await _show_next_upload_confirm(context, query.message.chat_id)


async def upload_name_rename_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✏️ تغییر نام -- منتظر اسم جدید از ادمین می‌مونیم."""
    query = update.callback_query
    await query.answer()
    queue = context.user_data.get("upload_queue") or []
    if not queue:
        await query.edit_message_text("فایلی در انتظار تأیید نیست.")
        return
    pending = queue[0]
    context.user_data["awaiting_upload_new_name"] = True
    await query.edit_message_text(
        f"اسم فعلی: «{pending['default_name']}»\n\n"
        "اسم جدید فایل رو بفرست (لازم نیست پسوند رو بنویسی؛ اگه ننویسی، پسوندِ فایلِ اصلی خودش حفظ می‌شه).\n"
        "برای انصراف /cancel رو بفرست."
    )


async def upload_name_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگه منتظر اسم جدید برای فایل تازه‌آپلودشده بودیم، اینجا نهاییش می‌کنه.
    خروجی True یعنی این پیام مصرف شد و receive_text نباید ادامه بده."""
    if not context.user_data.get("awaiting_upload_new_name"):
        return False
    context.user_data.pop("awaiting_upload_new_name", None)
    queue = context.user_data.get("upload_queue") or []
    if not queue:
        await update.message.reply_text("فایلی در انتظار تأیید نیست.")
        return True
    new_name = (update.message.text or "").strip()
    if new_name == "/cancel":
        # قبلاً این مسیر /cancel رو مثل بقیه‌ی متن‌ها به‌عنوان اسمِ فایل ثبت می‌کرد
        # (باگ)؛ الان درست مثل تأییدِ اسم برمی‌گرده به دیالوگِ همون فایل.
        context.user_data["awaiting_upload_new_name"] = False
        await update.message.reply_text("تغییر نام لغو شد؛ دوباره تصمیم بگیر.")
        await _show_next_upload_confirm(context, update.effective_chat.id)
        return True
    if not new_name:
        await update.message.reply_text("اسم نامعتبره؛ دوباره بفرست.")
        context.user_data["awaiting_upload_new_name"] = True
        return True
    pending = queue.pop(0)
    final_name = apply_original_extension(new_name, pending["default_name"])
    await update.message.reply_text(f"باشه، با نام «{final_name}» ثبت می‌شه...")
    await _finalize_pending_upload(context, update.effective_chat.id, update.effective_user.id, pending, final_name)
    # اگه فایل دیگه‌ای توی صف مونده، دیالوگ تأیید بعدی رو نشون بده.
    await _show_next_upload_confirm(context, update.effective_chat.id)
    return True


# ============================================================
# دانشجو: مرور برنامه -> درس -> (راهنما یا فایل)
# ============================================================

async def list_programs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌ی اصلی «📚 دروس» و دستور /darsha — گروه‌ها (مقطع) یا ترم‌ها رو نشون می‌ده."""
    load_data()  # مطمئن می‌شه هر ترمی درس‌های پیش‌فرضش رو داره
    text, markup = build_top_menu()
    await update.message.reply_text(text, reply_markup=markup)


def build_top_menu():
    """اگه بیش از یه گروه/مقطع وجود داشته باشه، اول گروه‌ها رو نشون می‌ده؛
    بخش‌های خالی هم نمایش داده می‌شن. برای تک‌گروهِ دارای ترم، منوی تخت حفظ می‌شه."""
    groups = dbmod.get_groups_with_ids()
    programs = get_program_names() if len(groups) <= 1 else []
    if len(groups) <= 1 and programs:
        buttons = [[InlineKeyboardButton(p, callback_data=f"prog:{p}")] for p in programs]
        return "📚 کدوم برنامه؟", InlineKeyboardMarkup(buttons)
    buttons = [[InlineKeyboardButton(g["name"], callback_data=f"grp:{g['name']}")] for g in groups]
    return "🎓 کدوم مقطع/گروه؟", InlineKeyboardMarkup(buttons)


async def show_group_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_name = query.data.split(":", 1)[1]
    terms = dbmod.get_terms_in_group(group_name)
    back_button = InlineKeyboardButton("🔙 بازگشت", callback_data="back_groups")
    if not terms:
        await query.edit_message_text(
            f"«{group_name}» هنوز ترمی نداره.", reply_markup=InlineKeyboardMarkup([[back_button]])
        )
        return
    buttons = [[InlineKeyboardButton(t, callback_data=f"prog:{t}")] for t in terms]
    buttons.append([back_button])
    await query.edit_message_text(f"{group_name} — کدوم ترم؟", reply_markup=InlineKeyboardMarkup(buttons))


async def back_to_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text, markup = build_top_menu()
    await query.edit_message_text(text, reply_markup=markup)


async def show_program_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    await render_course_list(query, program)


async def render_course_list(query, program: str) -> None:
    data = load_data()
    courses = data.get(program, {})
    # اگه بیش از یه گروه داریم، دکمه‌ی بازگشت باید به لیست ترم‌های همون گروه برگرده؛
    # وگرنه (تک‌گروهی، سازگار با قبل) مستقیم به لیست تخت ترم‌ها.
    groups = dbmod.get_groups_with_terms()
    if len(dbmod.get_groups_with_ids()) > 1:
        owning_group = next((g for g, terms in groups if program in terms), None)
        back_button = (
            InlineKeyboardButton("🔙 بازگشت", callback_data=f"grp:{owning_group}")
            if owning_group
            else InlineKeyboardButton("🔙 بازگشت", callback_data="back_groups")
        )
    else:
        back_button = InlineKeyboardButton("🔙 بازگشت", callback_data="back_programs")
    if not courses:
        await query.edit_message_text(
            f"«{program}» هنوز درسی نداره.", reply_markup=InlineKeyboardMarkup([[back_button]])
        )
        return
    # کورس‌های چرخشی (فاز ۱۲): کورسی که ادمین «فعال» علامت زده با 🟢 بالای لیست
    # میاد؛ کورس‌هایی که قبلاً فعال بودن و الان تمام‌شدن، تو یه بخشِ جدا در پایین
    # (آرشیو) نشون داده می‌شن. درس‌هایی که اصلاً درگیرِ این چرخه نیستن (یعنی
    # is_active همیشه False بوده و finished_at هم نداشتن) دقیقاً مثلِ قبل و
    # بدونِ هیچ پیشوندی نمایش داده می‌شن -- برای ترم‌های غیرِچرخشی هیچ تغییری
    # تو نمایش حس نمی‌شه.
    status_map = {c["name"]: c for c in dbmod.get_courses_with_status(program)}
    active_names, normal_names, archived_names = [], [], []
    for name in courses.keys():
        st = status_map.get(name)
        if st and st["is_active"]:
            active_names.append(name)
        elif st and st["finished_at"]:
            archived_names.append(name)
        else:
            normal_names.append(name)

    buttons = [
        [InlineKeyboardButton(f"🟢 {name}", callback_data=f"course:{courses[name]['id']}")]
        for name in active_names
    ]
    buttons += [
        [InlineKeyboardButton(name, callback_data=f"course:{courses[name]['id']}")]
        for name in normal_names
    ]
    if archived_names:
        buttons.append([InlineKeyboardButton("— کورس‌های تمام‌شده —", callback_data="noop")])
        buttons += [
            [InlineKeyboardButton(f"⚪️ {name}", callback_data=f"course:{courses[name]['id']}")]
            for name in archived_names
        ]
    buttons.append([back_button])
    await query.edit_message_text(f"دروس «{program}»:", reply_markup=InlineKeyboardMarkup(buttons))


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌ی صرفاً جداکننده/برچسب (مثلاً تیترِ «کورس‌های تمام‌شده» تو لیستِ دروس) --
    هیچ کاری نمی‌کنه، فقط callback_query رو جواب می‌ده که تلگرام لودینگ نشون نده."""
    await update.callback_query.answer()


async def back_to_programs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text, markup = build_top_menu()
    await query.edit_message_text(text, reply_markup=markup)


async def back_to_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    await render_course_list(query, program)


async def show_course_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بعد از انتخاب درس، دسته‌بندی‌های اون درس رو نشون می‌ده."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course_ref = dbmod.get_course_by_id(course_id)
    if course_ref is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course_ref["term_name"], course_ref["name"]
    track_course_view(f"{program} - {course_name}")

    data = load_data()
    course_data = data.get(program, {}).get(course_name, {})
    has_guide = bool(course_data.get("guide"))

    back_button = InlineKeyboardButton("🔙 بازگشت به دروس", callback_data=f"back_courses:{program}")

    buttons = []
    if has_guide:
        buttons.append([InlineKeyboardButton("📖 راهنمای مطالعه", callback_data=f"guide:{course_id}")])
    buttons.extend(build_course_category_buttons(program, course_name, course_data))

    buttons.append([back_button])
    await query.edit_message_text(
        f"«{course_name}» — یه بخش رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_manabe_submenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """زیرمنوی «📚 منابع»: ۴ زیربخشش (رفرنس و پاور / جزوه / خلاصه / نکات امتحانی)."""
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course_ref = dbmod.get_course_by_id(course_id)
    if course_ref is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course_ref["term_name"], course_ref["name"]

    data = load_data()
    categories = data.get(program, {}).get(course_name, {}).get("categories", {})
    back_button = InlineKeyboardButton("🔙 بازگشت", callback_data=f"course:{course_id}")

    buttons = []
    for cat in CATEGORIES[:MANABE_COUNT]:
        if cat not in categories:  # عمداً از طریقِ /editcategories حذف شده
            continue
        cat_id = dbmod.get_category_id_for_course(course_id, cat)
        if cat_id is None:
            continue
        n = len(categories.get(cat, {}).get("files", []))
        label = f"{cat} ({n})" if n else cat
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat:{course_id}:{cat_id}")])
    buttons.append([back_button])

    await query.edit_message_text(
        f"«{course_name}» — {MANABE_LABEL}:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def render_category_files(query, user_id: int, course_id: int, cat_idx: int) -> None:
    """توجه: با course_id کار می‌کنه (نه program/course_name) تا callback_dataهای
    manabe:/course:/filex: از ۶۴ بایتِ مجاز تلگرام رد نشن -- نگاه کن به توضیح توی
    build_course_category_buttons."""
    course_ref = dbmod.get_course_by_id(course_id)
    if course_ref is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course_ref["term_name"], course_ref["name"]

    # نکته‌ی مهم (باگ‌فیکس اصلی): cat_idx دیگه ایندکسِ CATEGORIES سراسری نیست --
    # id واقعیِ ردیفِ categories است (نگاه کن به category_name_by_id/categories_for).
    # این یعنی دسته‌های کاملاً سفارشی (مثلِ «گری» زیرِ یه درسِ حالتِ-سفارشی) هم اینجا
    # درست resolve می‌شن، نه فقط ۱۱ تا دسته‌ی استاندارد.
    cat_row = dbmod.get_category_by_id(cat_idx)
    if cat_row is None or cat_row["course_id"] != course_id:
        fallback_back = InlineKeyboardButton("🔙 بازگشت به بخش‌ها", callback_data=f"course:{course_id}")
        await query.edit_message_text("این بخش پیدا نشد.", reply_markup=InlineKeyboardMarkup([[fallback_back]]))
        return
    category = cat_row["name"]

    # زیرمنوی «📚 منابع» فقط برای درس‌های *استاندارد* معنی داره (نگاه کن به
    # build_course_category_buttons)؛ درس‌های حالتِ-سفارشی اصلاً چنین زیرمنویی ندارن.
    is_manabe_sub = (
        course_ref.get("category_mode") != CUSTOM_MODE and category in CATEGORIES[:MANABE_COUNT]
    )
    if is_manabe_sub:
        back_button = InlineKeyboardButton(
            f"🔙 بازگشت به {MANABE_LABEL}", callback_data=f"manabe:{course_id}"
        )
    else:
        back_button = InlineKeyboardButton("🔙 بازگشت به بخش‌ها", callback_data=f"course:{course_id}")

    data = load_data()
    cat_data = data.get(program, {}).get(course_name, {}).get("categories", {}).get(category, {})
    files = cat_data.get("files", [])
    groups = cat_data.get("groups", [])

    if not files and not groups:
        await query.edit_message_text(
            f"هنوز چیزی توی «{category}» اضافه نشده.",
            reply_markup=InlineKeyboardMarkup([[back_button]]),
        )
        return

    saved_fids = set(get_saved_fids(user_id))
    buttons = []
    # گروه‌ها (مثلاً استادها) رو اول نشون بده -- زدنشون یه زیرمنو باز می‌کنه
    for g in groups:
        buttons.append([InlineKeyboardButton(f"📁 {g['name']}", callback_data=f"fgrp:{course_id}:{cat_idx}:{g['id']}")])
    # بعد فایل‌ها/نوت‌هایی که هنوز داخل هیچ گروهی نیستن -- دقیقاً رفتار قدیمی
    for i, f in enumerate(files):
        if f.get("group_id"):
            continue
        if f.get("type") == "note":
            buttons.append([InlineKeyboardButton(f"📝 {f.get('caption') or 'نوت'}", callback_data=f"notex:{f.get('fid', '')}")])
            continue
        fid = f.get("fid", "")
        star = "💛" if fid in saved_fids else "⭐️"
        row = [
            InlineKeyboardButton(display_caption(f), callback_data=f"filex:{course_id}:{cat_idx}:{i}"),
            InlineKeyboardButton(star, callback_data=f"favtoggle:{fid}"),
        ]
        if is_admin(user_id) and fid:
            row.append(InlineKeyboardButton("🔗", callback_data=f"filelink_show:{fid}"))
        buttons.append(row)
    buttons.append([back_button])
    await query.edit_message_text(
        f"«{category}» (⭐️ برای ذخیره در علاقه‌مندی‌ها):", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def render_group_files(query, user_id: int, course_id: int, cat_idx: int, group_id: int) -> None:
    """محتوای یک گروهِ خاص (فایل‌ها + نوت‌ها) رو نشون می‌ده. index در callback filex همیشه
    نسبت به لیستِ کاملِ فایل‌های دسته‌بندیه (نه لیستِ فیلترشده‌ی این گروه) -- همون قراردادِ
    send_file. برای همین اینجا هم روی همون لیستِ کامل enumerate می‌کنیم و فقط نمایش رو فیلتر می‌کنیم."""
    course_ref = dbmod.get_course_by_id(course_id)
    if course_ref is None:
        await query.edit_message_text("این درس دیگه پیدا نشد؛ شاید حذف شده.")
        return
    program, course_name = course_ref["term_name"], course_ref["name"]

    cat_row = dbmod.get_category_by_id(cat_idx)
    if cat_row is None or cat_row["course_id"] != course_id:
        await query.edit_message_text("این بخش پیدا نشد.")
        return
    category = cat_row["name"]

    data = load_data()
    cat_data = data.get(program, {}).get(course_name, {}).get("categories", {}).get(category, {})
    files = cat_data.get("files", [])
    group = next((g for g in cat_data.get("groups", []) if g["id"] == group_id), None)
    group_name = group["name"] if group else "گروه"

    back_button = InlineKeyboardButton("🔙 بازگشت", callback_data=f"cat:{course_id}:{cat_idx}")
    saved_fids = set(get_saved_fids(user_id))
    buttons = []
    for i, f in enumerate(files):
        if f.get("group_id") != group_id:
            continue
        if f.get("type") == "note":
            buttons.append([InlineKeyboardButton(f"📝 {f.get('caption') or 'نوت'}", callback_data=f"notex:{f.get('fid', '')}")])
            continue
        fid = f.get("fid", "")
        star = "💛" if fid in saved_fids else "⭐️"
        row = [
            InlineKeyboardButton(display_caption(f), callback_data=f"filex:{course_id}:{cat_idx}:{i}"),
            InlineKeyboardButton(star, callback_data=f"favtoggle:{fid}"),
        ]
        if is_admin(user_id) and fid:
            row.append(InlineKeyboardButton("🔗", callback_data=f"filelink_show:{fid}"))
        buttons.append(row)

    if not buttons:
        await query.edit_message_text(
            f"هنوز چیزی توی «{group_name}» اضافه نشده.",
            reply_markup=InlineKeyboardMarkup([[back_button]]),
        )
        return

    buttons.append([back_button])
    await query.edit_message_text(
        f"«{category} → {group_name}» (⭐️ برای ذخیره در علاقه‌مندی‌ها):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_group_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, group_id_str = query.data.split(":", 3)
    await render_group_files(
        query, update.effective_user.id, int(course_id_str), int(cat_idx_str), int(group_id_str)
    )


async def show_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """توجه: نوت‌ها هم مثل فایل‌ها با fid آدرس‌دهی می‌شن (نه ایندکس) -- همون دلیلِ
    favtoggle: نوت متنه، نه فایلِ تلگرامی، پس به‌جای send_document فقط متنش فرستاده می‌شه."""
    query = update.callback_query
    _, fid = query.data.split(":", 1)
    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found or found[4].get("type") != "note":
        await query.answer("این نوت دیگه پیدا نشد؛ شاید حذف شده.", show_alert=True)
        return
    await query.answer()
    note = found[4]
    caption = note.get("caption", "")
    content = note.get("content", "")
    text = f"📝 {caption}\n\n{content}" if caption else f"📝 {content}"
    await query.message.reply_text(text)


async def show_category_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str = query.data.split(":", 2)
    await render_category_files(query, update.effective_user.id, int(course_id_str), int(cat_idx_str))


async def toggle_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """توجه: callback_data فقط fid رو داره (نه program/course_name/cat_idx) چون تلگرام
    callback_data رو به ۶۴ بایت UTF-8 محدود می‌کنه و برای درس‌های با اسم طولانی
    (مثلاً «ترم ۳ - علوم تشریح سر و گردن») این محدودیت رد می‌شد و کل کیبورد شکست
    می‌خورد (دکمه‌ها اصلاً کار نمی‌کردن). fid توی کل دیتابیس یکتاست، پس
    find_file_by_fid کافیه تا بقیه‌ی مسیر (برنامه/درس/دسته) پیدا بشه."""
    query = update.callback_query
    _, fid = query.data.split(":", 1)
    if not fid:
        await query.answer("این فایل قابل ذخیره نیست.", show_alert=True)
        return

    data = load_data()
    found = find_file_by_fid(data, fid)
    if not found:
        await query.answer("این فایل دیگه پیدا نشد؛ شاید حذف شده.", show_alert=True)
        return
    program, course_name, category, _, file_info = found
    course_id = data.get(program, {}).get(course_name, {}).get("id")
    # category اینجا یه اسمه (نه id) -- برای callback باید id واقعیِ همین درس رو
    # پیدا کنیم؛ get_category_id_for_course برای دسته‌های سفارشی هم درست کار می‌کنه.
    category_id = dbmod.get_category_id_for_course(course_id, category) if course_id else None

    is_saved = toggle_saved_fid(update.effective_user.id, fid)
    await query.answer("💛 به علاقه‌مندی‌ها اضافه شد" if is_saved else "از علاقه‌مندی‌ها حذف شد")
    if category_id is None:
        return
    # اگه فایل داخل یه گروهه، همون زیرمنوی گروه رو دوباره نشون بده (نه ریشه‌ی دسته‌بندی)
    group_id = file_info.get("group_id")
    if group_id:
        await render_group_files(query, update.effective_user.id, course_id, category_id, group_id)
    else:
        await render_category_files(query, update.effective_user.id, course_id, category_id)


async def saved_files_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دستور /saved -- فایل‌های ذخیره‌شده‌ی (علاقه‌مندی‌های) کاربر رو نشون می‌ده."""
    user_id = update.effective_user.id
    fids = get_saved_fids(user_id)
    if not fids:
        await update.message.reply_text(
            "هنوز چیزی توی «فایل‌های ذخیره‌شده‌ت» نیست.\n"
            "توی هر بخش، کنار هر فایل یه دکمه‌ی ⭐️ هست؛ بزنش تا اینجا اضافه بشه."
        )
        return

    data = load_data()
    buttons = []
    stale = []
    for fid in fids:
        found = find_file_by_fid(data, fid)
        if not found:
            stale.append(fid)
            continue
        program, course_name, category, index, f = found
        course_id = data.get(program, {}).get(course_name, {}).get("id")
        category_id = dbmod.get_category_id_for_course(course_id, category) if course_id else None
        if category_id is None:
            stale.append(fid)
            continue
        label = f"{display_caption(f)} ({course_name})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"filex:{course_id}:{category_id}:{index}")])

    if stale:
        users = load_users()
        entry = users.setdefault(str(user_id), {})
        entry["saved_fids"] = [fid for fid in fids if fid not in stale]
        save_users(users)

    if not buttons:
        await update.message.reply_text("فایل‌های ذخیره‌شده‌ت دیگه در دسترس نیستن.")
        return

    await update.message.reply_text(
        "⭐️ فایل‌های ذخیره‌شده‌ت:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, course_id_str, cat_idx_str, index_str = query.data.split(":", 3)
    cat_idx = int(cat_idx_str)
    index = int(index_str)

    course_ref = dbmod.get_course_by_id(int(course_id_str))
    if course_ref is None:
        await query.message.reply_text("این فایل دیگه در دسترس نیست.")
        return
    program, course_name = course_ref["term_name"], course_ref["name"]

    cat_row = dbmod.get_category_by_id(cat_idx)
    if cat_row is None or cat_row["course_id"] != int(course_id_str):
        await query.message.reply_text("این فایل دیگه در دسترس نیست.")
        return
    category = cat_row["name"]

    data = load_data()
    files = data.get(program, {}).get(course_name, {}).get("categories", {}).get(category, {}).get("files", [])
    if index >= len(files):
        await query.message.reply_text("این فایل دیگه در دسترس نیست.")
        return

    file_info = files[index]
    file_info["downloads"] = file_info.get("downloads", 0) + 1
    save_data(data)

    caption = display_caption(file_info)
    description = file_info.get("description", "")
    full_caption = f"{caption}\n\n📝 {description}" if description else caption
    if file_info.get("type") == "link":
        await query.message.reply_text(f"{caption}:\n{file_info['url']}")
    elif file_info.get("type") == "text":
        await query.message.reply_text(file_info.get("content", caption))
    else:
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_info["file_id"],
            caption=full_caption,
        )


# ============================================================
# برنامه‌ریزی: منوی مشترک
# ============================================================

async def schedule_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton("📅 برنامه‌ی امروزم", callback_data="today_agenda")],
        [InlineKeyboardButton("🏫 برنامه کلاس‌ها (رسمی)", callback_data="class_schedule_menu")],
        [InlineKeyboardButton("📝 برنامه امتحانات", callback_data="exam_schedule_menu")],
        [InlineKeyboardButton("📖 برنامه مطالعه شخصی", callback_data="personal_schedule_menu")],
        [InlineKeyboardButton("👥 گروه‌های عملی من", callback_data="sgroups_root")],
    ]
    await query.edit_message_text("📅 برنامه‌ریزی\nکدوم بخش؟", reply_markup=InlineKeyboardMarkup(buttons))


# ============================================================
# برنامه‌ریزی -> گروه‌های عملیِ من (فاز ۱۳): دانشجو خودش گروهی رو که از قبل
# (توسط نماینده/آموزش) بهش تعلق گرفته، برای هر درسِ گروه‌بندی‌شده انتخاب می‌کنه.
# بات هیچ تخصیصی انجام نمی‌ده، فقط بازتاب‌دهنده‌ست -- بدونِ قفل، بدونِ محدودیتِ
# ظرفیت، هر وقت بخواد قابلِ تغییره.
# ============================================================

async def render_student_groups_root(query, user_id: int, program: str) -> None:
    status = dbmod.get_student_group_status(user_id, program)
    if not status:
        buttons = [[InlineKeyboardButton("🔙 بازگشت", callback_data="schedule_root")]]
        await query.edit_message_text(
            f"«{program}» الان درسِ گروه‌بندی‌شده‌ای نداره.", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    buttons = []
    for item in status:
        sel = next((g for g in item["groups"] if g["id"] == item["selected_group_id"]), None)
        label = f"{item['course_name']} — {sel['name']}" if sel else f"⚠️ {item['course_name']} (گروهت مشخص نیست)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sgroups_course:{item['course_id']}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="schedule_root")])
    await query.edit_message_text(
        f"👥 گروه‌های عملیِ «{program}»\nروی هر درس بزن تا گروهتو انتخاب/تغییر بدی:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def student_groups_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = get_user_class_program(update.effective_user.id)
    if not program:
        buttons = [
            [InlineKeyboardButton(p, callback_data=f"pick_class_program_groups:{p}")] for p in get_program_names()
        ]
        await query.edit_message_text(
            "اول ترمت رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    await render_student_groups_root(query, update.effective_user.id, program)


async def pick_class_program_for_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مثلِ pick_class_program_for_exam -- وقتی از داخلِ «گروه‌های عملیِ من» ترم
    هنوز انتخاب نشده و کاربر همین‌جا انتخابش می‌کنه، باید بعدش برگرده همینجا
    (نه به روزهای برنامه‌ی کلاسی، که رفتارِ pick_class_program عمومیه)."""
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    set_user_class_program(update.effective_user.id, program)
    await render_student_groups_root(query, update.effective_user.id, program)


async def render_student_group_course(query, user_id: int, course_id: int) -> None:
    course = dbmod.get_course_by_id(course_id)
    if course is None:
        await query.edit_message_text("این درس دیگه وجود نداره.")
        return
    groups = dbmod.get_course_groups(course_id)
    if not groups:
        buttons = [[InlineKeyboardButton("🔙 بازگشت", callback_data="sgroups_root")]]
        await query.edit_message_text(
            f"«{course['name']}» فعلاً هیچ گروهی تعریف نشده.", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    selected = dbmod.get_student_course_group(user_id, course_id)
    buttons = []
    for g in groups:
        mark = "✅ " if g["id"] == selected else ""
        buttons.append(
            [InlineKeyboardButton(
                f"{mark}{g['name']} — {g['day']} {g['start']}-{g['end']}",
                callback_data=f"sgroups_pick:{course_id}:{g['id']}",
            )]
        )
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="sgroups_root")])
    await query.edit_message_text(
        f"«{course['name']}» — کدوم گروهی هستی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def student_groups_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    await render_student_group_course(query, update.effective_user.id, course_id)


async def student_groups_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, course_id_str, group_id_str = query.data.split(":", 2)
    course_id, group_id = int(course_id_str), int(group_id_str)
    group = dbmod.get_course_group_by_id(group_id)
    if group is None or group["course_id"] != course_id:
        await query.answer("این گروه دیگه وجود نداره.", show_alert=True)
        return
    dbmod.set_student_course_group(update.effective_user.id, course_id, group_id)
    await query.answer(f"باشه، ثبت شد: {group['name']} ✅")
    # برمی‌گردیم به همون صفحه‌ی انتخابِ گروه تا تیکِ ✅ جدید رو ببینه
    await render_student_group_course(query, update.effective_user.id, course_id)


async def send_course_categories_new_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, program: str, course_name: str
) -> None:
    """مثل show_course_categories ولی به‌جای ادیت پیام، یه پیام تازه می‌فرسته
    (برای دکمه‌ی «ورود به منابع» توی پیام‌های یادآوری)."""
    data = load_data()
    course_data = data.get(program, {}).get(course_name, {})
    has_guide = bool(course_data.get("guide"))

    buttons = []
    if has_guide:
        buttons.append(
            [InlineKeyboardButton("📖 راهنمای مطالعه", callback_data=f"guide:{course_data.get('id')}")]
        )
    buttons.extend(build_course_category_buttons(program, course_name, course_data))

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"«{course_name}» — یه بخش رو انتخاب کن:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def class_resource_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 2)
    if len(parts) == 2:
        # فرمتِ جدید: class_resource:{course_id}
        try:
            course_id = int(parts[1])
        except ValueError:
            course_id = None
        course = dbmod.get_course_by_id(course_id) if course_id is not None else None
        if course is None:
            await query.message.reply_text("این درس دیگه پیدا نشد.")
            return
        program, course_name = course["term_name"], course["name"]
    elif len(parts) == 3:
        # سازگاری با فرمتِ قدیمیِ class_resource:{program}:{course} -- ممکنه
        # پیامِ یادآوریِ قدیمی هنوز توی چتِ کاربر باشه و روش بزنه.
        _, program, course_name = parts
    else:
        return
    await send_course_categories_new_message(context, query.message.chat_id, program, course_name)


async def render_today_agenda(query, user_id: int, program: str) -> None:
    """📅 برنامه‌ی امروزم -- ترکیبِ کلاسِ رسمی + بلوکِ مطالعه‌ی شخصی + رویدادهای
    ثابتِ هفتگی + هشدارِ گروهِ انتخاب‌نشده، همه با هم و به ترتیبِ ساعت (فاز ۱۷).
    امتحان‌ها چون تاریخشون متنِ آزاده (نه یه تاریخِ واقعیِ قابلِ‌مقایسه با
    امروز)، جای مشخصی تو این نما ندارن -- فقط یه اشاره‌ی کلی بهشون می‌کنیم."""
    now = now_tehran()
    today_fa = PY_WEEKDAY_TO_FA[now.weekday()]
    today_date_str = now.strftime("%Y-%m-%d")

    items = []
    missing_groups = set()

    schedule = load_class_schedule()
    classes_all = sorted(schedule.get(program, {}).get(today_fa, []), key=lambda c: c["start"])
    classes, missing = filter_class_entries_for_student(classes_all, user_id)
    missing_groups.update(missing)
    for c in classes:
        if is_class_cancelled_on(program, today_fa, c, today_date_str):
            continue
        loc = f" — {c['location']}" if c.get("location") else ""
        pending = " — ⚠️ گروه هنوز مشخص نشده" if c.get("group_pending") else ""
        items.append((c["start"], f"🏫 {c['start']}–{c['end']} | {c['course']}{loc}{pending}"))

    personal = load_personal_schedule().get(str(user_id), {})
    for b in personal.get(today_fa, []):
        items.append((b["start"], f"📖 {b['start']}–{b['end']} | {b.get('course') or b.get('program', '')}"))

    for e in load_fixed_events():
        if e["day"] != today_fa or (e["scope"] != "all" and e["scope"] != program):
            continue
        items.append((e["start"], f"🗓 {e['start']}–{e['end']} | {e['name']}"))

    items.sort(key=lambda t: t[0])

    lines = [f"📅 برنامه‌ی امروز — «{program}» ({today_fa})"]
    if not items:
        lines.append("\nهیچ‌چیزِ ثبت‌شده‌ای برای امروز نداری.")
    else:
        lines.append("")
        lines.extend(text for _, text in items)

    if missing_groups:
        lines.append("")
        for name in sorted(missing_groups):
            lines.append(f"⚠️ گروهت برای «{name}» مشخص نیست — از «👥 گروه‌های عملی من» انتخابش کن.")

    if dbmod.list_exams(program):
        lines.append(f"\nℹ️ برای «{program}» امتحان(های) ثبت‌شده‌ای هم هست — از «📝 برنامه امتحانات» ببین.")

    buttons = [[InlineKeyboardButton("🔙 بازگشت", callback_data="schedule_root")]]
    await edit_message_with_keyboard_safe(query, "\n".join(lines), buttons, context_label="today_agenda")


async def today_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = get_user_class_program(update.effective_user.id)
    if not program:
        buttons = _program_buttons("pick_class_program_agenda")
        await query.edit_message_text("اول ترمت رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))
        return
    await render_today_agenda(query, update.effective_user.id, program)


async def pick_class_program_for_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    program = _resolve_program_token(token)
    if not program:
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    set_user_class_program(update.effective_user.id, program)
    await render_today_agenda(query, update.effective_user.id, program)


# ============================================================
# برنامه‌ریزی -> برنامه کلاس‌ها (رسمی): دانشجو
# ============================================================

async def render_class_day_picker(query, program: str) -> None:
    term_id = term_id_for_program(program)
    if term_id is None:
        logger.error("render_class_day_picker: no term found for program=%r", program)
        await query.edit_message_text(f"ترمِ «{program}» توی دیتابیس پیدا نشد. لطفاً با پشتیبانی تماس بگیر.")
        return
    buttons = []
    for day in DAYS_OF_WEEK:
        data = f"class_day:{term_id}:{day}"
        if validate_callback_data(data, context_label=f"class_day/program={program}"):
            buttons.append([InlineKeyboardButton(day, callback_data=data)])
    week_data = f"class_week:{term_id}"
    if validate_callback_data(week_data, context_label=f"class_week/program={program}"):
        buttons.append([InlineKeyboardButton("📆 کل هفته یکجا", callback_data=week_data)])
    buttons.append([InlineKeyboardButton("🔁 تغییر ترم", callback_data="change_class_program")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="schedule_root")])
    await edit_message_with_keyboard_safe(
        query, f"برنامه کلاسیِ «{program}» — کدوم روز؟", buttons, context_label="render_class_day_picker"
    )


async def class_schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = get_user_class_program(update.effective_user.id)
    if not program:
        buttons = _program_buttons("pick_class_program")
        await query.edit_message_text(
            "اول ترمت رو انتخاب کن (فقط یه‌بار لازمه، بعداً هم می‌تونی از همین‌جا عوضش کنی):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    await render_class_day_picker(query, program)


async def pick_class_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    program = _resolve_program_token(token)
    if not program:
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    set_user_class_program(update.effective_user.id, program)
    await render_class_day_picker(query, program)


# ============================================================
# برنامه‌ریزی -> برنامه امتحانات (رسمی): دانشجو
# ============================================================

EXAM_TYPE_LABELS = {"midterm": "📘 میان‌ترم", "final": "🏁 پایان‌ترم"}


def format_exam_line(exam: dict) -> str:
    type_prefix = ""
    if exam.get("exam_type") in EXAM_TYPE_LABELS:
        type_prefix = f"{EXAM_TYPE_LABELS[exam['exam_type']]} — "
    line = f"📝 {type_prefix}{exam['course']} — {exam['exam_date']}"
    if exam.get("exam_time"):
        line += f" ساعت {exam['exam_time']}"
    if exam.get("location"):
        line += f"\n   📍 {exam['location']}"
    return line


async def render_exam_list(query, program: str) -> None:
    exams = dbmod.list_exams(program)
    buttons = [
        [InlineKeyboardButton("🔁 تغییر ترم", callback_data="change_class_program")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="schedule_root")],
    ]
    periods = dbmod.get_term_periods(program) or {}
    period_lines = []
    if periods.get("class_period_start") or periods.get("class_period_end"):
        period_lines.append(
            f"🏫 بازه‌ی کلاسی: {periods.get('class_period_start') or '؟'} تا {periods.get('class_period_end') or '؟'}"
        )
    if periods.get("final_exam_start") or periods.get("final_exam_end"):
        period_lines.append(
            f"🏁 بازه‌ی پایان‌ترم: {periods.get('final_exam_start') or '؟'} تا {periods.get('final_exam_end') or '؟'}"
        )
    period_block = ("\n".join(period_lines) + "\n\n") if period_lines else ""
    if not exams:
        await query.edit_message_text(
            f"{period_block}برای «{program}» هنوز امتحانی ثبت نشده.", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    text = f"{period_block}📝 برنامه امتحانات «{program}»:\n\n" + "\n\n".join(format_exam_line(e) for e in exams)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def exam_schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = get_user_class_program(update.effective_user.id)
    if not program:
        buttons = _program_buttons("pick_class_program_exam")
        await query.edit_message_text(
            "اول ترمت رو انتخاب کن (فقط یه‌بار لازمه، بعداً هم می‌تونی عوضش کنی):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    await render_exam_list(query, program)


async def pick_class_program_for_exam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    program = _resolve_program_token(token)
    if not program:
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    set_user_class_program(update.effective_user.id, program)
    await render_exam_list(query, program)


async def change_class_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = _program_buttons("pick_class_program")
    await query.edit_message_text("ترم جدیدت رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))


# ============================================================
# 🔔 اطلاع‌رسانیِ چند-دسته‌ای
# ============================================================

async def toggle_notify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    key = query.data.split(":", 1)[1]
    user_id = update.effective_user.id
    prefs = get_notify_prefs(user_id)
    new_value = not prefs.get(key, False)
    set_notify_pref(user_id, key, new_value)
    await query.answer("✅ روشن شد" if new_value else "خاموش شد")
    await query.edit_message_text(
        build_notify_menu_text(user_id), reply_markup=build_notify_menu_markup(user_id)
    )


async def notify_menu_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("باشه ✅ هر وقت خواستی از «🔔 اطلاع‌رسانی» دوباره تنظیمشون کن.")


async def show_class_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, term_id_str, day = query.data.split(":", 2)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("show_class_day: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return

    schedule = load_class_schedule()
    classes_all = sorted(schedule.get(program, {}).get(day, []), key=lambda c: c["start"])
    classes, missing_groups = filter_class_entries_for_student(classes_all, update.effective_user.id)

    if not classes and not missing_groups:
        text = f"«{program}» روز {day} کلاسی ثبت نشده."
    else:
        lines = [f"📅 برنامه‌ی {day} — «{program}»:\n"]
        occurrence_date = next_date_for_fa_weekday(day)
        for c in classes:
            loc = f" — {c['location']}" if c.get("location") else ""
            pending = " — ⚠️ گروه هنوز مشخص نشده" if c.get("group_pending") else ""
            cancelled = bool(occurrence_date and is_class_cancelled_on(program, day, c, occurrence_date))
            prefix = "🚫" if cancelled else "⏰"
            cancelled_note = f" — لغو برای {format_gregorian_as_jalali(occurrence_date)}" if cancelled else ""
            lines.append(f"{prefix} {c['start']}–{c['end']} | {c['course']}{loc}{pending}{cancelled_note}")
        if missing_groups:
            lines.append("")
            for name in missing_groups:
                lines.append(f"⚠️ گروهت برای «{name}» مشخص نیست — از «👥 گروه‌های عملی من» انتخابش کن.")
        text = "\n".join(lines)

    buttons = []
    for c in classes:
        course_id = resolve_course_id_for_class_entry(program, c)
        if course_id is None:
            # درس توی دیتابیس پیدا نشد -- خودِ برنامه کلاسی همچنان نمایش داده
            # می‌شه، فقط دکمه‌ی منابع برای همین یه کلاس ساخته نمی‌شه.
            continue
        data = f"class_resource:{course_id}"
        if validate_callback_data(data, context_label=f"class_resource/program={program}/course={c['course']}"):
            buttons.append([InlineKeyboardButton(f"📚 منابع {c['course']}", callback_data=data)])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="class_schedule_menu")])
    await edit_message_with_keyboard_safe(query, text, buttons, context_label="show_class_day")


async def show_class_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📆 کل هفته یکجا -- به‌جای رفتن روز به روز، کلِ برنامه‌ی هفتگیِ ترمِ
    دانشجو (گروه‌آگاه، با احتسابِ مکان) رو تو یه پیام نشون می‌ده."""
    query = update.callback_query
    await query.answer()
    term_id_str = query.data.split(":", 1)[1]
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("show_class_week: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return

    schedule = load_class_schedule()
    lines = [f"📆 برنامه‌ی کاملِ هفته — «{program}»"]
    any_missing = set()
    has_any_class = False
    for day in DAYS_OF_WEEK:
        classes_all = sorted(schedule.get(program, {}).get(day, []), key=lambda c: c["start"])
        classes, missing = filter_class_entries_for_student(classes_all, update.effective_user.id)
        any_missing.update(missing)
        if not classes:
            continue
        has_any_class = True
        lines.append(f"\n🔸 {day}")
        occurrence_date = next_date_for_fa_weekday(day)
        for c in classes:
            loc = f" — {c['location']}" if c.get("location") else ""
            pending = " — ⚠️ گروه هنوز مشخص نشده" if c.get("group_pending") else ""
            cancelled = bool(occurrence_date and is_class_cancelled_on(program, day, c, occurrence_date))
            prefix = "🚫" if cancelled else "⏰"
            cancelled_note = f" — لغو برای {format_gregorian_as_jalali(occurrence_date)}" if cancelled else ""
            lines.append(f"  {prefix} {c['start']}–{c['end']} | {c['course']}{loc}{pending}{cancelled_note}")

    if not has_any_class:
        lines.append("\nهنوز هیچ کلاسی برای این ترم ثبت نشده.")
    if any_missing:
        lines.append("\n")
        for name in sorted(any_missing):
            lines.append(f"⚠️ گروهت برای «{name}» مشخص نیست — از «👥 گروه‌های عملی من» انتخابش کن.")

    buttons = [[InlineKeyboardButton("🔙 بازگشت", callback_data="class_schedule_menu")]]
    await edit_message_with_keyboard_safe(
        query, "\n".join(lines), buttons, context_label="show_class_week"
    )


# ============================================================
# برنامه‌ریزی -> برنامه کلاس‌ها (رسمی): ادمین
# ============================================================

def _cls_prog_buttons(allowed: list) -> list:
    """دکمه‌های انتخابِ ترم برای مدیریتِ برنامه‌کلاسی (ادمین) -- callback_data
    از term_id استفاده می‌کنه، نه اسمِ کاملِ ترم (نگاه کن به توضیحاتِ بالای
    validate_callback_data)."""
    buttons = []
    for p in allowed:
        term_id = term_id_for_program(p)
        if term_id is None:
            logger.error("_cls_prog_buttons: no term found for program=%r", p)
            continue
        data = f"cls_prog:{term_id}"
        if validate_callback_data(data, context_label=f"cls_prog/program={p}"):
            buttons.append([InlineKeyboardButton(p, callback_data=data)])
    return buttons


async def classschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = _cls_prog_buttons(allowed)
    await update.message.reply_text(
        "برنامه کلاسیِ کدوم ترم/برنامه رو مدیریت کنم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cls_back_to_progs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_class_entry", None)
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = _cls_prog_buttons(allowed)
    await edit_message_with_keyboard_safe(
        query, "برنامه کلاسیِ کدوم ترم/برنامه رو مدیریت کنم؟", buttons, context_label="cls_back_to_progs"
    )


async def cls_pick_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_class_entry", None)
    term_id_str = query.data.split(":", 1)[1]
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("cls_pick_day: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    buttons = []
    for day in DAYS_OF_WEEK:
        data = f"cls_day:{term_id}:{day}"
        if validate_callback_data(data, context_label=f"cls_day/program={program}"):
            buttons.append([InlineKeyboardButton(day, callback_data=data)])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="cls_back_progs")])
    await edit_message_with_keyboard_safe(query, f"کدوم روزِ «{program}»؟", buttons, context_label="cls_pick_day")


async def render_cls_day(query, program: str, day: str) -> None:
    term_id = term_id_for_program(program)
    if term_id is None:
        logger.error("render_cls_day: no term found for program=%r", program)

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    buttons = []
    for i, c in enumerate(classes):
        if term_id is None:
            continue
        data = f"cls_del:{term_id}:{day}:{c.get('id', i)}"
        extra_bits = []
        gid = c.get("group_id")
        if gid is not None:
            grp = dbmod.get_course_group_by_id(gid)
            if grp:
                extra_bits.append(f"گروه {grp['name']}")
        elif c.get("group_pending"):
            extra_bits.append("⚠️ گروه نامشخص")
        if c.get("location"):
            extra_bits.append(c["location"])
        exc_dates = get_class_exception_dates(program, day, c)
        if exc_dates:
            exc_dates_jalali = [format_gregorian_as_jalali(d) for d in exc_dates]
            extra_bits.append(f"🚫 لغوشده: {', '.join(exc_dates_jalali)}")
        suffix = f" ({'، '.join(extra_bits)})" if extra_bits else ""
        label = f"❌ {c['start']}-{c['end']} {c['course']}{suffix}"
        token = c.get("id", i)
        exc_data = f"cls_exc:{term_id}:{day}:{token}"
        group_data = f"cls_group:{term_id}:{day}:{token}"
        if validate_callback_data(data, context_label=f"cls_del/program={program}/day={day}/i={i}"):
            buttons.append([InlineKeyboardButton(label, callback_data=data)])
        quick_row = []
        if validate_callback_data(group_data, context_label=f"cls_group/program={program}/day={day}/i={i}"):
            group_label = "👥 گروه"
            quick_row.append(InlineKeyboardButton(group_label, callback_data=group_data))
        loc_data = f"cls_loc:{term_id}:{day}:{token}"
        if validate_callback_data(loc_data, context_label=f"cls_loc/program={program}/day={day}/i={i}"):
            loc_label = "📍 مکان" if not c.get("location") else "📍 تغییر مکان"
            quick_row.append(InlineKeyboardButton(loc_label, callback_data=loc_data))
        if quick_row:
            buttons.append(quick_row)
        if validate_callback_data(exc_data, context_label=f"cls_exc/program={program}/day={day}/i={i}"):
            cancel_row = []
            occurrence_date = next_date_for_fa_weekday(day)
            next_cancelled = bool(
                occurrence_date and is_class_cancelled_on(program, day, c, occurrence_date)
            )
            quick_exc_data = f"cls_exquick:{term_id}:{day}:{token}"
            if validate_callback_data(
                quick_exc_data,
                context_label=f"cls_exquick/program={program}/day={day}/i={i}",
            ):
                quick_label = "↩️ برگرداندن جلسه بعد" if next_cancelled else "🚫 لغو جلسه بعد"
                cancel_row.append(InlineKeyboardButton(quick_label, callback_data=quick_exc_data))
            cancel_row.append(InlineKeyboardButton("📅 تاریخ دیگر", callback_data=exc_data))
            buttons.append(cancel_row)
    if term_id is not None:
        add_data = f"cls_add:{term_id}:{day}"
        if validate_callback_data(add_data, context_label=f"cls_add/program={program}/day={day}"):
            buttons.append([InlineKeyboardButton("➕ افزودن کلاس", callback_data=add_data)])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"cls_prog:{term_id}")])
    else:
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="cls_back_progs")])
    text = f"برنامه‌ی {day} — «{program}»:" if classes else f"«{program}» روز {day} هنوز کلاسی نداره."
    await edit_message_with_keyboard_safe(query, text, buttons, context_label="render_cls_day")


async def cls_show_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_class_entry", None)
    _, term_id_str, day = query.data.split(":", 2)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("cls_show_day: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    await render_cls_day(query, program, day)


async def cls_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, term_id_str, day = query.data.split(":", 2)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("cls_add_start: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    context.user_data["pending_class_entry"] = {"program": program, "day": day}
    back_data = f"cls_day:{term_id}:{day}"
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=back_data)]])
    await query.edit_message_text(
        f"➕ افزودن کلاس برای «{day}»\n\n"
        "سریع‌ترین روش فقط این دو مورده:\n"
        "ساعت | نام درس\n\n"
        "مثال:\n"
        "08:00-10:00 | پاتولوژی\n\n"
        "بعد از ثبت، کنار همان کلاس دو دکمه‌ی «👥 گروه» و «📍 مکان» داری و لازم نیست "
        "گروه یا مکان را همین‌جا تایپ کنی.\n\n"
        "اگر خواستی، فرمت کامل قدیمی هم همچنان کار می‌کند:\n"
        "ساعت | نام درس | گروه | مکان\n"
        "مثال: 13:00-15:00 | انقلاب | پسر | سالن آمفی‌تئاتر\n\n"
        "می‌تونی چند کلاس رو هم هر کدوم در یک خط بفرستی.",
        reply_markup=back_btn,
    )


def _find_class_entry_index(classes: list, token: str):
    """شناسه‌ی پایدارِ کلاس را پیدا می‌کند؛ برای callbackهای قدیمی، index هم
    به‌عنوان fallback پشتیبانی می‌شود."""
    idx = next((i for i, c in enumerate(classes) if str(c.get("id")) == str(token)), None)
    if idx is None and str(token).isdigit() and int(token) < len(classes):
        idx = int(token)
    return idx


async def cls_group_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تعیین/تغییر گروهِ یک بلوکِ برنامه‌ی رسمی.

    حالت «نامشخص» مخصوص وقتی است که برنامه رسیده ولی گروه‌بندی هنوز اعلام نشده؛
    این بلوک می‌تواند موقتاً با بلوک‌های هم‌زمان کنار هم بماند و بعداً گروهش
    تعیین شود.
    """
    query = update.callback_query
    await query.answer()
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = _find_class_entry_index(classes, token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return
    entry = classes[idx]
    stable_token = str(entry.get("id", idx))
    course_id = resolve_course_id_for_class_entry(program, entry)
    groups = dbmod.get_course_groups(course_id) if course_id is not None else []
    current_gid = entry.get("group_id")

    buttons = []
    for g in groups:
        mark = "✅ " if g["id"] == current_gid else ""
        data = f"cls_setgrp:{term_id}:{day}:{stable_token}:{g['id']}"
        if validate_callback_data(data, context_label=f"cls_setgrp/group={g['id']}"):
            buttons.append([InlineKeyboardButton(f"{mark}👥 {g['name']}", callback_data=data)])

    pending_data = f"cls_setgrp:{term_id}:{day}:{stable_token}:p"
    general_data = f"cls_setgrp:{term_id}:{day}:{stable_token}:0"
    if validate_callback_data(pending_data, context_label="cls_setgrp/pending"):
        mark = "✅ " if entry.get("group_pending") and current_gid is None else ""
        buttons.append([InlineKeyboardButton(f"{mark}⏳ گروه هنوز مشخص نیست", callback_data=pending_data)])
    if validate_callback_data(general_data, context_label="cls_setgrp/general"):
        mark = "✅ " if current_gid is None and not entry.get("group_pending") else ""
        buttons.append([InlineKeyboardButton(f"{mark}🌐 بدون گروه (عمومی)", callback_data=general_data)])

    if course_id is not None:
        quick_group_data = f"cls_qgrp:{term_id}:{day}:{stable_token}"
        if validate_callback_data(quick_group_data, context_label="cls_group/quick_create"):
            buttons.append([InlineKeyboardButton("➕ ساخت گروه با ساعت همین کلاس", callback_data=quick_group_data)])
        buttons.append([InlineKeyboardButton("⚙️ مدیریت کامل گروه‌های این درس", callback_data=f"crs_groups:{course_id}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"cls_day:{term_id}:{day}")])

    current_text = "عمومی"
    if current_gid is not None:
        grp = dbmod.get_course_group_by_id(current_gid)
        current_text = grp["name"] if grp else "گروهِ حذف‌شده"
    elif entry.get("group_pending"):
        current_text = "هنوز مشخص نشده"

    no_groups_note = ""
    if course_id is None:
        no_groups_note = "\n\n⚠️ این درس در دیتابیس پیدا نشد؛ فعلاً فقط حالت نامشخص/عمومی قابل انتخاب است."
    elif not groups:
        no_groups_note = "\n\nℹ️ هنوز گروهی برای این درس تعریف نشده؛ هر وقت گروه‌ها مشخص شدند از «مدیریت گروه‌های این درس» بسازشان و بعد همین‌جا وصلشان کن."

    await query.edit_message_text(
        f"👥 گروهِ کلاس «{entry['course']}»\n"
        f"{day}، {entry['start']}-{entry['end']}\n"
        f"وضعیت فعلی: {current_text}\n\n"
        "⏳ «گروه هنوز مشخص نیست» اجازه می‌ده کلاس‌های هم‌زمان فعلاً ثبت بمونن؛ "
        "تا زمان تعیین گروه، این کلاس در برنامه برای همه دیده می‌شه ولی یادآوریِ خودکار براش ارسال نمی‌شه.\n"
        "🌐 «عمومی» یعنی کلاس واقعاً برای همه است و نباید با هیچ کلاس هم‌زمان دیگری تداخل داشته باشه."
        f"{no_groups_note}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cls_set_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, term_id_str, day, token, target = query.data.split(":", 4)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        await query.answer("این ترم دیگه پیدا نشد.", show_alert=True)
        return
    if not can_manage_term(update.effective_user.id, program):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = _find_class_entry_index(classes, token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return
    entry = classes[idx]
    course_id = resolve_course_id_for_class_entry(program, entry)

    overlaps = [
        other for j, other in enumerate(classes)
        if j != idx and _ranges_overlap(entry["start"], entry["end"], other["start"], other["end"])
    ]

    if target == "p":
        entry.pop("group_id", None)
        entry["group_pending"] = True
        status_text = "گروه هنوز مشخص نیست"
    elif target == "0":
        # کلاس عمومی برای همه است، پس با هر بلوک هم‌زمان دیگری مخاطب مشترک دارد.
        if overlaps:
            other = overlaps[0]
            await query.answer(
                f"نمی‌شه عمومی کرد؛ با «{other['course']}» {other['start']}-{other['end']} هم‌زمانه.",
                show_alert=True,
            )
            return
        entry.pop("group_id", None)
        entry.pop("group_pending", None)
        status_text = "عمومی"
    else:
        if course_id is None:
            await query.answer("این درس در دیتابیس پیدا نشد؛ اول اسم درس را بررسی کن.", show_alert=True)
            return
        try:
            group_id = int(target)
        except ValueError:
            await query.answer("گروه نامعتبره.", show_alert=True)
            return
        group = dbmod.get_course_group_by_id(group_id)
        if group is None or group["course_id"] != course_id:
            await query.answer("این گروه برای این درس معتبر نیست.", show_alert=True)
            return

        conflict = None
        for other in overlaps:
            other_gid = other.get("group_id")
            if other_gid == group_id:
                conflict = other
                break
            if other_gid is None and not other.get("group_pending"):
                conflict = other
                break
        if conflict is not None:
            await query.answer(
                f"این گروه با «{conflict['course']}» {conflict['start']}-{conflict['end']} تداخل داره.",
                show_alert=True,
            )
            return

        entry["group_id"] = group_id
        entry.pop("group_pending", None)
        # اگر برای درس گروه ساخته شده ولی flag آن قبلاً خاموش بوده، با اتصالِ
        # واقعیِ کلاس به گروه، گروه‌بندی را فعال نگه می‌داریم تا دانشجو بتواند
        # گروه خودش را انتخاب کند.
        dbmod.set_has_group_split(course_id, True)
        status_text = f"گروه {group['name']}"

    save_class_schedule(schedule)
    dbmod.log_action(
        update.effective_user.id,
        "SET_CLASS_GROUP",
        term=program,
        detail=f"{day} {entry['course']} {entry['start']}-{entry['end']} -> {status_text}",
    )
    await query.answer(f"ثبت شد: {status_text} ✅")
    await notify_program_schedule_change(
        context,
        program,
        f"👥 وضعیت گروهِ کلاس «{entry['course']}» ({day} {entry['start']}-{entry['end']}) شد: {status_text}.",
    )
    await render_cls_day(query, program, day)



async def cls_location_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تنظیم سریع مکان یک کلاس؛ بدون نیاز به ثبت دوباره‌ی کلاس."""
    query = update.callback_query
    await query.answer()
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = _find_class_entry_index(classes, token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return
    entry = classes[idx]
    stable_token = str(entry.get("id", idx))
    current = entry.get("location") or "ثبت نشده"

    buttons = [
        [InlineKeyboardButton("✏️ تعیین / تغییر مکان", callback_data=f"cls_locedit:{term_id}:{day}:{stable_token}")],
    ]
    if entry.get("location"):
        buttons.append([InlineKeyboardButton("🗑 حذف مکان", callback_data=f"cls_locclear:{term_id}:{day}:{stable_token}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"cls_day:{term_id}:{day}")])

    await query.edit_message_text(
        f"📍 مکان کلاس «{entry['course']}»\n"
        f"{day}، {entry['start']}-{entry['end']}\n\n"
        f"مکان فعلی: {current}\n\n"
        "برای تغییر، فقط اسم مکان را بفرست؛ مثلاً «کلاس ۲۰۳» یا «آمفی‌تئاتر».",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cls_location_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None or not can_manage_term(update.effective_user.id, program):
        await query.answer("دسترسی نامعتبره.", show_alert=True)
        return

    clear_admin_flow_state(context)
    context.user_data["pending_class_location"] = {
        "term_id": term_id,
        "program": program,
        "day": day,
        "token": token,
    }
    await query.edit_message_text(
        "📍 فقط اسم مکان رو بفرست.\n"
        "مثال: کلاس ۲۰۳\n\n"
        "همین؛ لازم نیست فرمت خاصی بنویسی.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 انصراف", callback_data=f"cls_day:{term_id}:{day}")]]
        ),
    )


async def cls_location_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None or not can_manage_term(update.effective_user.id, program):
        await query.answer("دسترسی نامعتبره.", show_alert=True)
        return

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = _find_class_entry_index(classes, token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return
    entry = classes[idx]
    entry.pop("location", None)
    save_class_schedule(schedule)
    await query.answer("مکان حذف شد ✅")
    dbmod.log_action(
        update.effective_user.id,
        "CLEAR_CLASS_LOCATION",
        term=program,
        detail=f"{day} {entry['course']} {entry['start']}-{entry['end']}",
    )
    await render_cls_day(query, program, day)


async def cls_quick_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """گروه را با روز و ساعت همین کلاس می‌سازد؛ ادمین فقط اسم گروه را تایپ می‌کند."""
    query = update.callback_query
    await query.answer()
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None or not can_manage_term(update.effective_user.id, program):
        await query.answer("دسترسی نامعتبره.", show_alert=True)
        return

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = _find_class_entry_index(classes, token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return
    entry = classes[idx]
    course_id = resolve_course_id_for_class_entry(program, entry)
    if course_id is None:
        await query.answer("اسم این درس در دیتابیس پیدا نشد.", show_alert=True)
        return

    clear_admin_flow_state(context)
    context.user_data["pending_quick_course_group"] = {
        "term_id": term_id,
        "program": program,
        "day": day,
        "token": str(entry.get("id", idx)),
        "course_id": course_id,
        "start": entry["start"],
        "end": entry["end"],
    }
    await query.edit_message_text(
        f"➕ ساخت گروه برای «{entry['course']}»\n"
        f"{day}، {entry['start']}-{entry['end']}\n\n"
        "فقط اسم گروه رو بفرست.\n"
        "مثال: گروه ۱\n\n"
        "روز و ساعت رو خود بات از همین کلاس برمی‌داره و گروه هم بلافاصله به همین کلاس وصل می‌شه.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 انصراف", callback_data=f"cls_group:{term_id}:{day}:{entry.get('id', idx)}")]]
        ),
    )


async def cls_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        await query.answer("این ترم دیگه پیدا نشد.", show_alert=True)
        return
    if not can_manage_term(update.effective_user.id, program):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = next((i for i, c in enumerate(classes) if c.get("id") == token), None)
    if idx is None and token.isdigit() and int(token) < len(classes):
        idx = int(token)
    if idx is not None:
        removed = classes.pop(idx)
        save_class_schedule(schedule)
        dbmod.log_action(update.effective_user.id, "DELETE_CLASS_SCHEDULE", term=program, detail=f"{day} {removed['course']}")
        await query.answer(f"«{removed['course']}» حذف شد")
        await notify_program_schedule_change(
            context, program, f"❌ کلاسِ «{removed['course']}» ({day} {removed['start']}-{removed['end']}) حذف شد."
        )
    else:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
    await render_cls_day(query, program, day)



async def cls_exception_quick_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لغو/برگرداندن نزدیک‌ترین جلسه‌ی این کلاس با یک کلیک."""
    query = update.callback_query
    await query.answer()
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None

    program = program_for_term_id(term_id)
    if program is None:
        await query.answer("این ترم دیگه پیدا نشد.", show_alert=True)
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return

    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = _find_class_entry_index(classes, token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return

    entry = classes[idx]
    date_str = next_date_for_fa_weekday(day)
    if not date_str:
        await query.answer("تاریخ جلسه بعد پیدا نشد.", show_alert=True)
        return
    display_date = format_gregorian_as_jalali(date_str)

    already = is_class_cancelled_on(program, day, entry, date_str)
    if already:
        remove_class_exception(program, day, entry, date_str)
        alert = f"لغو جلسه {display_date} برداشته شد ✅"
        change_text = (
            f"↩️ «{entry['course']}» ({day} {entry['start']}-{entry['end']}) "
            f"در تاریخ {display_date} دوباره برگزار می‌شه."
        )
        action_detail = "برگشت"
    else:
        add_class_exception(program, day, entry, date_str)
        alert = f"جلسه {display_date} لغو شد ✅"
        change_text = (
            f"🚫 «{entry['course']}» ({day} {entry['start']}-{entry['end']}) "
            f"در تاریخ {display_date} لغو شد."
        )
        action_detail = "لغو"

    dbmod.log_action(
        update.effective_user.id,
        "TOGGLE_CLASS_EXCEPTION_QUICK",
        term=program,
        detail=f"{day} {entry['course']} {date_str} -> {action_detail}",
    )
    await notify_program_schedule_change(context, program, change_text)
    await query.answer(alert, show_alert=True)
    await render_cls_day(query, program, day)


async def cls_exception_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🚫 لغوِ یه‌روزِ یه بلوکِ کلاسی -- بدونِ حذفِ خودِ بلوکِ تکرارشونده (فاز ۱۴).
    ادمین تاریخِ میلادی (YYYY-MM-DD) رو می‌فرسته؛ اگه همون تاریخ قبلاً لغو بوده،
    این‌بار برعکسش می‌کنه (رفتارِ toggle، برای اینکه نیازی به UI جدا برای
    برگردوندنِ لغو نباشه)."""
    query = update.callback_query
    await query.answer()
    _, term_id_str, day, token = query.data.split(":", 3)
    try:
        term_id = int(term_id_str)
    except ValueError:
        term_id = None
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("cls_exception_start: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    schedule = load_class_schedule()
    classes = schedule.get(program, {}).get(day, [])
    idx = next((i for i, c in enumerate(classes) if c.get("id") == token), None)
    if idx is None and token.isdigit() and int(token) < len(classes):
        idx = int(token)
    if idx is None:
        await query.answer("این کلاس دیگه وجود نداره.", show_alert=True)
        return
    entry = classes[idx]
    clear_admin_flow_state(context)
    context.user_data["pending_class_exception"] = {"program": program, "day": day, "class_id": entry.get("id"), "idx": idx}
    exc_dates = get_class_exception_dates(program, day, entry)
    exc_dates_jalali = [format_gregorian_as_jalali(d) for d in exc_dates]
    existing_note = f"\nتاریخ‌های الان‌لغوشده: {', '.join(exc_dates_jalali)}" if exc_dates_jalali else ""
    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"cls_day:{term_id}:{day}")]]
    )
    await query.edit_message_text(
        f"📅 لغو برای تاریخ دلخواه\n\n"
        f"کلاس: «{entry['course']}» — {day} {entry['start']}-{entry['end']}\n\n"
        "فقط تاریخ رو بفرست؛ شمسی یا میلادی هر دو قبوله.\n"
        "مثال: 1405/07/15\n\n"
        "اگر همون تاریخ از قبل لغو شده باشه، با فرستادن دوباره لغوش برداشته می‌شه."
        f"{existing_note}",
        reply_markup=back_btn,
    )


# ============================================================
# ادمین: برنامه امتحانات رسمی (هر ترم مستقل، فقط ادمین همون ترم)
# ============================================================

async def examschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/examschedule -- لیست/حذف امتحان‌های یک ترم."""
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"exam_admin_prog:{p}")] for p in allowed]
    await update.message.reply_text(
        "برنامه امتحاناتِ کدوم ترم رو مدیریت کنم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def render_exam_admin_list(query, program: str) -> None:
    exams = dbmod.list_exams(program)
    buttons = []
    for e in exams:
        type_prefix = EXAM_TYPE_LABELS.get(e.get("exam_type"), "")
        label = f"❌ {type_prefix + ' ' if type_prefix else ''}{e['course']} — {e['exam_date']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"exam_admin_del:{program}:{e['id']}")])
    buttons.append([InlineKeyboardButton("➕ افزودن امتحان", callback_data=f"exam_admin_add:{program}")])
    term_id = term_id_for_program(program)
    if term_id is not None:
        buttons.append([InlineKeyboardButton("🗓 بازه‌های زمانیِ ترم", callback_data=f"termperiods:{term_id}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="examschedule_root")])
    text = f"📝 امتحانات «{program}»" + ("" if exams else " (هنوز چیزی ثبت نشده)") + "\nروی امتحانی که می‌خوای حذف کنی بزن:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


_TERM_PERIOD_LABELS = {
    "class_period_start": "شروعِ بازه‌ی کلاسی",
    "class_period_end": "پایانِ بازه‌ی کلاسی",
    "final_exam_start": "شروعِ بازه‌ی پایان‌ترم",
    "final_exam_end": "پایانِ بازه‌ی پایان‌ترم",
}


def _term_periods_text_markup(term_id: int, program: str):
    periods = dbmod.get_term_periods(program) or {}
    lines = [f"🗓 بازه‌های زمانیِ «{program}»", ""]
    buttons = []
    for field, label in _TERM_PERIOD_LABELS.items():
        value = periods.get(field) or "— ثبت نشده —"
        lines.append(f"{label}: {value}")
        buttons.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"termperiod_edit:{term_id}:{field}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"exam_admin_prog:{program}")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def term_periods_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🗓 بازه‌های زمانیِ ترم (فاز ۱۵) -- «بازه‌ی کلاسی» و «بازه‌ی پایان‌ترم»،
    کاملاً مستقل از هم (ممکنه هم‌پوشانی داشته باشن، مثلاً میان‌ترم‌ها وقتی
    گرفته می‌شن که کلاس‌ها هنوز ادامه دارن). میان‌ترم‌ها بازه‌ی جداگانه
    نمی‌گیرن -- هرکدوم صرفاً یه ردیفِ دستی تو برنامه‌ی امتحانات با نوعِ «میان‌ترم»."""
    query = update.callback_query
    await query.answer()
    term_id = int(query.data.split(":", 1)[1])
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("term_periods_menu: no term found for term_id=%r", term_id)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    await query.edit_message_text(*_term_periods_text_markup(term_id, program))


async def term_period_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, term_id_str, field = query.data.split(":", 2)
    term_id = int(term_id_str)
    program = program_for_term_id(term_id)
    if program is None:
        logger.error("term_period_edit_start: no term found for term_id=%r", term_id_str)
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    if field not in _TERM_PERIOD_LABELS:
        await query.edit_message_text("فیلدِ نامعتبر.")
        return
    clear_admin_flow_state(context)
    context.user_data["pending_term_period"] = {"term_id": term_id, "field": field}
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"termperiods:{term_id}")]])
    await query.edit_message_text(
        f"مقدارِ جدیدِ «{_TERM_PERIOD_LABELS[field]}» رو بفرست (متنِ آزاد، مثلاً یه تاریخِ شمسی مثلِ «27 دی»).\n"
        "برای پاک‌کردنِ مقدارِ فعلی، فقط یه خط‌تیره (-) بفرست.",
        reply_markup=back_btn,
    )


async def exam_admin_pick_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_exam_entry", None)
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    await render_exam_admin_list(query, program)


async def exam_admin_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = [[InlineKeyboardButton(p, callback_data=f"exam_admin_prog:{p}")] for p in allowed]
    await query.edit_message_text(
        "برنامه امتحاناتِ کدوم ترم رو مدیریت کنم؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def exam_admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    program = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, program):
        await deny_term_access(query, program)
        return
    context.user_data["pending_exam_entry"] = {"term": program}
    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"exam_admin_prog:{program}")]]
    )
    await query.edit_message_text(
        f"باشه، امتحان جدید برای «{program}» رو به این فرم بفرست:\n"
        "نام‌درس | تاریخ | ساعت | مکان(اختیاری)\n"
        "مثال: پاتولوژی | ۱۴۰۴/۰۴/۲۰ | ۱۰:۰۰ | سالن امتحانات ۱",
        reply_markup=back_btn,
    )


async def exam_type_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بعد از پارس‌شدنِ فرمِ افزودنِ امتحان، ادمین این‌جا مشخص می‌کنه میان‌ترمه یا
    پایان‌ترم -- و فقط همین‌جا واقعاً تو دیتابیس ذخیره می‌شه."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    pending = context.user_data.pop("pending_exam_type_choice", None)
    if pending is None:
        await query.edit_message_text("این درخواست منقضی شده؛ دوباره از «➕ افزودن امتحان» شروع کن.")
        return
    program = pending["term"]
    if not can_manage_term(update.effective_user.id, program):
        await query.edit_message_text(f"⛔️ شما دسترسی مدیریت «{program}» رو ندارید.")
        return
    exam_type = None if choice == "none" else choice
    dbmod.add_exam(program, pending["course"], pending["exam_date"], pending["exam_time"], pending["location"], exam_type=exam_type)
    dbmod.log_action(
        update.effective_user.id, "ADD_EXAM", term=program,
        detail=f"{pending['course']} {pending['exam_date']} ({exam_type or 'نامشخص'})",
    )
    warn = ""
    if not course_exists(program, pending["course"]):
        warn = "\n⚠️ این اسم درس توی دروسِ ثبت‌شده پیدا نشد؛ فقط برای نمایش استفاده می‌شه."
    type_label = EXAM_TYPE_LABELS.get(exam_type, "")
    await query.edit_message_text(
        f"امتحان «{pending['course']}» ({pending['exam_date']}) {type_label} برای «{program}» ثبت شد ✅{warn}"
    )


async def exam_admin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, program, exam_id_str = query.data.split(":", 2)
    if not can_manage_term(update.effective_user.id, program):
        await query.answer("⛔️ دسترسی ندارید", show_alert=True)
        return
    exam = dbmod.get_exam(int(exam_id_str))
    if dbmod.delete_exam(int(exam_id_str)):
        dbmod.log_action(
            update.effective_user.id, "DELETE_EXAM", term=program,
            detail=exam["course"] if exam else exam_id_str,
        )
        await query.answer("حذف شد ✅")
    else:
        await query.answer("قبلاً حذف شده بود.")
    await render_exam_admin_list(query, program)


# ============================================================
# برنامه‌ریزی -> برنامه مطالعه‌ی شخصی
# ============================================================

async def render_personal_day_picker(query, user_id: int) -> None:
    schedule = load_personal_schedule().get(str(user_id), {})
    buttons = []
    for day in DAYS_OF_WEEK:
        n = len(schedule.get(day, []))
        label = f"{day} ({n})" if n else day
        buttons.append([InlineKeyboardButton(label, callback_data=f"pstudy_day:{day}")])
    buttons.append([InlineKeyboardButton("➕ افزودن بلوک مطالعه", callback_data="pstudy_add_start")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="schedule_root")])
    await query.edit_message_text(
        "📖 برنامه مطالعه‌ی شخصی‌ات — کدوم روز؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def personal_schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await render_personal_day_picker(query, update.effective_user.id)


async def render_personal_day(query, user_id: int, day: str) -> None:
    schedule = load_personal_schedule()
    blocks = schedule.get(str(user_id), {}).get(day, [])
    buttons = [
        [InlineKeyboardButton(f"❌ {b['start']}-{b['end']} {b['course']}", callback_data=f"pstudy_del:{day}:{b['id']}")]
        for b in blocks
    ]
    buttons.append([InlineKeyboardButton("➕ افزودن بلوک مطالعه", callback_data=f"pstudy_add_day:{day}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="personal_schedule_menu")])
    text = f"📖 برنامه‌ی {day}:" if blocks else f"هنوز چیزی برای {day} ثبت نکردی."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def show_personal_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    day = query.data.split(":", 1)[1]
    await render_personal_day(query, update.effective_user.id, day)


async def pstudy_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [[InlineKeyboardButton(day, callback_data=f"pstudy_add_day:{day}")] for day in DAYS_OF_WEEK]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="personal_schedule_menu")])
    await query.edit_message_text(
        "برای کدوم روز می‌خوای بلوک مطالعه اضافه کنی؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def pstudy_add_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    day = query.data.split(":", 1)[1]
    context.user_data["pstudy_new"] = {"day": day}
    await query.edit_message_text(f"باشه، برای «{day}» ساعت شروع و پایان مطالعه رو بفرست.\nمثال: 18:00-19:30")


async def pstudy_pick_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    program = _resolve_program_token(token)
    if not program:
        await query.edit_message_text("این ترم دیگه پیدا نشد.")
        return
    pstudy_new = context.user_data.get("pstudy_new")
    if not pstudy_new:
        await query.edit_message_text("یه مشکلی پیش اومد، دوباره از «➕ افزودن بلوک مطالعه» شروع کن.")
        return
    courses = dbmod.get_courses_with_ids(program)
    if not courses:
        await query.edit_message_text(f"«{program}» درسی نداره.")
        return
    pstudy_new["program"] = program
    context.user_data["pstudy_new"] = pstudy_new
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"pstudy_course:{c['id']}")] for c in courses]
    await query.edit_message_text(f"کدوم درسِ «{program}»؟", reply_markup=InlineKeyboardMarkup(buttons))


async def pstudy_pick_course(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    pstudy_new = context.user_data.pop("pstudy_new", None)
    if not pstudy_new or "program" not in pstudy_new:
        await query.edit_message_text("یه مشکلی پیش اومد، دوباره از «➕ افزودن بلوک مطالعه» شروع کن.")
        return
    course_name = token
    if token.isdigit():
        course = dbmod.get_course_by_id(int(token))
        if course is None or course["term_name"] != pstudy_new["program"]:
            await query.edit_message_text("این درس دیگه پیدا نشد؛ دوباره از افزودن بلوک شروع کن.")
            return
        course_name = course["name"]

    user_id = update.effective_user.id
    schedule = load_personal_schedule()
    day = pstudy_new["day"]
    start, end = pstudy_new["start"], pstudy_new["end"]
    conflicts = []
    for b in schedule.get(str(user_id), {}).get(day, []):
        if _ranges_overlap(start, end, b["start"], b["end"]):
            conflicts.append(f"مطالعه‌ی «{b.get('course', '')}» {b['start']}-{b['end']}")

    official_all = load_class_schedule().get(pstudy_new["program"], {}).get(day, [])
    official, _missing = filter_class_entries_for_student(official_all, user_id)
    for c in official:
        if _ranges_overlap(start, end, c["start"], c["end"]):
            conflicts.append(f"کلاس «{c['course']}» {c['start']}-{c['end']}")

    for e in get_fixed_events_for_day(day, pstudy_new["program"]):
        if _ranges_overlap(start, end, e["start"], e["end"]):
            conflicts.append(f"رویداد «{e['name']}» {e['start']}-{e['end']}")

    if conflicts:
        await query.edit_message_text(
            "⛔️ این بلوک با برنامه‌ات تداخل داره:\n• " + "\n• ".join(conflicts) +
            "\n\nیه بازه‌ی دیگه انتخاب کن و دوباره از «➕ افزودن بلوک مطالعه» شروع کن."
        )
        return

    schedule.setdefault(str(user_id), {}).setdefault(day, []).append(
        {
            "id": uuid.uuid4().hex[:12],
            "start": pstudy_new["start"],
            "end": pstudy_new["end"],
            "program": pstudy_new["program"],
            "course": course_name,
        }
    )
    save_personal_schedule(schedule)

    await query.edit_message_text(
        f"✅ ثبت شد: {pstudy_new['day']} {pstudy_new['start']}–{pstudy_new['end']} → {course_name}"
    )


async def pstudy_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, day, token = query.data.split(":", 2)
    user_id = update.effective_user.id
    schedule = load_personal_schedule()
    blocks = schedule.get(str(user_id), {}).get(day, [])
    idx = next((i for i, b in enumerate(blocks) if b.get("id") == token), None)
    # سازگاری با دکمه‌های قدیمی که index ذخیره می‌کردند.
    if idx is None and token.isdigit() and int(token) < len(blocks):
        idx = int(token)
    if idx is not None:
        removed = blocks.pop(idx)
        save_personal_schedule(schedule)
        await query.answer(f"«{removed['course']}» حذف شد")
    else:
        await query.answer("این بلوک دیگه وجود نداره.", show_alert=True)
    await render_personal_day(query, user_id, day)


# ============================================================
# برنامه‌ریزی -> یادآوری‌ها (کلاس رسمی و بلوک مطالعه‌ی شخصی)
# ============================================================

async def notify_program_schedule_change(context: ContextTypes.DEFAULT_TYPE, program: str, message: str) -> None:
    """به دانشجویانی که برای «schedule_change» تو همون ترم اطلاع‌رسانی رو
    روشن کردن، خبرِ تغییرِ برنامه‌ی کلاسی رو می‌فرسته (فاز ۱۴). خطای ارسال به
    تکِ‌تکِ کاربرها (بلاک‌کردنِ بات و غیره) نباید جلوی بقیه رو بگیره."""
    user_ids = dbmod.get_users_for_notify("schedule_change", class_program=program)
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=f"🔔 تغییر در برنامه‌ی کلاسیِ «{program}»\n{message}")
        except Exception:
            pass


async def send_class_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, program: str, c: dict) -> bool:
    buttons = []
    course_id = resolve_course_id_for_class_entry(program, c)
    if course_id is not None:
        data = f"class_resource:{course_id}"
        if validate_callback_data(data, context_label=f"send_class_reminder/program={program}"):
            buttons.append([InlineKeyboardButton("📚 ورود به منابع", callback_data=data)])
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⏰ یادآوری کلاس رسمی\n«{c['course']}» ساعت {c['start']} شروع می‌شه ({program})",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )
        return True
    except Exception as e:
        logger.warning("send_class_reminder failed user=%s course=%r: %s", user_id, c.get("course"), e)
        return False


async def send_fixed_event_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, event: dict) -> bool:
    event_type = normalize_fixed_event_type(event.get("type", "general"))
    if event_type == "food":
        text = "رزرو غذا یادت نره 🍱"
    elif event_type == "cultural":
        text = "یادت نره امروز دوشنبه فرهنگیه 🌱"
    else:
        text = f"⏰ یادآوری\n«{event['name']}» ساعت {event['start']} شروع می‌شه"
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        return True
    except Exception as e:
        logger.warning("send_fixed_event_reminder failed user=%s event=%r: %s", user_id, event.get("name"), e)
        return False


async def send_personal_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, day: str, idx: int, b: dict) -> bool:
    buttons = []
    course_id = dbmod.get_course_id_by_name(b["program"], b["course"])
    if course_id is not None:
        data = f"class_resource:{course_id}"
        if validate_callback_data(data, context_label="send_personal_reminder"):
            buttons.append([InlineKeyboardButton("📚 ورود به منابع", callback_data=data)])
    buttons.append(
        [InlineKeyboardButton("⏰ ۱۵ دقیقه بعد یادآوری کن", callback_data=f"pstudy_snooze:{user_id}:{day}:{b.get('id', idx)}")]
    )
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⏰ وقت مطالعه‌ست!\n«{b['course']}» ({b['start']} تا {b['end']})",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return True
    except Exception as e:
        logger.warning("send_personal_reminder failed user=%s: %s", user_id, e)
        return False


async def pstudy_snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, uid_str, day, token = query.data.split(":", 3)
    # کاربر نباید بتواند callback یک نفر دیگر را دستکاری و برای او Job بسازد.
    if int(uid_str) != update.effective_user.id:
        await query.answer("این دکمه برای شما نیست.", show_alert=True)
        return
    schedule = load_personal_schedule()
    blocks = schedule.get(uid_str, {}).get(day, [])
    idx = next((i for i, b in enumerate(blocks) if b.get("id") == token), None)
    if idx is None and token.isdigit() and int(token) < len(blocks):
        idx = int(token)
    if idx is None or context.job_queue is None:
        await query.answer("این بلوک دیگه وجود نداره.", show_alert=True)
        return
    await query.answer("باشه، ۱۵ دقیقه‌ی دیگه دوباره یادآوری می‌کنم ⏰")
    context.job_queue.run_once(
        snooze_fire, when=15 * 60, data={"user_id": int(uid_str), "day": day, "idx": idx, "block": dict(blocks[idx])}
    )


async def snooze_fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    await send_personal_reminder(context, d["user_id"], d["day"], d["idx"], d["block"])


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """یادآوری‌ها را با پنجره‌ی catch-up بررسی می‌کند؛ فقط ارسال موفق لاگ می‌شود."""
    now = now_tehran()
    today_fa = PY_WEEKDAY_TO_FA[now.weekday()]
    today_date_str = now.strftime("%Y-%m-%d")
    now_minute = now.hour * 60 + now.minute

    log = load_reminder_log()
    if log.get("date") != today_date_str:
        log = {"date": today_date_str, "sent": [], "last_minute": max(-1, now_minute - 2)}
    sent = set(log.get("sent", []))
    last_minute = log.get("last_minute")
    if not isinstance(last_minute, int) or last_minute > now_minute:
        last_minute = max(-1, now_minute - 2)
    lower_bound = max(last_minute, now_minute - REMINDER_CATCHUP_MINUTES - 1)

    def due(time_str: str) -> bool:
        try:
            target = _time_to_minutes(time_str)
        except Exception:
            return False
        return lower_bound < target <= now_minute

    # --- کلاس‌های رسمی: ابتدا برای هر دانشجو بر اساس گروه خودش فیلتر می‌شوند ---
    class_schedule = load_class_schedule()
    users = load_users()
    for uid_str, info in users.items():
        program = info.get("class_program")
        if not program:
            continue
        # هر دانشجو می‌تونه یادآوریِ کلاس رو از «🔔 اطلاع‌رسانی» خاموش کنه
        # (پیش‌فرض روشنه؛ فقط همین حلقه‌ی کلاس‌های رسمی رو تحت‌تأثیر قرار می‌ده).
        if not (info.get("notify_prefs") or {}).get("class_reminder", True):
            continue
        uid = int(uid_str)
        all_classes = class_schedule.get(program, {}).get(today_fa, [])
        classes, _missing = filter_class_entries_for_student(all_classes, uid)
        for c in classes:
            # وقتی مخاطبِ کلاس هنوز معلوم نیست، Reminder نفرستیم تا همه‌ی
            # دانشجوها بابت چند کلاسِ هم‌زمانِ احتمالی نوتیف اشتباه نگیرن.
            if c.get("group_pending"):
                continue
            if is_class_cancelled_on(program, today_fa, c, today_date_str):
                continue
            reminder_time = minutes_before(c["start"], REMINDER_LEAD_MINUTES)
            reminder_id = f"class:{uid_str}:{c.get('id') or _class_entry_key(c)}:{today_date_str}"
            if due(reminder_time) and reminder_id not in sent:
                if await send_class_reminder(context, uid, program, c):
                    sent.add(reminder_id)

    # --- برنامه مطالعه‌ی شخصی ---
    personal = load_personal_schedule()
    for uid_str, days in personal.items():
        blocks = days.get(today_fa, [])
        for idx, b in enumerate(blocks):
            reminder_id = f"pstudy:{uid_str}:{b.get('id', idx)}:{today_date_str}"
            if due(b["start"]) and reminder_id not in sent:
                if await send_personal_reminder(context, int(uid_str), today_fa, idx, b):
                    sent.add(reminder_id)

    # --- رویدادهای ثابت هفتگی ---
    todays_events = [e for e in load_fixed_events() if e["day"] == today_fa]
    if todays_events:
        for uid_str, info in users.items():
            program = info.get("class_program")
            for e_idx, e in enumerate(todays_events):
                if e["scope"] != "all" and e["scope"] != program:
                    continue
                reminder_time = minutes_before(e["start"], e.get("lead_minutes", 15))
                reminder_id = f"fixedevent:{uid_str}:{e.get('id', e_idx)}:{today_date_str}"
                if due(reminder_time) and reminder_id not in sent:
                    if await send_fixed_event_reminder(context, int(uid_str), e):
                        sent.add(reminder_id)

    log["sent"] = sorted(sent)
    log["last_minute"] = now_minute
    save_reminder_log(log)


# ============================================================
# ادمین: رویدادهای ثابتِ هفتگی (فاز ۱۶، مثلِ «دوشنبه فرهنگی») -- /fixedevents
# ============================================================

def _fixedevents_text_markup(user_id: int):
    events = load_fixed_events()
    allowed = set(admin_allowed_programs(user_id))
    is_super = is_super_admin(user_id)
    lines = ["🗓 رویدادهای ثابتِ هفتگی:"]
    buttons = []
    if not events:
        lines.append("هنوز چیزی ثبت نشده.")
    for i, e in enumerate(events):
        scope_label = "🌍 همه" if e["scope"] == "all" else e["scope"]
        lines.append(
            f"• {e['name']} — {e['day']} {e['start']}-{e['end']} "
            f"({scope_label}, یادآوری {e.get('lead_minutes', 15)} دقیقه قبل)"
        )
        can_delete = is_super or e["scope"] in allowed
        if can_delete:
            buttons.append([InlineKeyboardButton(f"🗑 حذفِ «{e['name']}»", callback_data=f"fixedevent_del:{e.get('id', i)}")])
    buttons.append([InlineKeyboardButton("➕ افزودنِ رویداد", callback_data="fixedevent_add")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def fixedevents_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    text, markup = _fixedevents_text_markup(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=markup)


async def fixedevent_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    allowed = admin_allowed_programs(update.effective_user.id)
    buttons = []
    if is_super_admin(update.effective_user.id):
        buttons.append([InlineKeyboardButton("🌍 برای همه‌ی ترم‌ها", callback_data="fixedevent_scope:all")])
    for p in allowed:
        buttons.append([InlineKeyboardButton(p, callback_data=f"fixedevent_scope:{p}")])
    if not buttons:
        await query.edit_message_text("شما دسترسیِ مدیریتِ هیچ ترمی رو ندارید.")
        return
    await query.edit_message_text(
        "این رویداد مخصوصِ کدوم ترمه؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def fixedevent_scope_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    scope = query.data.split(":", 1)[1]
    allowed = admin_allowed_programs(update.effective_user.id)
    if scope != "all" and scope not in allowed:
        await deny_term_access(query, scope)
        return
    if scope == "all" and not is_super_admin(update.effective_user.id):
        await query.edit_message_text("⛔️ فقط سوپرادمین می‌تونه رویدادِ «برای همه» بسازه.")
        return
    clear_admin_flow_state(context)
    context.user_data["pending_fixed_event"] = {"scope": scope}
    await query.edit_message_text(
        "فرمت جدید: نوع | نام | روز | ساعتِ‌شروع-ساعتِ‌پایان | دقیقه‌ی یادآوریِ‌قبلش(اختیاری)\n"
        "نوع‌ها: عمومی / غذا / فرهنگی\n"
        "مثال: فرهنگی | دوشنبه فرهنگی | دوشنبه | 10:00-12:00 | 60\n"
        "مثال: غذا | رزرو غذا | شنبه | 12:00-13:00 | 30\n\n"
        "فرمت قدیمیِ «نام | روز | ساعت | دقیقه» هم همچنان پذیرفته می‌شه."
    )


async def fixedevent_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    token = query.data.split(":", 1)[1]
    events = load_fixed_events()
    idx = next((i for i, e in enumerate(events) if e.get("id") == token), None)
    if idx is None and token.isdigit() and int(token) < len(events):
        idx = int(token)
    if idx is None:
        await query.answer("قبلاً حذف شده.", show_alert=True)
        text, markup = _fixedevents_text_markup(update.effective_user.id)
        await query.edit_message_text(text, reply_markup=markup)
        return
    e = events[idx]
    allowed = set(admin_allowed_programs(update.effective_user.id))
    if not (is_super_admin(update.effective_user.id) or e["scope"] in allowed):
        await query.answer("⛔️ اجازه‌ی حذفِ این رویداد رو نداری.", show_alert=True)
        return
    events.pop(idx)
    save_fixed_events(events)
    dbmod.log_action(update.effective_user.id, "DELETE_FIXED_EVENT", detail=e["name"])
    await query.answer("حذف شد ✅")
    text, markup = _fixedevents_text_markup(update.effective_user.id)
    await query.edit_message_text(text, reply_markup=markup)


# ============================================================
# 💬 گپ دانشجویی -- دانشجو: ورود به اتاق و ارسال پیام
# ============================================================

async def chat_exam_root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    groups = dbmod.get_groups_with_terms()
    back_button = InlineKeyboardButton("🔙 بازگشت", callback_data="chat_root_back")
    if len(groups) <= 1:
        buttons = [[InlineKeyboardButton(p, callback_data=f"chat_exam_term:{p}")] for p in get_program_names()]
        buttons.append([back_button])
        await query.edit_message_text("📚 گپ امتحانی — کدوم ترم/برنامه؟", reply_markup=InlineKeyboardMarkup(buttons))
        return
    buttons = [[InlineKeyboardButton(g, callback_data=f"chat_exam_grp:{g}")] for g, _t in groups]
    buttons.append([back_button])
    await query.edit_message_text("📚 گپ امتحانی — کدوم مقطع/گروه؟", reply_markup=InlineKeyboardMarkup(buttons))


async def chat_root_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton("📚 گپ امتحانی", callback_data="chat_exam_root")],
        [InlineKeyboardButton("☕ گپ دوستانه", callback_data="chat_casual_start")],
    ]
    await query.edit_message_text("💬 گپ دانشجویی\nیکی رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(buttons))


async def chat_exam_group_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_name = query.data.split(":", 1)[1]
    terms = dbmod.get_terms_in_group(group_name)
    back_button = InlineKeyboardButton("🔙 بازگشت", callback_data="chat_exam_root")
    buttons = [[InlineKeyboardButton(t, callback_data=f"chat_exam_term:{t}")] for t in terms]
    buttons.append([back_button])
    await query.edit_message_text(f"{group_name} — کدوم ترم؟", reply_markup=InlineKeyboardMarkup(buttons))


async def chat_exam_term_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    term_name = query.data.split(":", 1)[1]
    courses = dbmod.get_courses_with_ids(term_name)
    back_button = InlineKeyboardButton("🔙 بازگشت", callback_data="chat_exam_root")
    if not courses:
        await query.edit_message_text(f"«{term_name}» هنوز درسی نداره.", reply_markup=InlineKeyboardMarkup([[back_button]]))
        return
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"chat_exam_course:{c['id']}")] for c in courses]
    buttons.append([back_button])
    await query.edit_message_text(f"گپ امتحانی «{term_name}» — کدوم درس؟", reply_markup=InlineKeyboardMarkup(buttons))


async def chat_exam_course_enter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    buttons = [
        [
            InlineKeyboardButton("🕶 ورود ناشناس", callback_data=f"chatanon:exam:{course_id}:yes"),
            InlineKeyboardButton("👤 ورود با لقبم", callback_data=f"chatanon:exam:{course_id}:no"),
        ]
    ]
    await query.edit_message_text(
        "چطور وارد گپ بشی؟\n\n"
        "🕶 ناشناس: هیچ‌کس (حتی ادمین ترم) هویتت رو نمی‌بینه.\n"
        "👤 با لقب: بقیه لقبت رو کنار پیامت می‌بینن.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def chat_casual_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    buttons = [
        [
            InlineKeyboardButton("🕶 ورود ناشناس", callback_data="chatanon:casual:0:yes"),
            InlineKeyboardButton("👤 ورود با لقبم", callback_data="chatanon:casual:0:no"),
        ]
    ]
    await query.edit_message_text(
        "☕ گپ دوستانه — فضای آزادِ غیردرسی برای همه‌ی دانشجوهای MedVerse.\n\nچطور وارد بشی؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def chatanon_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, chat_type, course_id_str, choice = query.data.split(":", 3)
    course_id = int(course_id_str) if chat_type == "exam" else None
    anonymous = choice == "yes"
    user_id = update.effective_user.id

    if dbmod.is_user_restricted(user_id, course_id):
        await query.edit_message_text("⛔️ دسترسیت به گپ محدود شده. برای پیگیری با ادمین ترم در تماس باش.")
        return

    room = {"chat_type": chat_type, "course_id": course_id, "anonymous": anonymous}
    if chat_type == "exam":
        course = dbmod.get_course_by_id(course_id)
        if course is None:
            await query.edit_message_text("این درس دیگه وجود نداره.")
            return
        period = dbmod.get_open_exam_period(course_id)
        room["course_name"] = course["name"]
        room["term_name"] = course["term_name"]
        room["exam_period_id"] = period["id"]
        phase_label = "📚 آمادگی امتحان" if period["phase"] == "prep" else "🩺 تجربه امتحان"
        room_title = f"💬 گپ امتحانی | {course['term_name']} | {course['name']}\n({phase_label})"
    else:
        room["exam_period_id"] = None
        room_title = "☕ گپ دوستانه"

    dbmod.join_chat_room(chat_type, course_id, user_id, anonymous=anonymous)
    context.user_data["active_chat_room"] = room

    mode_label = "🕶 ناشناس" if anonymous else "👤 با لقب"
    await query.edit_message_text(f"{room_title}\n\nوارد شدی ({mode_label}). پیام‌هات رو بفرست 👇")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"برای خروج، دکمه‌ی «{CHAT_EXIT_BUTTON}» رو بزن.",
        reply_markup=chat_room_keyboard(),
    )


async def chat_exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("active_chat_room", None)
    context.user_data.pop("chat_edit_pending", None)
    context.user_data.pop("poll_new", None)
    await update.message.reply_text("از گپ خارج شدی.", reply_markup=main_menu_keyboard())


async def chat_identity_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """جابه‌جایی بین «ناشناس» و «با لقب» در حین حضور در اتاق -- بدون خروج از گپ.
    فقط روی پیام‌های بعدی اثر می‌ذاره؛ پیام‌هایی که قبلاً فرستاده شده عوض نمی‌شن."""
    room = context.user_data.get("active_chat_room")
    if not room:
        return
    room["anonymous"] = not room["anonymous"]
    context.user_data["active_chat_room"] = room
    dbmod.set_chat_identity_mode(room["chat_type"], room.get("course_id"), update.effective_user.id, room["anonymous"])
    mode_label = "🕶 ناشناس" if room["anonymous"] else "👤 با لقب"
    await update.message.reply_text(
        f"هویتت عوض شد؛ از این به بعد پیام‌هات به‌صورت «{mode_label}» فرستاده می‌شه.",
        reply_markup=chat_room_keyboard(),
    )


def _chat_reply_quote(reply_to_id: int) -> str:
    """اگه پیامِ اصلیِ ریپلای‌شده هنوز موجود/حذف‌نشده باشه، یه هدرِ نقل‌قول کوتاه
    می‌سازه؛ وگرنه رشته‌ی خالی برمی‌گردونه (پیام جدید بدونِ هدر فرستاده می‌شه)."""
    original = dbmod.get_chat_message(reply_to_id)
    if not original or original["is_deleted"]:
        return ""
    snippet = original["text"].replace("\n", " ").strip()
    if len(snippet) > CHAT_REPLY_SNIPPET_LEN:
        snippet = snippet[:CHAT_REPLY_SNIPPET_LEN].rstrip() + "…"
    return f"🧵 پاسخ به:\n{original['display_snapshot']}: {snippet}\n─────────\n"


def _chat_message_body(msg: dict) -> str:
    """متنِ کاملِ یک پیام (با هدرِ ریپلای در صورتِ وجود) -- برای هم ارسالِ اولیه و
    هم بازسازی بعد از ویرایش استفاده می‌شه تا فرمت همیشه یکی باشه."""
    quote = _chat_reply_quote(msg["reply_to_id"]) if msg.get("reply_to_id") else ""
    return f"{quote}{msg['display_snapshot']}\n{msg['text']}"


def _chat_message_markup(message_id: int, counts: dict = None, extra_rows: list = None) -> InlineKeyboardMarkup:
    counts = counts or dbmod.get_reaction_counts(message_id)
    reaction_row = [
        InlineKeyboardButton(f"{emoji} {counts.get(emoji, 0)}", callback_data=f"chatreact:{message_id}:{emoji}")
        for emoji in dbmod.CHAT_REACTION_EMOJIS
    ]
    report_row = [InlineKeyboardButton("🚩 گزارش", callback_data=f"chatreport:{message_id}")]
    rows = list(extra_rows or []) + [reaction_row, report_row]
    return InlineKeyboardMarkup(rows)


def _chat_own_controls_markup(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"chatmsgedit:{message_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"chatmsgdel:{message_id}"),
        ]]
    )


async def _post_chat_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    room: dict,
    user_id: int,
    anonymous: bool,
    display: str,
    text: str,
    reply_to_id: int = None,
    extra_markup_rows: list = None,
    send_controls: bool = True,
    include_poster_copy: bool = False,
) -> int:
    """هسته‌ی مشترکِ ارسالِ هر پیام به یه اتاق گپ -- چه پیامِ معمولیِ کاربر باشه، چه
    موضوعِ تصادفی، چه اعلانِ یه نظرسنجی/دو‌راهی. یه ردیف توی chat_messages می‌سازه،
    برای همه‌ی اعضا فن‌اوت می‌کنه، فن‌اوت‌ها رو برای ویرایش/حذف/ریپلای بعدی ثبت
    می‌کنه، و در صورت نیاز کنترل‌های ویرایش/حذف رو به فرستنده برمی‌گردونه.
    include_poster_copy=True یعنی خودِ فرستنده هم یه کپیِ فن‌اوت‌شده (با دکمه‌های
    ریکشن/رأی) می‌گیره -- برای موضوعِ تصادفی/نظرسنجی/دوراهی لازمه چون محتوا رو
    خودِ کاربر ننوشته و نباید فقط بقیه ببیننش؛ برای پیامِ معمولیِ تایپ‌شده لازم
    نیست چون کاربر خودش متنش رو توی همون چت می‌بینه.
    خروجی: message_id (شناسه‌ی chat_messages) که فراخوان می‌تونه برای ساختِ
    نظرسنجی/دوراهیِ مرتبط باهاش نگه داره."""
    course_id = room.get("course_id")
    message_id = dbmod.add_chat_message(
        room["chat_type"], course_id, room.get("exam_period_id"), user_id, anonymous, display, text,
        reply_to_id=reply_to_id,
    )

    outgoing = _chat_message_body(dbmod.get_chat_message(message_id))
    markup = _chat_message_markup(message_id, extra_rows=extra_markup_rows)
    exclude_id = None if include_poster_copy else user_id
    member_ids = dbmod.get_chat_room_members(room["chat_type"], course_id, exclude_user_id=exclude_id)
    for member_id in member_ids:
        try:
            sent = await context.bot.send_message(chat_id=member_id, text=outgoing, reply_markup=markup)
            dbmod.record_chat_delivery(message_id, member_id, sent.message_id)
        except Exception:
            pass

    if send_controls:
        try:
            await update.message.reply_text("برای این پیام:", reply_markup=_chat_own_controls_markup(message_id))
        except Exception:
            pass

    current_member_ids = set(member_ids) | {user_id}
    asyncio.create_task(_maybe_ping_chat_activity(context, room, current_member_ids))

    if room["chat_type"] == "exam":
        asyncio.create_task(_extract_and_store_topics(message_id, text))

    return message_id


async def handle_chat_room_text(update: Update, context: ContextTypes.DEFAULT_TYPE, room: dict) -> None:
    user_id = update.effective_user.id
    course_id = room.get("course_id")
    if dbmod.is_user_restricted(user_id, course_id):
        context.user_data.pop("active_chat_room", None)
        await update.message.reply_text(
            "⛔️ دسترسیت به این گپ محدود شده و از اتاق خارج شدی.", reply_markup=main_menu_keyboard()
        )
        return

    # اگه همین لحظه توی این اتاق یه بازیِ Mafia در جریانه، بازیکنانِ حذف‌شده حق
    # صحبت ندارن و توی فازِ شب/رأی‌گیری کلاً گپ ساکته (نگاه کن به mafia.chat_block_reason).
    block_reason = mafia.chat_block_reason(room["chat_type"], course_id, user_id)
    if block_reason:
        await update.message.reply_text(block_reason)
        return

    text = update.message.text.strip()
    if not text:
        return
    text = text[:2000]

    # اگه کاربر با Reply تلگرامی به یکی از کپی‌های ربات جواب داده، از روی جدولِ
    # فن‌اوت پیدا می‌کنیم اصل پیام کدومه (نگاه کن به db.get_message_id_by_delivery).
    reply_to_id = None
    incoming_reply = update.message.reply_to_message
    if incoming_reply:
        reply_to_id = dbmod.get_message_id_by_delivery(user_id, incoming_reply.message_id)

    anonymous = room["anonymous"]
    if anonymous:
        display = "🕶 دانشجوی ناشناس"
    else:
        display = f"🦋 {get_display_name(user_id, update.effective_user.first_name)}"

    await _post_chat_message(update, context, room, user_id, anonymous, display, text, reply_to_id=reply_to_id)


# ---------- موضوع تصادفی ----------

async def chat_random_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = context.user_data.get("active_chat_room")
    if not room:
        return
    user_id = update.effective_user.id
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await update.message.reply_text("⛔️ دسترسیت به این گپ محدود شده.")
        return
    topic = random.choice(CHAT_RANDOM_TOPICS)
    text = f"🎲 موضوع تصادفی برای بحث:\n{topic}"
    # مثلِ پیام‌های ناشناس رفتار می‌کنیم (is_anonymous=1) چون این پیشنهادِ رباته، نه
    # حرفِ شخصیِ کاربر -- هویتِ کسی که دکمه رو زده نباید توی گپ نمایش داده بشه.
    await _post_chat_message(
        update, context, room, user_id, True, "🎲 موضوع تصادفی", text,
        send_controls=False, include_poster_copy=True,
    )


# ---------- نظرسنجی ----------

def _poll_vote_buttons(poll: dict) -> list:
    return [
        [InlineKeyboardButton(opt, callback_data=f"chatpollvote:{poll['id']}:{idx}")]
        for idx, opt in enumerate(poll["options"])
    ]


def _extra_markup_rows_for_chat_message(message_id: int) -> list:
    """اگه این پیام اعلانِ یه نظرسنجیِ بازه، ردیف‌های دکمه‌ی رأی رو برمی‌گردونه؛
    وگرنه [] (نه هیچ ردیفِ اضافه‌ای)."""
    poll = dbmod.get_poll_by_chat_message(message_id)
    if not poll or poll["closed_at"]:
        return []
    return _poll_vote_buttons(poll)


def _render_poll_body(poll: dict) -> str:
    results = dbmod.get_poll_results(poll["id"])
    total = sum(results.values())
    title = "🤔 دو راهی سخت" if poll["kind"] == "would_you_rather" else "📊 نظرسنجی"
    lines = [f"{title}: {poll['question']}", ""]
    for idx, opt in enumerate(poll["options"]):
        n = results.get(idx, 0)
        pct = round((n / total) * 100) if total else 0
        filled = round(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(f"{opt}\n{bar} {pct}% ({n} رای)")
        lines.append("")
    if poll["closed_at"]:
        lines.append("🔒 این نظرسنجی بسته شده")
    return "\n".join(lines).strip()


async def _refresh_poll_message(context: ContextTypes.DEFAULT_TYPE, poll_id: int) -> None:
    poll = dbmod.get_chat_poll(poll_id)
    if not poll or not poll["chat_message_id"]:
        return
    body = _render_poll_body(poll)
    extra_rows = [] if poll["closed_at"] else _poll_vote_buttons(poll)
    markup = _chat_message_markup(poll["chat_message_id"], extra_rows=extra_rows)
    for recipient_id, telegram_message_id in dbmod.get_deliveries_for_message(poll["chat_message_id"]):
        try:
            await context.bot.edit_message_text(
                chat_id=recipient_id, message_id=telegram_message_id, text=body, reply_markup=markup
            )
        except Exception:
            pass


def _poll_close_controls_markup(poll_id: int, closed: bool) -> InlineKeyboardMarkup:
    if closed:
        return InlineKeyboardMarkup([])
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔒 بستن نظرسنجی", callback_data=f"chatpollclose:{poll_id}")]]
    )


async def _launch_chat_poll(update: Update, context: ContextTypes.DEFAULT_TYPE, room: dict, user_id: int,
                             question: str, options: list, kind: str = "poll") -> None:
    poll_id = dbmod.create_chat_poll(room["chat_type"], room.get("course_id"), user_id, question, options, kind)
    poll = dbmod.get_chat_poll(poll_id)
    body = _render_poll_body(poll)
    if kind == "would_you_rather":
        # مثلِ موضوع تصادفی، این یه پیشنهادِ رباته؛ هویتِ کسی که دکمه رو زده نمایش
        # داده نمی‌شه.
        anonymous, display = True, "🤔 دو راهی سخت"
    else:
        anonymous = room["anonymous"]
        display = "🕶 دانشجوی ناشناس" if anonymous else f"🦋 {get_display_name(user_id, update.effective_user.first_name)}"
    message_id = await _post_chat_message(
        update, context, room, user_id, anonymous, display, body,
        extra_markup_rows=_poll_vote_buttons(poll), send_controls=False, include_poster_copy=True,
    )
    dbmod.set_poll_chat_message(poll_id, message_id)
    if kind == "poll":
        try:
            await update.message.reply_text(
                "نظرسنجی ایجاد شد.", reply_markup=_poll_close_controls_markup(poll_id, False)
            )
        except Exception:
            pass


async def chat_poll_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    room = context.user_data.get("active_chat_room")
    if not room:
        return
    user_id = update.effective_user.id
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await update.message.reply_text("⛔️ دسترسیت به این گپ محدود شده.")
        return
    context.user_data["poll_new"] = {"stage": "question"}
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="chatpollnewcancel")]])
    await update.message.reply_text("📊 سوالِ نظرسنجی رو بنویس:", reply_markup=cancel_markup)


async def handle_chat_poll_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict) -> None:
    room = context.user_data.get("active_chat_room")
    if not room:
        context.user_data.pop("poll_new", None)
        return
    text = update.message.text.strip()
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف", callback_data="chatpollnewcancel")]])

    if state["stage"] == "question":
        if not text:
            await update.message.reply_text("سوال نمی‌تونه خالی باشه؛ دوباره بنویس:", reply_markup=cancel_markup)
            return
        state["question"] = text[:300]
        state["stage"] = "options"
        context.user_data["poll_new"] = state
        await update.message.reply_text(
            "گزینه‌ها رو هرکدوم توی یه خط جدا بنویس (حداقل ۲، حداکثر ۶):", reply_markup=cancel_markup
        )
        return

    if state["stage"] == "options":
        options = [o.strip()[:60] for o in text.split("\n") if o.strip()]
        if len(options) < 2:
            await update.message.reply_text(
                "حداقل ۲ گزینه لازمه؛ هرکدوم توی یه خط بنویس:", reply_markup=cancel_markup
            )
            return
        options = options[:6]
        context.user_data.pop("poll_new", None)
        preview = "\n".join(f"▫️ {o}" for o in options)
        confirm_markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ ایجاد نظرسنجی", callback_data="chatpollnewconfirm"),
                InlineKeyboardButton("❌ انصراف", callback_data="chatpollnewcancel"),
            ]]
        )
        context.user_data["poll_pending"] = {"question": state["question"], "options": options}
        await update.message.reply_text(
            f"📊 {state['question']}\n\n{preview}\n\nایجاد بشه؟", reply_markup=confirm_markup
        )


async def chat_poll_new_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.pop("poll_new", None)
    context.user_data.pop("poll_pending", None)
    await query.answer()
    await query.edit_message_text("ساختِ نظرسنجی لغو شد.")


async def chat_poll_new_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    pending = context.user_data.pop("poll_pending", None)
    room = context.user_data.get("active_chat_room")
    if not pending or not room:
        await query.answer("این درخواست منقضی شده.", show_alert=True)
        return
    user_id = update.effective_user.id
    if dbmod.is_user_restricted(user_id, room.get("course_id")):
        await query.answer("دسترسیت به این گپ محدود شده.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("✅ نظرسنجی ساخته شد و برای اعضای اتاق فرستاده شد.")
    await _launch_chat_poll(update, context, room, user_id, pending["question"], pending["options"])


async def chat_poll_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, poll_id_str, option_str = query.data.split(":", 2)
    poll_id, option_index = int(poll_id_str), int(option_str)
    user_id = update.effective_user.id

    poll = dbmod.get_chat_poll(poll_id)
    if not poll:
        await query.answer("این نظرسنجی دیگه وجود نداره.", show_alert=True)
        return
    if poll["closed_at"]:
        await query.answer("این نظرسنجی بسته شده.", show_alert=True)
        return
    if dbmod.is_user_restricted(user_id, poll["course_id"]):
        await query.answer("دسترسیت به این گپ محدود شده.", show_alert=True)
        return

    dbmod.vote_chat_poll(poll_id, user_id, option_index)
    await query.answer("رأیت ثبت شد ✅")
    asyncio.create_task(_refresh_poll_message(context, poll_id))


async def chat_poll_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    poll_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id

    ok = dbmod.close_chat_poll(poll_id, user_id)
    if not ok:
        await query.answer("این نظرسنجی مالِ تو نیست یا قبلاً بسته شده.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("🔒 نظرسنجی بسته شد.")
    asyncio.create_task(_refresh_poll_message(context, poll_id))


async def chat_react_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, message_id_str, emoji = query.data.split(":", 2)
    message_id = int(message_id_str)
    user_id = update.effective_user.id

    msg = dbmod.get_chat_message(message_id)
    if not msg or msg["is_deleted"]:
        await query.answer("این پیام دیگه وجود نداره.", show_alert=True)
        return
    if dbmod.is_user_restricted(user_id, msg["course_id"]):
        await query.answer("دسترسیت به این گپ محدود شده.", show_alert=True)
        return

    counts = dbmod.toggle_chat_reaction(message_id, user_id, emoji)
    await query.answer()
    asyncio.create_task(_propagate_chat_markup(context, message_id, counts))


async def _propagate_chat_markup(context: ContextTypes.DEFAULT_TYPE, message_id: int, counts: dict) -> None:
    """تغییرِ شمارشِ ریکشن رو روی همه‌ی کپی‌های فرستاده‌شده‌ی این پیام به‌روزرسانی می‌کنه
    (نگاه کن به db.chat_message_deliveries). اگه این پیام درواقع اعلانِ یه نظرسنجیِ
    بازه، دکمه‌های رأی رو هم حفظ می‌کنه (وگرنه با ریکشن زدن پاک می‌شدن)."""
    markup = _chat_message_markup(message_id, counts, extra_rows=_extra_markup_rows_for_chat_message(message_id))
    for recipient_id, telegram_message_id in dbmod.get_deliveries_for_message(message_id):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=recipient_id, message_id=telegram_message_id, reply_markup=markup
            )
        except Exception:
            pass


async def chat_msg_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id
    msg = dbmod.get_chat_message(message_id)
    if not msg or msg["is_deleted"] or msg["user_id"] != user_id:
        await query.answer("این پیام مالِ تو نیست یا دیگه وجود نداره.", show_alert=True)
        return
    await query.answer()
    context.user_data["chat_edit_pending"] = message_id
    cancel_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ انصراف از ویرایش", callback_data=f"chatmsgeditcancel:{message_id}")]]
    )
    await query.edit_message_text("✏️ متنِ جدیدِ پیام رو بفرست:", reply_markup=cancel_markup)


async def chat_msg_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message_id = int(query.data.split(":", 1)[1])
    await query.answer()
    if context.user_data.get("chat_edit_pending") == message_id:
        context.user_data.pop("chat_edit_pending", None)
    await query.edit_message_text("برای این پیام:", reply_markup=_chat_own_controls_markup(message_id))


async def handle_chat_msg_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int) -> None:
    context.user_data.pop("chat_edit_pending", None)
    user_id = update.effective_user.id
    new_text = update.message.text.strip()
    if not new_text:
        await update.message.reply_text("متن نمی‌تونه خالی باشه؛ ویرایش لغو شد.")
        return
    new_text = new_text[:2000]

    ok = dbmod.edit_own_chat_message(message_id, user_id, new_text)
    if not ok:
        await update.message.reply_text("⛔️ این پیام دیگه مالِ تو نیست یا حذف شده؛ نشد ویرایشش کنم.")
        return

    await update.message.reply_text("✏️ ویرایش شد.")
    updated_body = _chat_message_body(dbmod.get_chat_message(message_id))
    markup = _chat_message_markup(message_id)
    for recipient_id, telegram_message_id in dbmod.get_deliveries_for_message(message_id):
        try:
            await context.bot.edit_message_text(
                chat_id=recipient_id, message_id=telegram_message_id, text=updated_body, reply_markup=markup
            )
        except Exception:
            pass


async def chat_msg_delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id
    msg = dbmod.get_chat_message(message_id)
    if not msg or msg["is_deleted"] or msg["user_id"] != user_id:
        await query.answer("این پیام مالِ تو نیست یا دیگه وجود نداره.", show_alert=True)
        return
    await query.answer()
    confirm_markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ حذف بشه", callback_data=f"chatmsgdelconfirm:{message_id}"),
            InlineKeyboardButton("❌ انصراف", callback_data=f"chatmsgdelcancel:{message_id}"),
        ]]
    )
    await query.edit_message_text("مطمئنی این پیام حذف بشه؟", reply_markup=confirm_markup)


async def chat_msg_delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message_id = int(query.data.split(":", 1)[1])
    await query.answer()
    await query.edit_message_text("برای این پیام:", reply_markup=_chat_own_controls_markup(message_id))


async def chat_msg_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    message_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id

    ok = dbmod.delete_own_chat_message(message_id, user_id)
    if not ok:
        await query.answer("این پیام دیگه مالِ تو نیست یا حذف شده.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("🗑 پیام حذف شد.")
    for recipient_id, telegram_message_id in dbmod.get_deliveries_for_message(message_id):
        try:
            await context.bot.delete_message(chat_id=recipient_id, message_id=telegram_message_id)
        except Exception:
            try:
                await context.bot.edit_message_text(
                    chat_id=recipient_id, message_id=telegram_message_id, text="🗑 این پیام حذف شد."
                )
            except Exception:
                pass


# پینگِ نرمِ «این گپ در جریانه» -- برای هر اتاق (نوعِ گپ + درس/کورس)، حداکثر هر
# CHAT_ACTIVITY_PING_COOLDOWN_MINUTES دقیقه یه‌بار می‌فرستیم، نه به‌ازای هر پیام؛
# وگرنه توی یه گپِ پرحرف، کاربرهایی که فقط اطلاع‌رسانیِ «در جریان بودن» رو روشن
# دارن (نه خودِ گپ رو) اسپم می‌شن. این حافظه‌ی in-memory با ری‌استارتِ ربات ریست
# می‌شه که مشکلی نیست (فقط یعنی بعد از هر روشن‌شدنِ ربات، یه پینگِ دیگه هم می‌ره).
_LAST_ACTIVITY_PING: dict = {}
CHAT_ACTIVITY_PING_COOLDOWN_MINUTES = 20


async def _maybe_ping_chat_activity(context: ContextTypes.DEFAULT_TYPE, room: dict, current_member_ids: set) -> None:
    chat_type = room["chat_type"]
    course_id = room.get("course_id")
    room_key = (chat_type, course_id)
    now = datetime.now()
    last = _LAST_ACTIVITY_PING.get(room_key)
    if last is not None and (now - last).total_seconds() < CHAT_ACTIVITY_PING_COOLDOWN_MINUTES * 60:
        return
    _LAST_ACTIVITY_PING[room_key] = now

    if chat_type == "casual":
        recipients = dbmod.get_users_for_notify("casual_chat")
        ping_text = (
            "☕ یه گپ دوستانه همین الان در جریانه!\n"
            f"برای پیوستن: {MENU_CHAT} → ☕ گپ دوستانه"
        )
    else:
        term_name = room.get("term_name")
        course_name = room.get("course_name")
        recipients = dbmod.get_users_for_notify("exam_chat", class_program=term_name)
        ping_text = (
            f"📚 یه گپ امتحانی برای «{course_name}» ({term_name}) همین الان در جریانه!\n"
            f"برای پیوستن: {MENU_CHAT} → 📚 گپ امتحانی"
        )

    for uid in recipients:
        if uid in current_member_ids:
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=ping_text, disable_notification=True)
        except Exception:
            pass


async def _extract_and_store_topics(message_id: int, text: str) -> None:
    try:
        topics = await ai_experience.extract_topics(text)
        if topics:
            dbmod.add_chat_topics(message_id, topics)
    except ai_experience.AIConfigError:
        pass
    except Exception:
        logger.warning("استخراج موضوع با AI برای پیام %s شکست خورد.", message_id, exc_info=True)


# ============================================================
# 💬 گپ دانشجویی -- گزارش پیام
# ============================================================

async def chat_report_pick_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    message_id = query.data.split(":", 1)[1]
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"chatreportreason:{message_id}:{code}")]
        for code, label in CHAT_REPORT_REASONS
    ]
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text="دلیل گزارش چیه؟", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def chat_report_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, message_id_str, code = query.data.split(":", 2)
    message_id = int(message_id_str)
    reason_label = dict(CHAT_REPORT_REASONS).get(code, code)
    msg = dbmod.get_chat_message(message_id)
    if not msg:
        await query.edit_message_text("این پیام دیگه وجود نداره.")
        return
    dbmod.report_chat_message(message_id, update.effective_user.id, reason_label)
    await query.edit_message_text("🚩 گزارش ثبت شد، ممنون. ادمین بررسیش می‌کنه.")

    # اطلاع به ادمین‌های مجاز: برای گپ امتحانی، ادمین‌های همون ترم + سوپرادمین‌ها؛
    # برای گپ دوستانه فقط سوپرادمین‌ها (طبق spec، گپ دوستانه رو فقط سوپرادمین مدیریت می‌کنه).
    if msg["course_id"]:
        course = dbmod.get_course_by_id(msg["course_id"])
        recipients = dbmod.get_admins_for_term(course["term_name"]) if course else get_super_admin_ids()
    else:
        recipients = get_super_admin_ids()
    for admin_id in recipients:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🚩 گزارش جدید در گپ دانشجویی\nدلیل: {reason_label}\nبرای بررسی: /chatadmin",
            )
        except Exception:
            pass


# ============================================================
# 💬 گپ دانشجویی -- ادمین: بررسی گزارش‌ها، محدودیت، ساخت پیش‌نویس تجربه
# ============================================================

async def chatadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    terms = admin_allowed_programs(user_id)
    buttons = [[InlineKeyboardButton(t, callback_data=f"chatadmin_term:{t}")] for t in terms]
    if is_super_admin(user_id):
        buttons.append([InlineKeyboardButton("☕ گپ دوستانه", callback_data="chatadmin_casual")])
    await update.message.reply_text("💬 مدیریت گپ دانشجویی — کدوم ترم؟", reply_markup=InlineKeyboardMarkup(buttons))


async def chatadmin_term_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    term_name = query.data.split(":", 1)[1]
    if not can_manage_term(update.effective_user.id, term_name):
        await deny_term_access(query, term_name)
        return
    courses = dbmod.get_courses_with_ids(term_name)
    if not courses:
        await query.edit_message_text(f"«{term_name}» هنوز درسی نداره.")
        return
    buttons = [[InlineKeyboardButton(c["name"], callback_data=f"chatadmin_course:{c['id']}")] for c in courses]
    await query.edit_message_text(f"گپ‌های «{term_name}» — کدوم درس؟", reply_markup=InlineKeyboardMarkup(buttons))


async def chatadmin_course_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    buttons = [
        [InlineKeyboardButton("📨 گزارش‌های باز", callback_data=f"chatadmin_reports:{course_id}")],
        [InlineKeyboardButton("🧠 ساخت پیش‌نویس تجربه (AI)", callback_data=f"chatadmin_build:{course_id}")],
        [InlineKeyboardButton("🗂 پیش‌نویس‌های در انتظار تأیید", callback_data=f"chatadmin_drafts:{course_id}")],
        [InlineKeyboardButton("🔒 بستن دوره‌ی فعلی و شروع دوره‌ی جدید", callback_data=f"chatadmin_closeperiod:{course_id}")],
    ]
    await query.edit_message_text(
        f"💬 گپ امتحانی «{course['term_name']} - {course['name']}»", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def chatadmin_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    reports = dbmod.get_reports_for_admin(course_id=course_id, status="open")
    if not reports:
        await query.edit_message_text("گزارش باز جدیدی نیست ✅")
        return
    super_admin = is_super_admin(update.effective_user.id)
    await query.edit_message_text(f"📨 {len(reports)} گزارش باز:")
    for r in reports:
        # هویت فرستنده‌ی پیام‌های ناشناس اینجا هرگز نمایش داده نمی‌شه (dbmod.get_reports_for_admin
        # این کار رو خودش تضمین می‌کنه)؛ حتی برای سوپرادمین -- کشف هویت فقط با دکمه‌ی جدا و لاگ‌شده.
        sender_line = "دانشجوی ناشناس 🕶" if r["is_anonymous"] else r["display_snapshot"]
        text = f"«{r['text']}»\n\nفرستنده: {sender_line}\nدلیل گزارش: {r['reason']}"
        buttons = [
            [
                InlineKeyboardButton("🗑 حذف پیام", callback_data=f"chatadmin_delmsg:{r['message_id']}:{course_id}"),
                InlineKeyboardButton("🔨 محدود کردن فرستنده", callback_data=f"chatadmin_restrict:{r['message_id']}:{course_id}"),
            ],
            [InlineKeyboardButton("✅ بررسی شد", callback_data=f"chatadmin_resolve:{r['report_id']}:{course_id}")],
        ]
        if super_admin and r["is_anonymous"]:
            buttons.append(
                [InlineKeyboardButton("👑 مشاهده هویت (فقط سوپرادمین)", callback_data=f"chatadmin_reveal:{r['message_id']}")]
            )
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(buttons)
        )


async def chatadmin_delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, message_id_str, course_id_str = query.data.split(":", 2)
    course = dbmod.get_course_by_id(int(course_id_str))
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    dbmod.soft_delete_chat_message(int(message_id_str), update.effective_user.id)
    dbmod.log_action(update.effective_user.id, "DELETE_CHAT_MESSAGE", term=course["term_name"], detail=message_id_str)
    await query.edit_message_text("🗑 پیام حذف شد.")


async def chatadmin_restrict_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, message_id_str, course_id_str = query.data.split(":", 2)
    course = dbmod.get_course_by_id(int(course_id_str))
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    # نکته: این تابع هرگز user_id واقعی رو به این handler برنمی‌گردونه؛ فقط True/False.
    ok = dbmod.restrict_sender_of_message(
        int(message_id_str), scope="course", restricted_by=update.effective_user.id, reason="گزارش دانشجویی",
    )
    dbmod.log_action(update.effective_user.id, "RESTRICT_CHAT_SENDER", term=course["term_name"], detail=message_id_str)
    await query.edit_message_text("🔨 فرستنده از گپ این درس محدود شد." if ok else "این پیام دیگه وجود نداره.")


async def chatadmin_resolve_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, report_id_str, course_id_str = query.data.split(":", 2)
    course = dbmod.get_course_by_id(int(course_id_str))
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    dbmod.resolve_report(int(report_id_str), update.effective_user.id)
    await query.edit_message_text("✅ گزارش بررسی‌شده علامت خورد.")


async def chatadmin_reveal_identity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فقط سوپرادمین. نتیجه با show_alert نمایش داده می‌شه (نه edit_message_text) تا هویت
    توی متن چت/history نمونه؛ هر بار هم در audit_log ثبت می‌شه (نگاه کن به db.reveal_anonymous_sender)."""
    query = update.callback_query
    message_id = int(query.data.split(":", 1)[1])
    result = dbmod.reveal_anonymous_sender(update.effective_user.id, message_id)
    if result is None:
        await query.answer("⛔️ فقط سوپرادمین اجازه‌ی این کار رو داره.", show_alert=True)
        return
    real_display = get_display_name(result["user_id"], str(result["user_id"]))
    await query.answer(f"👑 فرستنده‌ی واقعی: {real_display} ({result['user_id']})", show_alert=True)


async def chatadmin_build_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("در حال ساخت پیش‌نویس با AI...")
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return

    period = dbmod.get_open_exam_period(course_id)
    messages = dbmod.get_chat_messages_for_course(course_id, period["id"], "exam")
    if not messages:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="هنوز پیامی در گپ این دوره‌ی امتحانی ثبت نشده.")
        return

    try:
        for m in messages:
            topics = await ai_experience.extract_topics(m["text"])
            if topics:
                dbmod.add_chat_topics(m["id"], topics)
        draft = await ai_experience.build_experience_draft(
            course["name"], course["term_name"], [{"id": m["id"], "text": m["text"]} for m in messages]
        )
    except ai_experience.AIConfigError as e:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⚠️ {e}")
        return
    except Exception:
        logger.warning("ساخت پیش‌نویس تجربه با AI شکست خورد.", exc_info=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ مدلِ AI پاسخ قابل‌قبولی نداد، دوباره امتحان کن.")
        return

    topic_counts = dbmod.get_topic_counts_for_period(course_id, period["id"])
    rendered = ai_experience.render_draft_text(course["name"], period["label"], draft, topic_counts)
    draft_id = dbmod.create_experience_draft(course_id, period["id"], rendered, [m["id"] for m in messages])
    await _send_draft_review(context, update.effective_chat.id, draft_id, course_id)


async def chatadmin_drafts_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    drafts = dbmod.list_drafts_for_course(course_id, "pending_review")
    if not drafts:
        await query.edit_message_text("پیش‌نویسِ در انتظار تأییدی نیست.")
        return
    await query.edit_message_text(f"🗂 {len(drafts)} پیش‌نویس در انتظار تأیید:")
    for d in drafts:
        await _send_draft_review(context, update.effective_chat.id, d["id"], course_id)


async def _send_draft_review(context: ContextTypes.DEFAULT_TYPE, chat_id: int, draft_id: int, course_id: int) -> None:
    draft = dbmod.get_experience_draft(draft_id)
    buttons = [
        [
            InlineKeyboardButton("✅ تأیید و انتشار", callback_data=f"chatadmin_draftok:{draft_id}:{course_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"chatadmin_draftno:{draft_id}:{course_id}"),
        ],
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"chatadmin_draftedit:{draft_id}:{course_id}")],
    ]
    await context.bot.send_message(
        chat_id=chat_id, text=draft["draft_content"], reply_markup=InlineKeyboardMarkup(buttons)
    )


async def chatadmin_draft_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, draft_id_str, course_id_str = query.data.split(":", 2)
    course_id = int(course_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    context.user_data["awaiting_draft_edit"] = {"draft_id": int(draft_id_str), "course_id": course_id}
    await context.bot.send_message(chat_id=update.effective_chat.id, text="متن نهایی پیش‌نویس رو بفرست:")


async def chatadmin_draft_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, draft_id_str, course_id_str = query.data.split(":", 2)
    draft_id, course_id = int(draft_id_str), int(course_id_str)
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    dbmod.approve_experience_draft(draft_id, update.effective_user.id)
    draft = dbmod.get_experience_draft(draft_id)

    data = load_data()
    course_data = data.setdefault(course["term_name"], {}).setdefault(course["name"], new_course())
    ensure_course_shape(course_data)
    category_label = "🩺 تجربه ارسالی دانشجویان"
    course_data["categories"].setdefault(category_label, {"files": []})["files"].append(
        {"type": "text", "content": draft["final_content"], "caption": f"تجربه امتحان - {course['name']}", "downloads": 0}
    )
    save_data(data)
    dbmod.log_action(update.effective_user.id, "APPROVE_EXPERIENCE_DRAFT", term=course["term_name"], detail=f"draft={draft_id}")
    await query.edit_message_text("✅ تأیید و در «🩺 تجربه ارسالی دانشجویان» منتشر شد.")


async def chatadmin_draft_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, draft_id_str, course_id_str = query.data.split(":", 2)
    course = dbmod.get_course_by_id(int(course_id_str))
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    dbmod.reject_experience_draft(int(draft_id_str), update.effective_user.id)
    await query.edit_message_text("❌ پیش‌نویس رد شد.")


async def chatadmin_close_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split(":", 1)[1])
    course = dbmod.get_course_by_id(course_id)
    if course is None or not can_manage_term(update.effective_user.id, course["term_name"]):
        await deny_term_access(query, course["term_name"] if course else "")
        return
    period = dbmod.get_open_exam_period(course_id)
    new_id = dbmod.close_exam_period(period["id"], new_label=f"دوره‌ی {datetime.now().strftime('%Y-%m-%d')}")
    dbmod.log_action(update.effective_user.id, "CLOSE_EXAM_PERIOD", term=course["term_name"], detail=f"course={course['name']}")
    await query.edit_message_text(
        "🔒 دوره‌ی امتحانی بسته شد و یک دوره‌ی جدید («📚 آمادگی امتحان») باز شد.\n"
        f"(دوره‌ی جدید: {new_id})"
    )


async def handle_chatadmin_draft_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    dbmod.update_experience_draft_content(pending["draft_id"], update.message.text.strip())
    context.user_data.pop("awaiting_draft_edit", None)
    await update.message.reply_text("✏️ پیش‌نویس به‌روزرسانی شد.")
    await _send_draft_review(context, update.effective_chat.id, pending["draft_id"], pending["course_id"])


async def chatadmin_casual_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        await query.edit_message_text("⛔️ گپ دوستانه فقط توسط سوپرادمین مدیریت می‌شه.")
        return
    buttons = [[InlineKeyboardButton("📨 گزارش‌های باز", callback_data="chatadmin_reports_casual")]]
    await query.edit_message_text("☕ مدیریت گپ دوستانه", reply_markup=InlineKeyboardMarkup(buttons))


async def chatadmin_reports_casual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        await query.edit_message_text("⛔️ گپ دوستانه فقط توسط سوپرادمین مدیریت می‌شه.")
        return
    reports = dbmod.get_reports_for_admin(status="open", casual_only=True)
    if not reports:
        await query.edit_message_text("گزارش باز جدیدی نیست ✅")
        return
    await query.edit_message_text(f"📨 {len(reports)} گزارش باز:")
    for r in reports:
        sender_line = "دانشجوی ناشناس 🕶" if r["is_anonymous"] else r["display_snapshot"]
        text = f"«{r['text']}»\n\nفرستنده: {sender_line}\nدلیل گزارش: {r['reason']}"
        buttons = [
            [
                InlineKeyboardButton("🗑 حذف پیام", callback_data=f"chatadmin_delmsg_c:{r['message_id']}"),
                InlineKeyboardButton("🔨 محدود کردن فرستنده", callback_data=f"chatadmin_restrict_c:{r['message_id']}"),
            ],
            [InlineKeyboardButton("✅ بررسی شد", callback_data=f"chatadmin_resolve_c:{r['report_id']}")],
        ]
        if r["is_anonymous"]:
            buttons.append(
                [InlineKeyboardButton("👑 مشاهده هویت (فقط سوپرادمین)", callback_data=f"chatadmin_reveal:{r['message_id']}")]
            )
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(buttons))


async def chatadmin_delete_message_casual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    message_id = int(query.data.split(":", 1)[1])
    dbmod.soft_delete_chat_message(message_id, update.effective_user.id)
    dbmod.log_action(update.effective_user.id, "DELETE_CHAT_MESSAGE", term=None, detail=f"casual:{message_id}")
    await query.edit_message_text("🗑 پیام حذف شد.")


async def chatadmin_restrict_sender_casual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    message_id = int(query.data.split(":", 1)[1])
    ok = dbmod.restrict_sender_of_message(message_id, scope="global", restricted_by=update.effective_user.id, reason="گزارش دانشجویی (گپ دوستانه)")
    dbmod.log_action(update.effective_user.id, "RESTRICT_CHAT_SENDER", term=None, detail=f"casual:{message_id}")
    await query.edit_message_text("🔨 فرستنده به‌صورت کلی از گپ محدود شد." if ok else "این پیام دیگه وجود نداره.")


async def chatadmin_resolve_report_casual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_super_admin(update.effective_user.id):
        return
    report_id = int(query.data.split(":", 1)[1])
    dbmod.resolve_report(report_id, update.effective_user.id)
    await query.edit_message_text("✅ گزارش بررسی‌شده علامت خورد.")


# ============================================================
# راه‌اندازی
# ============================================================

def main() -> None:
    dbmod.init_db()
    migration_report = dbmod.migrate_json_to_sqlite(
        data_dir=".",
        courses_file=DATA_FILE,
        users_file=USERS_FILE,
        subscribers_file=SUBSCRIBERS_FILE,
        programs_file=PROGRAMS_FILE,
        admin_ids=ADMIN_IDS,
    )
    if migration_report.get("skipped"):
        logger.info("مهاجرت داده قبلاً انجام شده؛ از SQLite (%s) استفاده می‌شه.", dbmod.DB_FILE)
    else:
        logger.info("مهاجرت اولیه‌ی JSON -> SQLite انجام شد: %s", migration_report)

    group_report = dbmod.seed_group_structure(dbmod.get_program_names())
    if not group_report.get("skipped"):
        logger.info("ساختار گروه/مقطع (فاز ۳) ساخته شد: %s", group_report)

    physio1_report = dbmod.seed_physiopath1_defaults(PHYSIOPATH1_NAME, PHYSIOPATH1_COURSES)
    if not physio1_report.get("skipped"):
        logger.info("درس‌های پیش‌فرضِ فیزیوپات۱ (یک‌بار برای همیشه) بررسی شدن: %s", physio1_report)

    cat_report = dbmod.migrate_category_restructure_v2()
    logger.info("مهاجرتِ ساختار جدید دسته‌بندی‌ها (idempotent) اجرا شد: %s", cat_report)
    if not cat_report.get("ok"):
        logger.warning("چک صحتِ مهاجرتِ دسته‌بندی‌ها fail شد -- لطفاً دستی بررسی کن! %s", cat_report)

    # فاز ۸: دسته‌ها (categories) رو از یک جدولِ سراسری به دسته‌های مستقلِ per-course
    # تبدیل می‌کنه -- بعد از این، ویرایش/حذفِ دسته‌ی یک درس هیچ اثری روی دسته‌ی
    # هم‌نامِ درسِ دیگه نداره (idempotent، فقط یک‌بار واقعاً اجرا می‌شه).
    percourse_report = dbmod.migrate_categories_per_course_v3()
    if not percourse_report.get("skipped"):
        logger.info("مهاجرتِ دسته‌های per-course (فاز ۸) اجرا شد: %s", percourse_report)
        if not percourse_report.get("ok", True):
            logger.warning("چک صحتِ مهاجرتِ دسته‌های per-course fail شد -- لطفاً دستی بررسی کن! %s", percourse_report)

    # برنامه کلاس‌ها (رسمی): به رکوردهای قدیمیِ class_schedule.json که فقط
    # نامِ درس دارن، در صورتِ امکان course_id اضافه می‌کنه (idempotent، امن،
    # قبلش backup می‌گیره -- نگاه کن به تعریفِ تابع برای جزئیات).
    migrate_class_schedule_course_ids()

    logger.info("در حال بررسی اتصال مستقیم به تلگرام...")
    direct_ok = can_connect_directly()

    builder = Application.builder().token(BOT_TOKEN)
    if direct_ok:
        logger.info("اتصال مستقیم برقراره — بدون پروکسی اجرا می‌شه.")
    else:
        if not PROXY_URL:
            raise SystemExit("اتصال مستقیم برقرار نشد و PROXY_URL هم تنظیم نشده.")
        logger.info("اتصال مستقیم برقرار نشد — از پروکسی استفاده می‌شه.")
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)

    builder = builder.post_init(notify_startup).post_shutdown(notify_shutdown)
    app = builder.build()

    # گیت عضویت کانال + ساعت فعالیت -- باید قبل از همه اجرا بشه (گروه -1)
    app.add_handler(MessageHandler(filters.ALL, offline_gate_message), group=-1)
    app.add_handler(CallbackQueryHandler(offline_gate_callback), group=-1)

    # عمومی
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("backupserver", backup_server_command))
    app.add_handler(CommandHandler("darsha", list_programs))
    app.add_handler(CommandHandler("setnickname", set_nickname_command))

    # ادمین
    app.add_handler(CommandHandler("addsemester", add_semester_start))
    app.add_handler(CommandHandler("addcourse", add_course_start))
    app.add_handler(CommandHandler("upload", add_course_start_upload))
    app.add_handler(CommandHandler("addguide", add_guide_start))
    app.add_handler(CommandHandler("delete", delete_start))
    app.add_handler(CommandHandler("movefile", move_file_start))
    app.add_handler(CommandHandler("setbadge", badge_start))
    app.add_handler(CommandHandler("filelink", filelink_start))
    app.add_handler(CommandHandler("rename", rename_start))
    app.add_handler(CommandHandler("editdesc", editdesc_start))
    app.add_handler(CommandHandler("filegroups", filegroups_start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("requests", requests_command))
    app.add_handler(CommandHandler("classschedule", classschedule_start))

    # سوپرادمین -- مدیریت ادمین‌ها (RBAC چندسطحی)
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("listadmins", listadmins_command))
    app.add_handler(CommandHandler("auditlog", auditlog_command))
    app.add_handler(CommandHandler("syncmembers", syncmembers_command))
    app.add_handler(CommandHandler("chatadmin", chatadmin_start))

    # عمومی -- فایل‌های ذخیره‌شده
    app.add_handler(CommandHandler("saved", saved_files_command))

    # دکمه‌های شیشه‌ای -- عضویت کانال و لقب کاربر
    app.add_handler(CallbackQueryHandler(check_membership_callback, pattern=r"^check_membership$"))
    app.add_handler(ChatMemberHandler(on_channel_membership_change, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(pick_nickname, pattern=r"^pick_nick:"))
    app.add_handler(CallbackQueryHandler(pick_nickname_custom, pattern=r"^pick_nick_custom$"))
    app.add_handler(CallbackQueryHandler(pick_nickname_skip, pattern=r"^pick_nick_skip$"))

    # دکمه‌ی شیشه‌ای -- بخش «درباره»: راهنمای استفاده از بات (فایل ثابت، نگاه کن به USAGE_GUIDE_FILE)
    app.add_handler(CallbackQueryHandler(usage_guide_callback, pattern=r"^usage_guide$"))

    # دکمه‌های شیشه‌ای -- مشارکت دانشجویان: تجربه امتحان
    app.add_handler(CallbackQueryHandler(submit_menu_exam, pattern=r"^submit_exam_start$"))
    app.add_handler(CallbackQueryHandler(submit_exam_pick_course, pattern=r"^subexam_prog:"))
    app.add_handler(CallbackQueryHandler(submit_exam_start_form, pattern=r"^subexam_course:"))
    app.add_handler(CallbackQueryHandler(exam_confirm_anon, pattern=r"^examanon:"))

    # دکمه‌های شیشه‌ای -- مشارکت دانشجویان: ارسال فایل
    app.add_handler(CallbackQueryHandler(submit_menu_file, pattern=r"^submit_file_start$"))
    app.add_handler(CallbackQueryHandler(submit_file_pick_course, pattern=r"^subfile_prog:"))
    app.add_handler(CallbackQueryHandler(submit_file_pick_category, pattern=r"^subfile_course:"))
    app.add_handler(CallbackQueryHandler(submit_file_pick_anon, pattern=r"^subfile_cat:"))
    app.add_handler(CallbackQueryHandler(submit_file_confirm_anon, pattern=r"^subfileanon:"))

    # دکمه‌های شیشه‌ای -- مشارکت دانشجویان: درخواست
    app.add_handler(CallbackQueryHandler(submit_request_start, pattern=r"^submit_request_start$"))
    app.add_handler(CallbackQueryHandler(close_request_callback, pattern=r"^close_request:"))

    # دکمه‌های شیشه‌ای -- تأیید/رد ادمین
    app.add_handler(CallbackQueryHandler(approve_submission, pattern=r"^approve_sub:"))
    app.add_handler(CallbackQueryHandler(reject_submission, pattern=r"^reject_sub:"))
    app.add_handler(CallbackQueryHandler(reject_submission, pattern=r"^reject_sub_bad:"))

    # دکمه‌های شیشه‌ای -- ادمین: آپلود / راهنما / حذف (انتخاب برنامه)
    app.add_handler(CallbackQueryHandler(pick_group_for_semester, pattern=r"^addsem_grp:"))
    app.add_handler(CallbackQueryHandler(pick_program_for_addcourse, pattern=r"^pick_prog_addcourse:"))
    app.add_handler(CallbackQueryHandler(addcourse_back_to_progs, pattern=r"^addc_back$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(course_set_active, pattern=r"^crs_active:"))
    app.add_handler(CallbackQueryHandler(course_unset_active, pattern=r"^crs_deactivate:"))
    app.add_handler(CallbackQueryHandler(course_groups_menu, pattern=r"^crs_groups:"))
    app.add_handler(CallbackQueryHandler(course_groups_toggle, pattern=r"^crs_groups_toggle:"))
    app.add_handler(CallbackQueryHandler(course_group_add_start, pattern=r"^crs_group_add:"))
    app.add_handler(CallbackQueryHandler(course_group_delete, pattern=r"^crs_group_del:"))
    app.add_handler(CallbackQueryHandler(course_edit_name_start, pattern=r"^crs_edit:"))
    app.add_handler(CallbackQueryHandler(course_delete_confirm_start, pattern=r"^crs_del:"))
    app.add_handler(CallbackQueryHandler(course_delete_execute, pattern=r"^crs_del_yes:"))
    app.add_handler(CallbackQueryHandler(course_delete_cancel, pattern=r"^crs_del_no$"))
    app.add_handler(CommandHandler("editcourse", editcourse_start))
    app.add_handler(CommandHandler("editterm", editterm_start))
    app.add_handler(CallbackQueryHandler(editterm_root, pattern=r"^eterm_root$"))
    app.add_handler(CallbackQueryHandler(editterm_pick, pattern=r"^eterm_pick:"))
    app.add_handler(CallbackQueryHandler(editterm_rename_start, pattern=r"^eterm_ren:"))
    app.add_handler(CallbackQueryHandler(editterm_delete_confirm, pattern=r"^eterm_del:"))
    app.add_handler(CallbackQueryHandler(editterm_delete_execute, pattern=r"^eterm_del_yes:"))

    # فاز ۹ -- مدیریتِ بخش‌ها (Section): ایجاد/ویرایش‌نام/حذف با id
    app.add_handler(CommandHandler("editsections", editsection_start))
    app.add_handler(CommandHandler("supportlink", support_link_command))
    app.add_handler(CommandHandler("supportoff", support_off_command))
    app.add_handler(CommandHandler("addsupporter", add_supporter_command))
    app.add_handler(CommandHandler("removesupporter", remove_supporter_command))
    app.add_handler(CallbackQueryHandler(editsection_root, pattern=r"^esec_root$"))
    app.add_handler(CallbackQueryHandler(editsection_add_start, pattern=r"^esec_add$"))
    app.add_handler(CallbackQueryHandler(editsection_pick, pattern=r"^esec_pick:"))
    app.add_handler(CallbackQueryHandler(editsection_rename_start, pattern=r"^esec_ren:"))
    app.add_handler(CallbackQueryHandler(editsection_delete_confirm, pattern=r"^esec_del:"))
    app.add_handler(CallbackQueryHandler(editsection_delete_execute, pattern=r"^esec_del_yes:"))

    # فاز ۹ -- مدیریتِ دسته‌های هر درس (per-course، id-based، مستقل بینِ درس‌ها)
    app.add_handler(CommandHandler("editcategories", editcategories_start))
    app.add_handler(CallbackQueryHandler(ecat_back_progs, pattern=r"^ecat_back_progs$"))
    app.add_handler(CallbackQueryHandler(ecat_pick_course, pattern=r"^ecat_prog:"))
    app.add_handler(CallbackQueryHandler(ecat_show_course, pattern=r"^ecat_course:"))
    app.add_handler(CallbackQueryHandler(ecat_pick_category, pattern=r"^ecat_pick:"))
    app.add_handler(CallbackQueryHandler(ecat_move, pattern=r"^ecat_move:"))
    app.add_handler(CallbackQueryHandler(ecat_toggle_mode, pattern=r"^ecat_mode:"))
    app.add_handler(CallbackQueryHandler(ecat_add_start, pattern=r"^ecat_add:"))
    app.add_handler(CallbackQueryHandler(ecat_rename_start, pattern=r"^ecat_ren:"))
    app.add_handler(CallbackQueryHandler(ecat_delete_confirm, pattern=r"^ecat_del:"))
    app.add_handler(CallbackQueryHandler(ecat_delete_execute, pattern=r"^ecat_del_yes:"))
    app.add_handler(CallbackQueryHandler(ecat_copy_start, pattern=r"^ecat_copy:"))
    app.add_handler(CallbackQueryHandler(ecat_bulk_start, pattern=r"^ecat_bulk_start:"))
    app.add_handler(CallbackQueryHandler(ecat_bulk_toggle, pattern=r"^ecat_bulk_toggle:"))
    app.add_handler(CallbackQueryHandler(ecat_bulk_confirm, pattern=r"^ecat_bulk_confirm:"))
    app.add_handler(CallbackQueryHandler(ecat_bulk_execute, pattern=r"^ecat_bulk_yes:"))
    app.add_handler(CallbackQueryHandler(ecopy_pick_course, pattern=r"^ecopy_prog:"))
    app.add_handler(CallbackQueryHandler(ecopy_pick_category, pattern=r"^ecopy_course:"))
    app.add_handler(CallbackQueryHandler(ecopy_execute, pattern=r"^ecopy_exec:"))

    app.add_handler(CallbackQueryHandler(editcourse_root, pattern=r"^ecourse_root$"))
    app.add_handler(CallbackQueryHandler(editcourse_pick_program, pattern=r"^ecourse_prog:"))
    app.add_handler(CallbackQueryHandler(editcourse_pick_course, pattern=r"^ecourse_pick:"))
    app.add_handler(CallbackQueryHandler(continue_last_upload, pattern=r"^continue_last_upload$"))
    app.add_handler(CallbackQueryHandler(upload_back_to_progs, pattern=r"^up_back_progs$"))
    app.add_handler(CallbackQueryHandler(upload_finish, pattern=r"^upfin_end$"))
    app.add_handler(CallbackQueryHandler(upload_change_path, pattern=r"^upfin_change$"))
    app.add_handler(CallbackQueryHandler(pick_program_for_upload, pattern=r"^pick_prog_upload:"))
    app.add_handler(CallbackQueryHandler(pick_course_for_upload, pattern=r"^pick_course_upload:"))
    app.add_handler(CallbackQueryHandler(pick_category_for_upload, pattern=r"^pick_cat_upload:"))
    app.add_handler(CallbackQueryHandler(pick_upload_destination, pattern=r"^pick_updest:"))
    app.add_handler(CallbackQueryHandler(pick_upload_new_group_start, pattern=r"^pick_upnewgrp:"))

    # دکمه‌های شیشه‌ای -- ادمین: مدیریت گروه‌ها و نوت‌ها (فاز ۷)
    app.add_handler(CallbackQueryHandler(fgm_back_progs, pattern=r"^fgm_back_progs$"))
    app.add_handler(CallbackQueryHandler(fgm_pick_course, pattern=r"^fgm_prog:"))
    app.add_handler(CallbackQueryHandler(fgm_pick_category, pattern=r"^fgm_course:"))
    app.add_handler(CallbackQueryHandler(fgm_show_category, pattern=r"^fgm_cat:"))
    app.add_handler(CallbackQueryHandler(fgm_prompt_new_group, pattern=r"^fgm_newgroup:"))
    app.add_handler(CallbackQueryHandler(fgm_show_group, pattern=r"^fgm_group:"))
    app.add_handler(CallbackQueryHandler(fgm_upload_entry, pattern=r"^fgm_upload:"))
    app.add_handler(CallbackQueryHandler(fgm_prompt_add_note, pattern=r"^fgm_addnote:"))
    app.add_handler(CallbackQueryHandler(fgm_show_content, pattern=r"^fgm_content:"))
    app.add_handler(CallbackQueryHandler(fgm_show_item_actions, pattern=r"^fgm_item:"))
    app.add_handler(CallbackQueryHandler(fgm_ungroup_item, pattern=r"^fgm_ungroupitem:"))
    app.add_handler(CallbackQueryHandler(fgm_prompt_edit_note, pattern=r"^fgm_editnote:"))
    app.add_handler(CallbackQueryHandler(fgm_delete_note_confirm, pattern=r"^fgm_delnote:"))
    app.add_handler(CallbackQueryHandler(fgm_delete_note_execute, pattern=r"^fgm_delnote_yes:"))
    app.add_handler(CallbackQueryHandler(fgm_rename_group_prompt, pattern=r"^fgm_ren:"))
    app.add_handler(CallbackQueryHandler(fgm_move_order, pattern=r"^fgm_up:"))
    app.add_handler(CallbackQueryHandler(fgm_move_order, pattern=r"^fgm_down:"))
    app.add_handler(CallbackQueryHandler(fgm_delete_group_confirm, pattern=r"^fgm_del:"))
    app.add_handler(CallbackQueryHandler(fgm_delete_group_execute, pattern=r"^fgm_del_yes:"))
    app.add_handler(CallbackQueryHandler(fgm_show_ungrouped, pattern=r"^fgm_ungrouped:"))
    app.add_handler(CallbackQueryHandler(fgm_toggle_move_selection, pattern=r"^fgm_movesel:"))
    app.add_handler(CallbackQueryHandler(fgm_pick_target_group, pattern=r"^fgm_movego:"))
    app.add_handler(CallbackQueryHandler(fgm_execute_move, pattern=r"^fgm_moveallto:"))
    app.add_handler(CallbackQueryHandler(upload_name_confirm, pattern=r"^upnm_yes$"))
    app.add_handler(CallbackQueryHandler(upload_name_rename_prompt, pattern=r"^upnm_rename$"))
    app.add_handler(CallbackQueryHandler(upload_post_main_menu, pattern=r"^upfin_home$"))
    app.add_handler(CallbackQueryHandler(guide_back_to_progs, pattern=r"^guide_back_progs$"))
    app.add_handler(CallbackQueryHandler(pick_program_for_guide, pattern=r"^pick_prog_guide:"))
    app.add_handler(CallbackQueryHandler(pick_course_for_guide, pattern=r"^pick_course_guide:"))
    app.add_handler(CallbackQueryHandler(delete_back_to_progs, pattern=r"^del_back_progs$"))
    app.add_handler(CallbackQueryHandler(delete_pick_course, pattern=r"^del_prog:"))
    app.add_handler(CallbackQueryHandler(delete_pick_category, pattern=r"^del_course:"))
    app.add_handler(CallbackQueryHandler(delete_pick_file, pattern=r"^del_cat:"))
    app.add_handler(CallbackQueryHandler(delete_toggle_file, pattern=r"^delsel:"))
    app.add_handler(CallbackQueryHandler(delete_confirm_selected, pattern=r"^delgo:"))
    app.add_handler(CallbackQueryHandler(delete_execute_selected, pattern=r"^delyes:"))

    # دکمه‌های شیشه‌ای -- ادمین: جابه‌جایی فایل بین دسته‌بندی‌ها (چندانتخابی)
    app.add_handler(CallbackQueryHandler(move_back_to_progs, pattern=r"^mv_back_progs$"))
    app.add_handler(CallbackQueryHandler(move_pick_course, pattern=r"^mv_prog:"))
    app.add_handler(CallbackQueryHandler(move_pick_source_category, pattern=r"^mv_course:"))
    app.add_handler(CallbackQueryHandler(move_pick_file, pattern=r"^mv_cat:"))
    app.add_handler(CallbackQueryHandler(move_toggle_file, pattern=r"^mvsel:"))
    app.add_handler(CallbackQueryHandler(move_pick_destination, pattern=r"^mvgo:"))
    app.add_handler(CallbackQueryHandler(move_execute, pattern=r"^mvdst:"))

    # دکمه‌های شیشه‌ای -- ادمین: نشان کیفیت (MedVerse Verified)
    app.add_handler(CallbackQueryHandler(badge_back_to_progs, pattern=r"^bdg_back_progs$"))
    app.add_handler(CallbackQueryHandler(badge_pick_course, pattern=r"^bdg_prog:"))
    app.add_handler(CallbackQueryHandler(badge_pick_category, pattern=r"^bdg_course:"))
    app.add_handler(CallbackQueryHandler(badge_pick_file, pattern=r"^bdg_cat:"))
    app.add_handler(CallbackQueryHandler(badge_pick_value, pattern=r"^bdg_file:"))
    app.add_handler(CallbackQueryHandler(badge_apply, pattern=r"^bdg_set:"))
    app.add_handler(CallbackQueryHandler(filelink_show_callback, pattern=r"^filelink_show:"))
    app.add_handler(CallbackQueryHandler(filelink_back_terms, pattern=r"^fl_back_terms$"))
    app.add_handler(CallbackQueryHandler(filelink_pick_course, pattern=r"^fl_term:"))
    app.add_handler(CallbackQueryHandler(filelink_pick_category, pattern=r"^fl_course:"))
    app.add_handler(CallbackQueryHandler(filelink_pick_file, pattern=r"^fl_cat:"))

    # دکمه‌های شیشه‌ای -- ادمین: تغییر نام واقعی فایل
    app.add_handler(CallbackQueryHandler(rename_back_to_progs, pattern=r"^ren_back_progs$"))
    app.add_handler(CallbackQueryHandler(rename_pick_course, pattern=r"^ren_prog:"))
    app.add_handler(CallbackQueryHandler(rename_pick_category, pattern=r"^ren_course:"))
    app.add_handler(CallbackQueryHandler(rename_pick_file, pattern=r"^ren_cat:"))
    app.add_handler(CallbackQueryHandler(rename_ask_new_name, pattern=r"^ren_file:"))

    # دکمه‌های شیشه‌ای -- ادمین: ویرایش توضیحِ (کپشنِ تلگرامیِ) یه فایل
    app.add_handler(CallbackQueryHandler(editdesc_back_to_progs, pattern=r"^desc_back_progs$"))
    app.add_handler(CallbackQueryHandler(editdesc_pick_course, pattern=r"^desc_prog:"))
    app.add_handler(CallbackQueryHandler(editdesc_pick_category, pattern=r"^desc_course:"))
    app.add_handler(CallbackQueryHandler(editdesc_pick_file, pattern=r"^desc_cat:"))
    app.add_handler(CallbackQueryHandler(editdesc_ask_new_text, pattern=r"^desc_file:"))

    # دکمه‌های شیشه‌ای -- علاقه‌مندی‌ها
    app.add_handler(CallbackQueryHandler(toggle_favorite, pattern=r"^favtoggle:"))

    # دکمه‌های شیشه‌ای -- دانشجو: مرور برنامه/درس/فایل
    app.add_handler(CallbackQueryHandler(show_program_courses, pattern=r"^prog:"))
    app.add_handler(CallbackQueryHandler(back_to_programs, pattern=r"^back_programs$"))
    app.add_handler(CallbackQueryHandler(show_group_terms, pattern=r"^grp:"))
    app.add_handler(CallbackQueryHandler(back_to_groups, pattern=r"^back_groups$"))
    app.add_handler(CallbackQueryHandler(back_to_courses, pattern=r"^back_courses:"))
    app.add_handler(CallbackQueryHandler(show_guide, pattern=r"^guide:"))
    app.add_handler(CallbackQueryHandler(show_course_categories, pattern=r"^course:"))
    app.add_handler(CallbackQueryHandler(show_manabe_submenu, pattern=r"^manabe:"))
    app.add_handler(CallbackQueryHandler(show_category_files, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(send_file, pattern=r"^filex:"))
    app.add_handler(CallbackQueryHandler(show_group_files, pattern=r"^fgrp:"))
    app.add_handler(CallbackQueryHandler(show_note, pattern=r"^notex:"))

    # دکمه‌های شیشه‌ای -- برنامه‌ریزی: منوی مشترک
    app.add_handler(CallbackQueryHandler(schedule_root, pattern=r"^schedule_root$"))
    app.add_handler(CallbackQueryHandler(today_agenda, pattern=r"^today_agenda$"))
    app.add_handler(CallbackQueryHandler(pick_class_program_for_agenda, pattern=r"^pick_class_program_agenda:"))
    app.add_handler(CallbackQueryHandler(student_groups_root, pattern=r"^sgroups_root$"))
    app.add_handler(CallbackQueryHandler(pick_class_program_for_groups, pattern=r"^pick_class_program_groups:"))
    app.add_handler(CallbackQueryHandler(student_groups_course, pattern=r"^sgroups_course:"))
    app.add_handler(CallbackQueryHandler(student_groups_pick, pattern=r"^sgroups_pick:"))

    # دکمه‌های شیشه‌ای -- برنامه کلاس‌ها (رسمی): دانشجو
    app.add_handler(CallbackQueryHandler(class_schedule_menu, pattern=r"^class_schedule_menu$"))
    app.add_handler(CallbackQueryHandler(pick_class_program, pattern=r"^pick_class_program:"))
    app.add_handler(CallbackQueryHandler(change_class_program, pattern=r"^change_class_program$"))

    # دکمه‌های شیشه‌ای -- آنبوردینگ: انتخاب ترم بعد از /start
    app.add_handler(CallbackQueryHandler(pick_start_program, pattern=r"^pick_start_program:"))
    app.add_handler(CallbackQueryHandler(pick_start_program_skip, pattern=r"^pick_start_program_skip$"))

    # دکمه‌های شیشه‌ای -- 🔔 اطلاع‌رسانیِ چند-دسته‌ای
    app.add_handler(CallbackQueryHandler(toggle_notify_callback, pattern=r"^toggle_notify:"))
    app.add_handler(CallbackQueryHandler(notify_menu_close, pattern=r"^notify_menu_close$"))
    app.add_handler(CallbackQueryHandler(show_class_day, pattern=r"^class_day:"))
    app.add_handler(CallbackQueryHandler(show_class_week, pattern=r"^class_week:"))
    app.add_handler(CallbackQueryHandler(class_resource_callback, pattern=r"^class_resource:"))

    # دکمه‌های شیشه‌ای -- برنامه کلاس‌ها (رسمی): ادمین
    app.add_handler(CallbackQueryHandler(cls_back_to_progs, pattern=r"^cls_back_progs$"))
    app.add_handler(CallbackQueryHandler(cls_pick_day, pattern=r"^cls_prog:"))
    app.add_handler(CallbackQueryHandler(cls_show_day, pattern=r"^cls_day:"))
    app.add_handler(CallbackQueryHandler(cls_add_start, pattern=r"^cls_add:"))
    app.add_handler(CallbackQueryHandler(cls_group_menu, pattern=r"^cls_group:"))
    app.add_handler(CallbackQueryHandler(cls_set_group, pattern=r"^cls_setgrp:"))
    app.add_handler(CallbackQueryHandler(cls_location_menu, pattern=r"^cls_loc:"))
    app.add_handler(CallbackQueryHandler(cls_location_edit_start, pattern=r"^cls_locedit:"))
    app.add_handler(CallbackQueryHandler(cls_location_clear, pattern=r"^cls_locclear:"))
    app.add_handler(CallbackQueryHandler(cls_quick_group_start, pattern=r"^cls_qgrp:"))
    app.add_handler(CallbackQueryHandler(cls_delete, pattern=r"^cls_del:"))
    app.add_handler(CallbackQueryHandler(cls_exception_quick_toggle, pattern=r"^cls_exquick:"))
    app.add_handler(CallbackQueryHandler(cls_exception_start, pattern=r"^cls_exc:"))

    # دکمه‌های شیشه‌ای -- برنامه امتحانات (رسمی): دانشجو
    app.add_handler(CallbackQueryHandler(exam_schedule_menu, pattern=r"^exam_schedule_menu$"))
    app.add_handler(CallbackQueryHandler(pick_class_program_for_exam, pattern=r"^pick_class_program_exam:"))

    # دکمه‌های شیشه‌ای -- برنامه امتحانات (رسمی): ادمین
    app.add_handler(CommandHandler("examschedule", examschedule_start))
    app.add_handler(CommandHandler("fixedevents", fixedevents_start))
    app.add_handler(CallbackQueryHandler(exam_admin_root, pattern=r"^examschedule_root$"))
    app.add_handler(CallbackQueryHandler(exam_admin_pick_program, pattern=r"^exam_admin_prog:"))
    app.add_handler(CallbackQueryHandler(exam_admin_add_start, pattern=r"^exam_admin_add:"))
    app.add_handler(CallbackQueryHandler(term_periods_menu, pattern=r"^termperiods:"))
    app.add_handler(CallbackQueryHandler(term_period_edit_start, pattern=r"^termperiod_edit:"))
    app.add_handler(CallbackQueryHandler(exam_type_pick, pattern=r"^examtype_pick:"))
    app.add_handler(CallbackQueryHandler(fixedevent_add_start, pattern=r"^fixedevent_add$"))
    app.add_handler(CallbackQueryHandler(fixedevent_scope_pick, pattern=r"^fixedevent_scope:"))
    app.add_handler(CallbackQueryHandler(fixedevent_delete, pattern=r"^fixedevent_del:"))
    app.add_handler(CallbackQueryHandler(exam_admin_delete, pattern=r"^exam_admin_del:"))

    # دکمه‌های شیشه‌ای -- برنامه مطالعه‌ی شخصی
    app.add_handler(CallbackQueryHandler(personal_schedule_menu, pattern=r"^personal_schedule_menu$"))
    app.add_handler(CallbackQueryHandler(show_personal_day, pattern=r"^pstudy_day:"))
    app.add_handler(CallbackQueryHandler(pstudy_add_start, pattern=r"^pstudy_add_start$"))
    app.add_handler(CallbackQueryHandler(pstudy_add_day, pattern=r"^pstudy_add_day:"))
    app.add_handler(CallbackQueryHandler(pstudy_pick_program, pattern=r"^pstudy_prog:"))
    app.add_handler(CallbackQueryHandler(pstudy_pick_course, pattern=r"^pstudy_course:"))
    app.add_handler(CallbackQueryHandler(pstudy_delete, pattern=r"^pstudy_del:"))
    app.add_handler(CallbackQueryHandler(pstudy_snooze_callback, pattern=r"^pstudy_snooze:"))

    # دکمه‌های شیشه‌ای -- 💬 گپ دانشجویی: دانشجو
    app.add_handler(CallbackQueryHandler(chat_root_back, pattern=r"^chat_root_back$"))
    app.add_handler(CallbackQueryHandler(chat_exam_root, pattern=r"^chat_exam_root$"))
    app.add_handler(CallbackQueryHandler(chat_exam_group_terms, pattern=r"^chat_exam_grp:"))
    app.add_handler(CallbackQueryHandler(chat_exam_term_courses, pattern=r"^chat_exam_term:"))
    app.add_handler(CallbackQueryHandler(chat_exam_course_enter, pattern=r"^chat_exam_course:"))
    app.add_handler(CallbackQueryHandler(chat_casual_start, pattern=r"^chat_casual_start$"))
    app.add_handler(CallbackQueryHandler(chatanon_confirm, pattern=r"^chatanon:"))
    app.add_handler(CallbackQueryHandler(chat_report_pick_reason, pattern=r"^chatreport:"))
    app.add_handler(CallbackQueryHandler(chat_report_confirm, pattern=r"^chatreportreason:"))
    app.add_handler(CallbackQueryHandler(chat_react_callback, pattern=r"^chatreact:"))
    app.add_handler(CallbackQueryHandler(chat_msg_edit_cancel, pattern=r"^chatmsgeditcancel:"))
    app.add_handler(CallbackQueryHandler(chat_msg_edit_start, pattern=r"^chatmsgedit:"))
    app.add_handler(CallbackQueryHandler(chat_msg_delete_confirm, pattern=r"^chatmsgdelconfirm:"))
    app.add_handler(CallbackQueryHandler(chat_msg_delete_cancel, pattern=r"^chatmsgdelcancel:"))
    app.add_handler(CallbackQueryHandler(chat_msg_delete_start, pattern=r"^chatmsgdel:"))
    app.add_handler(CallbackQueryHandler(chat_poll_new_cancel, pattern=r"^chatpollnewcancel$"))
    app.add_handler(CallbackQueryHandler(chat_poll_new_confirm, pattern=r"^chatpollnewconfirm$"))
    app.add_handler(CallbackQueryHandler(chat_poll_vote_callback, pattern=r"^chatpollvote:"))
    app.add_handler(CallbackQueryHandler(chat_poll_close_callback, pattern=r"^chatpollclose:"))

    # 🎭 بازی Mafia (MedVerse Hospital)
    app.add_handler(CallbackQueryHandler(mafia.mf_home, pattern=r"^mf:home$"))
    app.add_handler(CallbackQueryHandler(mafia.mf_create, pattern=r"^mf:create$"))
    app.add_handler(CallbackQueryHandler(mafia.mf_join, pattern=r"^mf:join:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_invite_join, pattern=r"^mf:invjoin:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_leave, pattern=r"^mf:leave:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_start, pattern=r"^mf:start:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_cancel, pattern=r"^mf:cancel:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_watch, pattern=r"^mf:watch:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_unwatch, pattern=r"^mf:unwatch:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_night_action, pattern=r"^mfn:"))
    app.add_handler(CallbackQueryHandler(mafia.mf_vote, pattern=r"^mfv:"))

    # دکمه‌های شیشه‌ای -- 💬 گپ دانشجویی: ادمین ترم / سوپرادمین
    app.add_handler(CallbackQueryHandler(chatadmin_term_courses, pattern=r"^chatadmin_term:"))
    app.add_handler(CallbackQueryHandler(chatadmin_course_panel, pattern=r"^chatadmin_course:"))
    app.add_handler(CallbackQueryHandler(chatadmin_reports_list, pattern=r"^chatadmin_reports:"))
    app.add_handler(CallbackQueryHandler(chatadmin_delete_message, pattern=r"^chatadmin_delmsg:"))
    app.add_handler(CallbackQueryHandler(chatadmin_restrict_sender, pattern=r"^chatadmin_restrict:"))
    app.add_handler(CallbackQueryHandler(chatadmin_resolve_report, pattern=r"^chatadmin_resolve:"))
    app.add_handler(CallbackQueryHandler(chatadmin_reveal_identity, pattern=r"^chatadmin_reveal:"))
    app.add_handler(CallbackQueryHandler(chatadmin_build_draft, pattern=r"^chatadmin_build:"))
    app.add_handler(CallbackQueryHandler(chatadmin_drafts_list, pattern=r"^chatadmin_drafts:"))
    app.add_handler(CallbackQueryHandler(chatadmin_draft_edit_start, pattern=r"^chatadmin_draftedit:"))
    app.add_handler(CallbackQueryHandler(chatadmin_draft_approve, pattern=r"^chatadmin_draftok:"))
    app.add_handler(CallbackQueryHandler(chatadmin_draft_reject, pattern=r"^chatadmin_draftno:"))
    app.add_handler(CallbackQueryHandler(chatadmin_close_period, pattern=r"^chatadmin_closeperiod:"))
    app.add_handler(CallbackQueryHandler(chatadmin_casual_panel, pattern=r"^chatadmin_casual$"))
    app.add_handler(CallbackQueryHandler(chatadmin_reports_casual, pattern=r"^chatadmin_reports_casual$"))
    app.add_handler(CallbackQueryHandler(chatadmin_delete_message_casual, pattern=r"^chatadmin_delmsg_c:"))
    app.add_handler(CallbackQueryHandler(chatadmin_restrict_sender_casual, pattern=r"^chatadmin_restrict_c:"))
    app.add_handler(CallbackQueryHandler(chatadmin_resolve_report_casual, pattern=r"^chatadmin_resolve_c:"))

    # دکمه‌های منوی دائمی (باید قبل از هندلر عمومی متن ثبت بشه)
    menu_pattern = (
        f"^({MENU_COURSES}|{MENU_NOTIFY}|{MENU_PARTICIPATE}|{MENU_SCHEDULE}|"
        f"{MENU_STUDY_GROUP}|{MENU_ABOUT}|{MENU_CHAT}|{MENU_SUPPORT})$"
    )
    app.add_handler(MessageHandler(filters.Regex(menu_pattern), handle_menu_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CHAT_EXIT_BUTTON)}$"), chat_exit))
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(CHAT_IDENTITY_TOGGLE_BUTTON)}$"), chat_identity_toggle)
    )
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(CHAT_RANDOM_TOPIC_BUTTON)}$"), chat_random_topic)
    )
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CHAT_POLL_BUTTON)}$"), chat_poll_start))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CHAT_GAME_BUTTON)}$"), mafia.chat_game_menu))

    # پیام‌های متنی و فایل‌ها
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, receive_file))

    logger.info("ربات در حال اجراست... برای توقف Ctrl+C بزن.")

    if app.job_queue is not None:
        app.job_queue.run_repeating(check_reminders, interval=60, first=10)
    else:
        logger.warning(
            'JobQueue فعال نیست؛ یادآوری‌های کلاس و مطالعه کار نمی‌کنن. '
            'برای فعال‌سازی این دستور رو بزن: pip install "python-telegram-bot[job-queue]"'
        )

    # وب‌سرور کوچک پرداخت/Callback زرین‌پال در یک thread جدا اجرا می‌شود؛
    # خود تلگرام همچنان بدون تغییر با long polling کار می‌کند.
    payment.start_payment_server()

    # allowed_updates=ALL_TYPES لازمه وگرنه تلگرام آپدیت‌های chat_member رو
    # (که تشخیصِ لفت‌دادنِ کاربر از کانال بهش وابسته‌ست) اصلاً برای بات نمی‌فرسته.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
