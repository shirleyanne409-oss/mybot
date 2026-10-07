"""
لایه‌ی دیتابیس MedVerse -- فاز ۱ مهاجرت به SQLite

این ماژول جایگزین خواندن/نوشتن مستقیم JSON برای این موجودیت‌ها شده:
    - گروه‌های آموزشی (مقطع) و ترم‌ها      -> groups, terms
    - درس‌ها، دسته‌بندی‌ها، فایل‌ها          -> courses, categories, course_files
    - کاربران و مشترکین                    -> users, subscribers
    - ادمین‌ها (چندسطحی، فاز بعدی فعالش می‌کنه) -> admins
    - ثبت فعالیت مدیران (فاز بعدی)          -> audit_log

فایل‌های دیگر (class_schedule.json, personal_schedule.json,
pending_submissions.json, requests.json, analytics.json, reminder_log.json)
فعلاً همون‌طور JSON می‌مونن و توی فازهای بعدی (که خودشون دوباره لمس می‌شن)
به همین دیتابیس منتقل می‌شن -- تا کار دوباره انجام نشه.

طراحی عمدی: توابع load_data()/save_data() و بقیه، همون شکلِ dict/list قبلی رو
برمی‌گردونن و می‌گیرن. یعنی هزاران خط منطق موجود در bot.py که این دیکشنری‌ها رو
دستکاری می‌کنن، دست‌نخورده می‌مونن -- فقط "پشتِ صحنه" به‌جای فایل JSON از
SQLite خونده/نوشته می‌شه. این یعنی صفر تغییر رفتار برای کاربر نهایی.
"""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager

DB_FILE = os.environ.get("MEDVERSE_DB_FILE", "medverse.db")

# اگه DB_FILE داخل یه زیرپوشه باشه (مثلاً /data/medverse.db روی Railway) و اون پوشه
# هنوز وجود نداشته باشه، sqlite3.connect با خطا مواجه می‌شه. اینجا فقط پوشه رو
# می‌سازیم (اگه از قبل بود، کاری نمی‌کنه)؛ خودِ فایل دیتابیس دست‌نخورده می‌مونه.
_db_dir = os.path.dirname(DB_FILE)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

# --- Bootstrap یک‌باره‌ی دیتابیس از روی یک لینک دانلود (اختیاری) -------------
# فقط برای راه‌اندازی اولیه‌ی یک Volume خالی روی Railway استفاده می‌شه، وقتی
# راه‌های دیگه (CLI/Shell) در دسترس نباشن. اگه DB_FILE از قبل وجود داشته باشه
# (یعنی داده‌ی واقعی همین الان اونجاست)، این کد هیچ‌کاری نمی‌کنه و دست به فایل
# موجود نمی‌زنه -- پس نمی‌تونه داده‌ی واقعی رو با یه نسخه‌ی دیگه جایگزین کنه.
_bootstrap_url = os.environ.get("MEDVERSE_DB_BOOTSTRAP_URL", "").strip()
if _bootstrap_url and not os.path.exists(DB_FILE):
    import urllib.request

    print(f"[bootstrap] دیتابیسی در {DB_FILE} پیدا نشد؛ در حال دانلود از MEDVERSE_DB_BOOTSTRAP_URL ...")
    _tmp_path = DB_FILE + ".bootstrap_tmp"
    try:
        urllib.request.urlretrieve(_bootstrap_url, _tmp_path)
        # یه چک حداقلی: فایل دانلودشده باید واقعاً یه فایل SQLite باشه، نه یه
        # صفحه‌ی HTML خطا (مثلاً اگه لینک اشتباه/منقضی بود).
        with open(_tmp_path, "rb") as f:
            header = f.read(16)
        if header[:16] == b"SQLite format 3\x00":
            os.replace(_tmp_path, DB_FILE)
            print(f"[bootstrap] دیتابیس با موفقیت در {DB_FILE} قرار گرفت.")
        else:
            os.remove(_tmp_path)
            print("[bootstrap] فایل دانلودشده یک دیتابیس SQLite معتبر نیست؛ نادیده گرفته شد.")
    except Exception as e:
        print(f"[bootstrap] دانلود ناموفق بود: {e}")
        if os.path.exists(_tmp_path):
            os.remove(_tmp_path)

# دسته‌بندی‌های پیش‌فرض (همون CATEGORIES قبلی در bot.py) -- فعلاً سراسری،
# در فازهای بعدی می‌تونه به ازای هر ترم/درس قابل تنظیم بشه.
#
# ساختار جدید (migration v2 -- بازآرایی دسته‌بندی‌های داخل درس):
#   📚 منابع (نمایشی، UI-only) شامل ۴ دسته‌ی اول زیره:
#     📖 رفرنس و پاور  (قبلاً «📖 منابع اصلی»)
#     📝 جزوه          (جدید)
#     📌 خلاصه          (قبلاً «📝 خلاصه‌ها»)
#     ⭐️ نکات امتحانی   (بدون تغییر اسم)
#   💡 پیشنهاد مطالعه  (جدید، مستقل)
#   📘 Study_Guid       (جدید -- از داخل «🩺 تجربه امتحان دانشجویان» جدا شد)
#   ❓ نمونه‌سوالات      (قبلاً «❓ نمونه سوالات»)
#   🩺 تجربه ارسالی دانشجویان (قبلاً «🩺 تجربه امتحان دانشجویان»)
#   🧪 عملی             (جدید -- فقط برای درس‌هایی که courses.has_practical=1 داره)
#   📂 فایل‌های تکمیلی   (بدون تغییر)
#   📋 طرح درس          (جدید -- فقط برای درس‌هایی که courses.has_lesson_plan=1 داره؛
#                        عمداً در انتهای لیست اضافه شده تا ایندکسِ دسته‌های قبلی جابه‌جا نشه)
DEFAULT_CATEGORIES = [
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
    "📚 رفرنس‌ها",
]

# دسته‌های قدیمی که با migration v2 تغییر اسم دادن: {اسم قدیم: اسم جدید}
_CATEGORY_RENAMES_V2 = {
    "📖 منابع اصلی": "📖 پاور",
    "📝 خلاصه‌ها": "📌 خلاصه",
    "❓ نمونه سوالات": "❓ نمونه‌سوالات",
    "🩺 تجربه امتحان دانشجویان": "🩺 تجربه ارسالی دانشجویان",
}

# درس‌هایی که فعلاً باید بخش «🧪 عملی» داشته باشن: [(نام ترم, نام درس), ...]
PRACTICAL_COURSES_V2 = [
    ("ترم ۱", "مقدمات علوم تشریح"),
    ("ترم ۱", "علوم تشریح اسکلتی عضلانی"),
    ("ترم ۱", "بیوشیمی مولکول و سلول"),
    ("ترم ۲", "علوم تشریح قلب و عروق"),
    ("ترم ۲", "علوم تشریح دستگاه تنفس"),
    ("ترم ۲", "علوم تشریح دستگاه گوارش"),
    ("ترم ۲", "بیوشیمی دیسیپلین"),
    ("ترم ۳", "علوم تشریح سر و گردن"),
    ("ترم ۳", "علوم تشریح ادراری تناسلی"),
    ("ترم ۳", "علوم تشریح حواس ویژه"),
    ("ترم ۳", "علوم تشریح غدد درون‌ریز"),
    ("ترم ۳", "ایمنی‌شناسی"),
    ("ترم ۴", "باکتری‌شناسی"),
    ("ترم ۴", "انگل‌شناسی"),
    ("ترم ۴", "قارچ‌شناسی"),
    ("ترم ۴", "نوروآناتومی"),
    ("فیزیوپات ۱", "پاتولوژی"),
]

# درس‌هایی که فعلاً باید بخش «📋 طرح درس» داشته باشن: [(نام ترم, نام درس), ...]
# (فایل‌های رسمیِ تقسیم‌بندی مباحث بین استادان -- مستقل از منابع/جزوه/فایل‌های تکمیلی)
LESSON_PLAN_COURSES_V2 = [
    ("فیزیوپات ۱", "جراحی"),
    ("فیزیوپات ۱", "روان"),
    ("فیزیوپات ۱", "عفونی"),
    ("فیزیوپات ۱", "اطفال"),
    ("فیزیوپات ۲", "کورس قلب"),
    ("فیزیوپات ۲", "کورس خون"),
    ("فیزیوپات ۲", "کورس گوارش"),
    ("فیزیوپات ۳", "کورس تنفس"),
    ("فیزیوپات ۳", "کورس غدد"),
    ("فیزیوپات ۳", "کورس کلیه"),
    ("فیزیوپات ۳", "کورس روماتولوژی"),
    ("فیزیوپات ۳", "کورس اعصاب"),
]

DEFAULT_GROUP_NAME = "بدون گروه"  # ترم‌های قدیمی (فیزیوپات ۱-۳) که هنوز گروه ندارن
PHYSIOPATH_GROUP_NAME = "🩺 فیزیوپات"
BASIC_SCIENCES_GROUP_NAME = "📚 علوم پایه"
BASIC_SCIENCES_TERMS = ["ترم ۱", "ترم ۲", "ترم ۳", "ترم ۴"]

# حداکثرِ تعدادِ بازیکنانِ یک بازیِ Mafia -- در join_mafia_game استفاده می‌شه.
# (حداقل ۶ توسطِ mafia.py قبل از شروع چک می‌شه چون به فازِ لابی/UI مربوطه، نه ذخیره‌سازی.)
MAFIA_MAX_PLAYERS = 15


# ============================================================
# اتصال و ساخت جدول‌ها
# ============================================================

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """جدول‌ها رو اگه نبودن می‌سازه. Idempotent -- هر بار اجرا بی‌خطره."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            -- فاز ۱۰ (migrate_terms_unique_per_group_v4): یکتاییِ نامِ ترم دیگه
            -- سراسری نیست -- در سطحِ گروهه (UNIQUE(group_id, name))، تا دو گروهِ
            -- مختلف (مثلاً «📚 علوم پایه» و «🩺 فیزیوپات») هر کدوم بتونن ترمی به نامِ
            -- «رفرنس» داشته باشن، بدون تصادم. برای دیتابیس‌های از قبل موجود (که این
            -- جدول رو با UNIQUE(name) قدیمی ساختن)، migrate_terms_unique_per_group_v4
            -- جدول رو بازسازی می‌کنه.
            CREATE TABLE IF NOT EXISTS terms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(group_id, name)
            );

            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                guide TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                has_practical INTEGER NOT NULL DEFAULT 0,
                has_lesson_plan INTEGER NOT NULL DEFAULT 0,
                -- فاز ۱۰: 'standard' یعنی همون ۱۱ دسته‌ی استاندارد + منطقِ عملی/طرح‌درس/منابع
                -- (رفتارِ قدیمی، بدون تغییر)؛ 'custom' یعنی این درس کاملاً database-driven
                -- عمل می‌کنه -- هر دسته‌ای که در جدولِ categories برای این course_id باشه
                -- (هر اسمی، هر تعداد) دقیقاً همون‌طور نمایش داده می‌شه، بدون هیچ فیلترِ
                -- CATEGORIES/عملی/طرح‌درس/منابع. برای ترم‌های «رفرنس» (یا هر ساختارِ
                -- سفارشیِ دیگه‌ای که بعداً لازم بشه) از این حالت استفاده می‌شه.
                category_mode TEXT NOT NULL DEFAULT 'standard',
                UNIQUE(term_id, name)
            );

            -- فاز ۸ (migrate_categories_per_course_v3): دسته‌ها دیگه سراسری نیستن --
            -- هر دسته به یک course_id مشخص وابسته‌ست و id مستقل خودش رو داره.
            -- یعنی «قلب» توی فیزیولوژی و «قلب» توی آناتومی دو ردیف کاملاً جدا با
            -- id متفاوتن؛ حذف/ویرایشِ یکی هیچ اثری روی اون یکی نداره.
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(course_id, name)
            );

            CREATE TABLE IF NOT EXISTS course_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                category_id INTEGER NOT NULL REFERENCES categories(id),
                fid TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL DEFAULT 'file',
                file_id TEXT,
                url TEXT,
                caption TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                content TEXT,
                downloads INTEGER NOT NULL DEFAULT 0,
                badge TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_channel INTEGER NOT NULL DEFAULT 0,
                nickname_asked INTEGER NOT NULL DEFAULT 0,
                nickname TEXT,
                class_program TEXT,
                saved_fids TEXT NOT NULL DEFAULT '[]',
                notify_prefs TEXT,
                program_asked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY
            );

            -- حامی‌های مالیِ تأییدشده. همچنان مستقل نگه داشته می‌شود تا قابلیت
            -- ثبت/حذف دستیِ حامی برای سوپرادمین حفظ شود؛ پرداخت موفق زرین‌پال هم
            -- به‌صورت خودکار user_id را به همین جدول اضافه می‌کند.
            CREATE TABLE IF NOT EXISTS supporters (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- تاریخچه‌ی پرداخت‌های حمایت مالی. amount همیشه به ریال ذخیره می‌شود.
            -- authority یکتاست تا callback تکراری یا refresh صفحه باعث ثبت دوباره نشود.
            CREATE TABLE IF NOT EXISTS support_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER NOT NULL CHECK(amount > 0),
                authority TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'paid', 'failed', 'cancelled')),
                ref_id TEXT,
                gateway_code INTEGER,
                gateway_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_support_payments_user
                ON support_payments(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_support_payments_status
                ON support_payments(status, created_at);

            -- یک کاربر می‌تونه هم‌زمان ادمینِ چند ترمِ مختلف باشه؛ برای همین user_id
            -- دیگه PRIMARY KEY نیست (وگرنه هر ادمینیِ جدید، ادمینیِ قبلیِ همون کاربر رو
            -- بازنویسی/حذف می‌کرد) -- هر رابطه‌ی (user_id, role برای TERM_ADMIN, term_id)
            -- یک ردیفِ مستقله. نگاه کن به migrate_admins_multi_term_v5 برای مهاجرتِ
            -- دیتابیس‌های قدیمی‌تری که user_id توشون PRIMARY KEY بوده.
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN', 'TERM_ADMIN')),
                term_id INTEGER REFERENCES terms(id) ON DELETE CASCADE,
                UNIQUE(user_id, term_id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                term TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS exam_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                course TEXT NOT NULL,
                exam_date TEXT NOT NULL,
                exam_time TEXT,
                location TEXT
            );

            -- فاز ۴ -- گپ دانشجویی
            --
            -- نکته‌ی امنیتی مهم: user_id واقعیِ فرستنده همیشه اینجا ذخیره می‌شه (برای
            -- محدودسازی/ضدِاسپم و شمارش دانشجوی تکراری لازمه)، ولی هیچ تابعی که برای
            -- ادمین ترم صدا زده می‌شه (get_chat_messages_for_admin, get_reports_for_admin, ...)
            -- این فیلد رو برای ردیف‌های is_anonymous=1 برنمی‌گردونه. فقط reveal_anonymous_sender()
            -- که گارد SUPER_ADMIN داره و در audit_log ثبت می‌شه، اجازه‌ی این کار رو داره.
            CREATE TABLE IF NOT EXISTS exam_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'prep' CHECK(phase IN ('prep', 'post')),
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                opened_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_type TEXT NOT NULL CHECK(chat_type IN ('exam', 'casual')),
                course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
                exam_period_id INTEGER REFERENCES exam_periods(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                is_anonymous INTEGER NOT NULL DEFAULT 0,
                display_snapshot TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_by INTEGER,
                reply_to_id INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_chat_messages_room
                ON chat_messages(chat_type, course_id, is_deleted);

            -- هر ردیفِ فن‌اوت یک پیام رو -- برای هر گیرنده، همون کپیِ تلگرامیِ پیام
            -- (chat_id گیرنده == user_id چون همه‌چیز توی چت خصوصی کاربر با رباته، و
            -- telegram_message_id همون پیامیه که ربات براش فرستاده) رو ذخیره می‌کنه.
            -- بدونِ این جدول امکان ندارد بعداً پیام رو ویرایش/حذف کنیم یا از روی
            -- «ریپلای تلگرامی» کاربر بفهمیم داره به کدوم پیام‌مون پاسخ می‌ده.
            CREATE TABLE IF NOT EXISTS chat_message_deliveries (
                message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                PRIMARY KEY (message_id, recipient_id)
            );

            CREATE INDEX IF NOT EXISTS idx_chat_deliveries_reverse
                ON chat_message_deliveries(recipient_id, telegram_message_id);

            -- ریکشن‌های یک پیام. کلید یکتا یعنی هر کاربر با هر ایموجی فقط یک بار
            -- می‌تونه ریکشن بده؛ تپ دوباره روی همون ایموجی (toggle) یعنی برداشتنش.
            CREATE TABLE IF NOT EXISTS chat_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(message_id, user_id, emoji)
            );

            -- فاز ۷ -- نظرسنجی و «دو راهی سخت». هر نظرسنجی یه پیامِ گپِ معمولی هم
            -- داره (chat_message_id) که متنِ نتایج توش زندگی می‌کنه و از همون
            -- زیرساختِ فن‌اوت/ادیت/ریکشن chat_messages استفاده می‌کنه؛ برای همینه
            -- که نظرسنجی‌ها controls ویرایش/حذفِ عمومی نمی‌گیرن (به‌جاش دکمه‌ی
            -- مخصوصِ «بستن نظرسنجی» دارن) -- نگاه کن به bot.py::_post_chat_message.
            CREATE TABLE IF NOT EXISTS chat_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_type TEXT NOT NULL,
                course_id INTEGER,
                creator_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'poll' CHECK(kind IN ('poll', 'would_you_rather')),
                chat_message_id INTEGER REFERENCES chat_messages(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_poll_votes (
                poll_id INTEGER NOT NULL REFERENCES chat_polls(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                voted_at TEXT NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            );

            -- موضوعاتی که AI روی هر پیام تشخیص داده -- شمارش «چند دانشجوی مستقل»
            -- همیشه با COUNT(DISTINCT chat_messages.user_id) روی این جدول محاسبه می‌شه،
            -- نه با یک عدد که مستقیم از خروجی AI اعتماد بشه.
            CREATE TABLE IF NOT EXISTS chat_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
                topic_label TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_room_members (
                chat_type TEXT NOT NULL,
                course_id INTEGER,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                identity_mode TEXT NOT NULL DEFAULT 'nickname' CHECK(identity_mode IN ('nickname', 'anonymous')),
                PRIMARY KEY (chat_type, course_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS chat_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
                reporter_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
                resolved_by INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_restrictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('global', 'course')),
                course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
                restricted_by INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                lifted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS exam_experience_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                exam_period_id INTEGER NOT NULL REFERENCES exam_periods(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK(status IN ('pending_review', 'approved', 'rejected')),
                draft_content TEXT NOT NULL,
                final_content TEXT,
                source_message_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                approved_by INTEGER,
                approved_at TEXT,
                version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS exam_experience_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id INTEGER NOT NULL REFERENCES exam_experience_drafts(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                vote TEXT NOT NULL CHECK(vote IN ('same', 'different')),
                created_at TEXT NOT NULL,
                UNIQUE(draft_id, user_id)
            );

            -- بازی Mafia (MedVerse Hospital) -- جایگزینِ کاملِ بازیِ سؤال‌محورِ قبلی.
            -- هر بازی به یک اتاقِ گپ (chat_type + course_id، دقیقاً مثلِ chat_polls)
            -- محدود می‌شه؛ به همین خاطر هم‌زمان فقط یک بازیِ «لابی یا در حالِ اجرا»
            -- می‌تونه توی هر اتاق وجود داشته باشه (چک می‌شه توی get_active_mafia_game).
            CREATE TABLE IF NOT EXISTS mafia_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_type TEXT NOT NULL,
                course_id INTEGER,
                host_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'lobby'
                    CHECK(status IN ('lobby', 'running', 'finished', 'cancelled')),
                phase TEXT NOT NULL DEFAULT 'lobby'
                    CHECK(phase IN ('lobby', 'night', 'day', 'discussion', 'voting', 'ended')),
                round_number INTEGER NOT NULL DEFAULT 0,
                winner TEXT CHECK(winner IN ('city', 'mafia')),
                mvp_user_id INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                invites_sent INTEGER NOT NULL DEFAULT 0,
                invited_joins INTEGER NOT NULL DEFAULT 0,
                invite_sent_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_mafia_games_room
                ON mafia_games(chat_type, course_id, status);

            CREATE TABLE IF NOT EXISTS mafia_players (
                game_id INTEGER NOT NULL REFERENCES mafia_games(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT CHECK(role IN ('mafia', 'detective', 'doctor', 'citizen')),
                alive INTEGER NOT NULL DEFAULT 1,
                is_host INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT NOT NULL,
                eliminated_round INTEGER,
                eliminated_by TEXT,
                PRIMARY KEY (game_id, user_id)
            );

            -- اقدامِ شبانه‌ی هر نقش. UNIQUE روی (game_id, round_number, actor_id) یعنی
            -- انتخابِ دوباره (پشیمون شدن قبل از پایانِ شب) جایگزینِ انتخابِ قبلی می‌شه.
            CREATE TABLE IF NOT EXISTS mafia_night_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES mafia_games(id) ON DELETE CASCADE,
                round_number INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('kill', 'save', 'inspect')),
                target_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(game_id, round_number, actor_id)
            );

            CREATE TABLE IF NOT EXISTS mafia_votes (
                game_id INTEGER NOT NULL REFERENCES mafia_games(id) ON DELETE CASCADE,
                round_number INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (game_id, round_number, voter_id)
            );

            CREATE TABLE IF NOT EXISTS mafia_spectators (
                game_id INTEGER NOT NULL REFERENCES mafia_games(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (game_id, user_id)
            );

            -- آرشیوِ پرونده‌های بسته‌شده -- برای نمایشِ فوریِ گزارشِ پایانی کافیه،
            -- ولی نگه‌داشتنش به‌عنوانِ تاریخچه هم رایگانه و بعداً (لیگ/آمار کلی) لازم می‌شه.
            CREATE TABLE IF NOT EXISTS mafia_game_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                chat_type TEXT NOT NULL,
                course_id INTEGER,
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- فاز ۷ -- لایه‌ی «گروه» زیرِ دسته‌بندیِ فعلی (مثلاً استاد/جلسه) برای هر
            -- درس+دسته‌بندی. این لایه جایگزینِ دسته‌بندی‌های فعلی نمی‌شه، فقط یه سطح
            -- زیرِ اون‌هاست. فایل/نوتی که group_id نداره (NULL) دقیقاً رفتار قدیمی رو
            -- داره -- مستقیم زیرِ دسته‌بندی نمایش داده می‌شه.
            CREATE TABLE IF NOT EXISTS course_file_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                category_id INTEGER NOT NULL REFERENCES categories(id),
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(course_id, category_id, name)
            );

            -- فاز ۱۳ (گروه‌بندیِ عملی) -- هر گروهِ یه درسِ خاص (مثلاً «گروه ۱»،
            -- یا «پسر»/«دختر») با روز و ساعتِ خودش. ادمین این‌ها رو برای درسی که
            -- has_group_split=1 داره تعریف می‌کنه؛ خودِ تخصیصِ رسمی (کی تو کدوم
            -- گروهه) از جای دیگه‌ای (نماینده/آموزش) میاد -- دانشجو فقط همون
            -- تخصیصِ ازقبل‌مشخص‌شده رو تو بات ثبت می‌کنه.
            CREATE TABLE IF NOT EXISTS course_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                day TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            -- انتخابِ هر دانشجو برای هر درسِ گروه‌بندی‌شده -- جدا جدا، چون ممکنه
            -- یه نفر تو باکتری گروه ۱ باشه ولی تو انگل گروه ۳ (نگاه کن به بحثِ
            -- طراحی). بدونِ قفل/محدودیتِ ظرفیت -- دانشجو هروقت بخواد می‌تونه
            -- عوضش کنه. حذفِ خودِ گروه (course_groups) این ردیف رو هم پاک می‌کنه
            -- (ON DELETE CASCADE) تا انتخابِ یتیم/گمراه‌کننده نمونه.
            CREATE TABLE IF NOT EXISTS student_course_groups (
                user_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                group_id INTEGER NOT NULL REFERENCES course_groups(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, course_id)
            );
            """
        )
        # ستون‌هایی که به جدول‌های از قبل موجود اضافه شدن (idempotent -- فقط اگه نبودن اضافه می‌شن)
        existing_course_cols = {r["name"] for r in conn.execute("PRAGMA table_info(courses)")}
        if "has_practical" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN has_practical INTEGER NOT NULL DEFAULT 0")
        if "has_lesson_plan" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN has_lesson_plan INTEGER NOT NULL DEFAULT 0")
        if "single_category" not in existing_course_cols:
            # قدیمی/deprecated -- نگه داشته شده فقط برای سازگاری با دیتابیس‌های
            # خیلی قدیمی. منطقِ جدید از category_mode استفاده می‌کنه (پایین‌تر).
            conn.execute("ALTER TABLE courses ADD COLUMN single_category TEXT")
        if "category_mode" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN category_mode TEXT NOT NULL DEFAULT 'standard'")
            # هر درسی که قبلاً single_category روش ست شده بود (حالتِ قدیمیِ
            # «تک‌دسته‌ای») از این به بعد category_mode='custom' می‌گیره -- یعنی
            # الان دیگه محدود به همون یک دسته نیست، بلکه *همه‌ی* دسته‌های واقعیِ
            # اون درس در دیتابیس نمایش داده می‌شن (که عملاً یعنی همون یکی، چون قبلاً
            # UI فقط اجازه‌ی ساختِ همون یکی رو می‌داد -- هیچ محتوایی گم نمی‌شه).
            conn.execute(
                "UPDATE courses SET category_mode = 'custom' WHERE single_category IS NOT NULL"
            )
        # فاز ۱۲ (کورس‌های چرخشی/فیزیوپات) -- به‌جای تاریخِ از‌پیش‌حدس‌زده‌شده، ادمین
        # دستی تأیید می‌کنه الان کدوم کورس (مثلاً «اطفال»، «جراحی») در حالِ برگزاریه.
        # هر لحظه حداکثر یک کورس در هر ترم is_active_course=1 داره؛ set_active_course()
        # این محدودیت رو خودش تضمین می‌کنه. activated_at/finished_at خودکار و بر
        # اساسِ لحظه‌ی تأییدِ ادمین پر می‌شن (نه تاریخِ حدسی) -- برای آرشیوِ
        # کورس‌های تمام‌شده و اتصالِ خودکارِ منابع به کورسِ فعال استفاده می‌شن.
        if "is_active_course" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN is_active_course INTEGER NOT NULL DEFAULT 0")
        if "activated_at" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN activated_at TEXT")
        if "finished_at" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN finished_at TEXT")
        # فاز ۱۳ (گروه‌بندیِ عملی) -- بعضی درس‌ها (باکتری/انگل، یا حتی کلاس‌های
        # نظری‌ای مثلِ انقلاب/دفاعِ‌مقدس که پسر/دختر جدا میرن) بینِ چند گروه با
        # ساعت‌های متفاوت تقسیم می‌شن. has_group_split صرفاً یه پرچمه؛ خودِ
        # گروه‌ها تو جدولِ course_groups (پایین‌تر) تعریف می‌شن.
        if "has_group_split" not in existing_course_cols:
            conn.execute("ALTER TABLE courses ADD COLUMN has_group_split INTEGER NOT NULL DEFAULT 0")

        # فاز ۱۵ -- بازه‌های زمانیِ سطحِ ترم: «بازه‌ی کلاسی» (کِی تا کِی کلاس‌های
        # هفتگی برگزار می‌شه) و «بازه‌ی پایان‌ترم» (امتحاناتِ نهایی) از هم کاملاً
        # مستقل‌ان و ممکنه هم‌پوشانی داشته باشن (مثلاً میان‌ترم‌ها وقتی گرفته
        # می‌شن که کلاس‌ها هنوز ادامه دارن). میان‌ترم‌ها بازه‌ی جداگانه نمی‌گیرن --
        # هرکدوم صرفاً یه ردیفِ دستی تو exam_schedule با exam_type='midterm'.
        # مقادیر عمداً متنِ آزادن (نه تاریخِ واقعی) چون این پروژه از اول برای
        # exam_date هم همینطور بوده -- بدونِ نیازِ به کتابخانه‌ی تقویمِ جدید.
        existing_term_cols = {r["name"] for r in conn.execute("PRAGMA table_info(terms)")}
        for col in ("class_period_start", "class_period_end", "final_exam_start", "final_exam_end"):
            if col not in existing_term_cols:
                conn.execute(f"ALTER TABLE terms ADD COLUMN {col} TEXT")

        # فاز ۱۵ -- نوعِ امتحان (میان‌ترم/پایان‌ترم) برای هر ردیفِ exam_schedule.
        # مقدارِ خالی/NULL یعنی «نامشخص» (امتحان‌هایی که قبل از این فیچر ثبت شدن).
        existing_exam_cols = {r["name"] for r in conn.execute("PRAGMA table_info(exam_schedule)")}
        if "exam_type" not in existing_exam_cols:
            conn.execute("ALTER TABLE exam_schedule ADD COLUMN exam_type TEXT")

        # description (توضیحِ فایل) و content (متن کاملِ ورودی‌های type='text' مثل
        # تجربه امتحان) -- قبلاً این دو تا اصلاً ستون نداشتن و توی save_data/load_data
        # بی‌صدا گم می‌شدن؛ اینجا برای دیتابیس‌های از قبل موجود اضافه‌شون می‌کنیم.
        existing_file_cols = {r["name"] for r in conn.execute("PRAGMA table_info(course_files)")}
        if "description" not in existing_file_cols:
            conn.execute("ALTER TABLE course_files ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "content" not in existing_file_cols:
            conn.execute("ALTER TABLE course_files ADD COLUMN content TEXT")

        # فاز ۷ -- ارجاع به گروهِ زیرِ دسته‌بندی (course_file_groups.id). NULL یعنی
        # «بدون گروه» -- یعنی دقیقاً همون رفتار قدیمی. حذفِ یک گروه هرگز این ستون رو
        # cascade نمی‌کنه (در delete_file_group قبل از حذفِ گروه صریحاً NULL می‌شه)،
        # پس هیچ فایلی به‌خاطرِ حذفِ گروه از بین نمی‌ره.
        if "group_id" not in existing_file_cols:
            conn.execute(
                "ALTER TABLE course_files ADD COLUMN group_id INTEGER REFERENCES course_file_groups(id)"
            )

        # فاز ۵ -- تقسیم اطلاع‌رسانی به چند دسته (به‌جای یه لیستِ subscribers یکپارچه).
        # برای دیتابیس‌های از قبل موجود اضافه می‌شه؛ روی دیتابیسِ تازه هم بی‌خطره چون
        # ستون از قبل توی CREATE TABLE users هست.
        existing_user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "notify_prefs" not in existing_user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN notify_prefs TEXT")
        if "program_asked" not in existing_user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN program_asked INTEGER NOT NULL DEFAULT 0")
        # last_seen -- برای معیارِ «فعالِ اخیر» توی /stats. تاریخچه فقط از لحظه‌ی
        # اضافه‌شدنِ این ستون به بعد ثبت می‌شه؛ کاربرهای قدیمی تا اولین پیامِ
        # بعدی‌شون last_seen خالی دارن و در شمارشِ «فعالِ اخیر» حساب نمی‌شن.
        if "last_seen" not in existing_user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")

        # فاز ۶ -- هویت قابل‌تغییر در حین گپ + ریپلای + ویرایش پیام. برای دیتابیس‌های
        # از قبل موجود اضافه می‌شن؛ روی دیتابیسِ تازه هم بی‌خطره چون ستون از قبل توی
        # CREATE TABLEهای بالا هست.
        existing_room_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_room_members)")}
        if "identity_mode" not in existing_room_cols:
            conn.execute(
                "ALTER TABLE chat_room_members ADD COLUMN identity_mode TEXT NOT NULL DEFAULT 'nickname'"
            )
        existing_msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)")}
        if "reply_to_id" not in existing_msg_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN reply_to_id INTEGER")
        if "updated_at" not in existing_msg_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN updated_at TEXT")

        # دعوتِ عمومیِ لابیِ مافیا -- شمارشِ دعوت‌های ارسال‌شده و ورودی‌های حاصل از
        # اون‌ها، برای دیتابیس‌های از قبل موجود اضافه می‌شه (روی دیتابیسِ تازه بی‌خطره
        # چون ستون از قبل توی CREATE TABLE mafia_games هست).
        existing_mafia_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mafia_games)")}
        if "invites_sent" not in existing_mafia_cols:
            conn.execute("ALTER TABLE mafia_games ADD COLUMN invites_sent INTEGER NOT NULL DEFAULT 0")
        if "invited_joins" not in existing_mafia_cols:
            conn.execute("ALTER TABLE mafia_games ADD COLUMN invited_joins INTEGER NOT NULL DEFAULT 0")
        if "invite_sent_at" not in existing_mafia_cols:
            conn.execute("ALTER TABLE mafia_games ADD COLUMN invite_sent_at TEXT")

        # نکته: از migrate_categories_per_course_v3 به بعد، «دسته‌بندیِ پیش‌فرض» دیگه
        # سراسری/بی‌قیدوشرط seed نمی‌شه -- چون هر ردیفِ categories باید یک course_id
        # مشخص داشته باشه (ستونِ NOT NULL). ساختِ دسته‌های پیش‌فرض برای درس‌های موجود
        # کارِ migrate_categories_per_course_v3 هست؛ برای درسِ تازه‌ساخته‌شده هم
        # save_data() خودش (از رویِ new_course() در bot.py) دسته‌ها رو می‌سازه.

    # فاز ۱۰: یکتاییِ نامِ ترم رو از سطحِ سراسری به سطحِ گروه می‌بره (بدونِ از دست
    # رفتنِ هیچ داده‌ای). جدا از تراکنشِ create-table های بالا نگه داشته شده چون
    # ممکنه لازم باشه یه بار PRAGMA foreign_keys رو موقتاً خاموش کنه.
    migrate_terms_unique_per_group_v4()

    # فاز ۱۱: امکانِ ادمینیِ هم‌زمانِ یک کاربر روی چند ترمِ مختلف (بدونِ اینکه
    # ادمینیِ جدید، ادمینیِ قبلیِ همون کاربر رو حذف کنه).
    migrate_admins_multi_term_v5()


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_support_link():
    """لینک موقت حمایت مالی؛ None یعنی فراخوان/لینک فعال نیست."""
    with get_conn() as conn:
        value = _meta_get(conn, "support_link")
        return value.strip() if value and value.strip() else None


def set_support_link(url):
    """لینک حمایت را تنظیم یا با None/رشته خالی غیرفعال می‌کند."""
    with get_conn() as conn:
        _meta_set(conn, "support_link", (url or "").strip())


def is_supporter(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM supporters WHERE user_id = ?", (int(user_id),)).fetchone()
        return row is not None


def set_supporter(user_id: int, enabled: bool = True) -> None:
    with get_conn() as conn:
        if enabled:
            conn.execute("INSERT OR IGNORE INTO supporters (user_id) VALUES (?)", (int(user_id),))
        else:
            conn.execute("DELETE FROM supporters WHERE user_id = ?", (int(user_id),))


def create_support_payment(user_id, amount: int, authority: str) -> int:
    """یک پرداخت تازه‌ی زرین‌پال را ثبت می‌کند و id داخلی را برمی‌گرداند.

    user_id برای لینک عمومی کانال می‌تواند None باشد. amount همیشه ریال است.
    """
    uid = int(user_id) if user_id not in (None, "", 0, "0") else None
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO support_payments (user_id, amount, authority, status) "
            "VALUES (?, ?, ?, 'pending')",
            (uid, int(amount), str(authority)),
        )
        return int(cur.lastrowid)


def get_support_payment_by_authority(authority: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM support_payments WHERE authority = ?",
            (str(authority),),
        ).fetchone()
        return dict(row) if row else None


def set_support_payment_status(
    authority: str,
    status: str,
    *,
    gateway_code=None,
    gateway_message=None,
    ref_id=None,
) -> bool:
    """وضعیت یک پرداخت را idempotent به‌روزرسانی می‌کند.

    پرداختی که قبلاً paid شده هرگز به failed/cancelled برنمی‌گردد.
    """
    if status not in {"pending", "paid", "failed", "cancelled"}:
        raise ValueError("invalid support payment status")
    with get_conn() as conn:
        current = conn.execute(
            "SELECT status FROM support_payments WHERE authority = ?",
            (str(authority),),
        ).fetchone()
        if not current:
            return False
        if current["status"] == "paid" and status != "paid":
            return True
        paid_at_sql = "CURRENT_TIMESTAMP" if status == "paid" else "paid_at"
        conn.execute(
            f"UPDATE support_payments SET status = ?, gateway_code = ?, gateway_message = ?, "
            f"ref_id = COALESCE(?, ref_id), paid_at = {paid_at_sql}, "
            "updated_at = CURRENT_TIMESTAMP WHERE authority = ?",
            (
                status,
                int(gateway_code) if gateway_code is not None else None,
                str(gateway_message) if gateway_message is not None else None,
                str(ref_id) if ref_id is not None else None,
                str(authority),
            ),
        )
        return True


def mark_support_payment_paid(authority: str, *, ref_id=None, gateway_code=100, gateway_message="Verified"):
    """پرداخت را paid می‌کند و اگر به کاربر تلگرام وصل باشد، حامی را خودکار ثبت می‌کند."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, status FROM support_payments WHERE authority = ?",
            (str(authority),),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "paid":
            conn.execute(
                "UPDATE support_payments SET status = 'paid', ref_id = COALESCE(?, ref_id), "
                "gateway_code = ?, gateway_message = ?, paid_at = COALESCE(paid_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP WHERE authority = ?",
                (
                    str(ref_id) if ref_id is not None else None,
                    int(gateway_code) if gateway_code is not None else None,
                    str(gateway_message) if gateway_message is not None else None,
                    str(authority),
                ),
            )
        if row["user_id"] is not None:
            conn.execute(
                "INSERT OR IGNORE INTO supporters (user_id) VALUES (?)",
                (int(row["user_id"]),),
            )
        result = conn.execute(
            "SELECT * FROM support_payments WHERE authority = ?",
            (str(authority),),
        ).fetchone()
        return dict(result) if result else None


def list_support_payments(limit: int = 50):
    """برای گزارش مدیریتی/دیباگ؛ جدیدترین پرداخت‌ها را برمی‌گرداند."""
    safe_limit = max(1, min(int(limit), 500))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM support_payments ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# مهاجرت یک‌باره از فایل‌های JSON قدیمی
# ============================================================

def migrate_json_to_sqlite(
    data_dir: str = ".",
    courses_file: str = "courses_data.json",
    users_file: str = "users.json",
    subscribers_file: str = "subscribers.json",
    programs_file: str = "programs.json",
    admin_ids: list = None,
) -> dict:
    """
    اگه migration قبلاً انجام نشده، داده‌های JSON فعلی رو به SQLite منتقل می‌کنه.
    Idempotent: اگه یک بار انجام شده باشه (meta['migrated_v1'] == 'done')، دوباره کاری نمی‌کنه.
    خروجی: گزارش کوتاهی از تعداد رکوردهای منتقل‌شده (برای لاگ/دیباگ).
    """
    init_db()
    report = {"skipped": False}
    with get_conn() as conn:
        if _meta_get(conn, "migrated_v1") == "done":
            report["skipped"] = True
            return report

        # --- گروه پیش‌فرض برای ترم‌های قدیمی ---
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, 0)",
            (DEFAULT_GROUP_NAME,),
        )
        default_group_id = conn.execute(
            "SELECT id FROM groups WHERE name = ?", (DEFAULT_GROUP_NAME,)
        ).fetchone()["id"]

        # --- برنامه‌ها/ترم‌ها ---
        programs_path = os.path.join(data_dir, programs_file)
        program_names = []
        if os.path.exists(programs_path):
            with open(programs_path, "r", encoding="utf-8") as f:
                program_names = json.load(f)

        courses_path = os.path.join(data_dir, courses_file)
        courses_data = {}
        if os.path.exists(courses_path):
            with open(courses_path, "r", encoding="utf-8") as f:
                courses_data = json.load(f)

        # هر ترمی که یا توی programs.json یا توی courses_data.json هست باید ساخته بشه
        all_term_names = list(dict.fromkeys(list(program_names) + list(courses_data.keys())))

        term_ids = {}
        for i, term_name in enumerate(all_term_names):
            conn.execute(
                "INSERT OR IGNORE INTO terms (group_id, name, sort_order) VALUES (?, ?, ?)",
                (default_group_id, term_name, i),
            )
            row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
            term_ids[term_name] = row["id"]

        cat_ids = {
            r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")
        }

        courses_count = 0
        files_count = 0
        for term_name, courses in courses_data.items():
            term_id = term_ids[term_name]
            for c_i, (course_name, course) in enumerate(courses.items()):
                conn.execute(
                    "INSERT OR IGNORE INTO courses (term_id, name, guide, sort_order) "
                    "VALUES (?, ?, ?, ?)",
                    (term_id, course_name, course.get("guide", ""), c_i),
                )
                course_row = conn.execute(
                    "SELECT id FROM courses WHERE term_id = ? AND name = ?",
                    (term_id, course_name),
                ).fetchone()
                course_id = course_row["id"]
                courses_count += 1

                categories = course.get("categories", {})
                for cat_name, cat_data in categories.items():
                    if cat_name not in cat_ids:
                        conn.execute(
                            "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
                            (cat_name, len(cat_ids)),
                        )
                        cat_ids[cat_name] = conn.execute(
                            "SELECT id FROM categories WHERE name = ?", (cat_name,)
                        ).fetchone()["id"]
                    category_id = cat_ids[cat_name]

                    for f_i, f in enumerate(cat_data.get("files", [])):
                        fid = f.get("fid") or uuid.uuid4().hex[:8]
                        conn.execute(
                            """INSERT OR IGNORE INTO course_files
                               (course_id, category_id, fid, type, file_id, url,
                                caption, description, content, downloads, badge, sort_order)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                course_id,
                                category_id,
                                fid,
                                f.get("type", "file"),
                                f.get("file_id"),
                                f.get("url"),
                                f.get("caption", ""),
                                f.get("description", ""),
                                f.get("content"),
                                f.get("downloads", 0),
                                f.get("badge"),
                                f_i,
                            ),
                        )
                        files_count += 1

        # --- کاربران ---
        users_path = os.path.join(data_dir, users_file)
        users_count = 0
        if os.path.exists(users_path):
            with open(users_path, "r", encoding="utf-8") as f:
                users = json.load(f)
            for uid, u in users.items():
                conn.execute(
                    """INSERT OR IGNORE INTO users
                       (user_id, joined_channel, nickname_asked, nickname,
                        class_program, saved_fids)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        int(uid),
                        1 if u.get("joined_channel") else 0,
                        1 if u.get("nickname_asked") else 0,
                        u.get("nickname"),
                        u.get("class_program"),
                        json.dumps(u.get("saved_fids", []), ensure_ascii=False),
                    ),
                )
                users_count += 1

        # --- مشترکین ---
        subs_path = os.path.join(data_dir, subscribers_file)
        subs_count = 0
        if os.path.exists(subs_path):
            with open(subs_path, "r", encoding="utf-8") as f:
                subs = json.load(f)
            for uid in subs:
                conn.execute("INSERT OR IGNORE INTO subscribers (user_id) VALUES (?)", (int(uid),))
                subs_count += 1

        # --- ادمین‌های فعلی (ADMIN_IDS هاردکد) به‌عنوان SUPER_ADMIN بذر می‌شن ---
        admins_count = 0
        for admin_id in (admin_ids or []):
            conn.execute(
                "INSERT OR IGNORE INTO admins (user_id, role, term_id) VALUES (?, 'SUPER_ADMIN', NULL)",
                (admin_id,),
            )
            admins_count += 1

        _meta_set(conn, "migrated_v1", "done")

        report.update(
            terms=len(term_ids),
            courses=courses_count,
            files=files_count,
            users=users_count,
            subscribers=subs_count,
            admins_seeded=admins_count,
        )
        return report


# ============================================================
# توابع خواندن/نوشتن -- همون امضا و شکل خروجیِ نسخه‌ی JSON قبلی
# ============================================================

def get_program_names() -> list:
    """لیست تخت اسامی ترم‌ها (سازگار با نسخه‌ی قبلی -- فاز ۳ گروه‌بندی رو توی UI اضافه می‌کنه)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.name FROM terms t JOIN groups g ON t.group_id = g.id "
            "ORDER BY g.sort_order, t.sort_order"
        ).fetchall()
        return [r["name"] for r in rows]


def get_courses_with_ids(term_name: str) -> list:
    """[{'id': ..., 'name': ...}, ...] دروسِ یک ترم به ترتیب sort_order.
    برای جاهایی که نمی‌خوایم نام کامل درس رو توی callback_data تلگرام بذاریم
    (تلگرام callback_data رو به ۶۴ بایت UTF-8 محدود می‌کنه؛ نام دروسِ فارسیِ
    طولانی‌تر -- که توی خیلی از ترم‌ها معموله -- از این حد رد می‌شه و دکمه
    اصلاً ساخته نمی‌شه). id همیشه کوتاهه، پس امنه."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            "SELECT id, name FROM courses WHERE term_id = ? ORDER BY sort_order", (row["id"],)
        ).fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows]


def get_course_by_id(course_id: int):
    """{'id', 'name', 'term_id', 'term_name'} یا None. برای resolve کردن
    course_id (که توی callback_data کوتاه ذخیره شده) به نام واقعی درس/ترم."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.id, c.name, c.term_id, c.category_mode, t.name AS term_name "
            "FROM courses c JOIN terms t ON c.term_id = t.id WHERE c.id = ?",
            (course_id,),
        ).fetchone()
        return dict(row) if row else None


def get_courses_with_status(term_name: str) -> list:
    """مثلِ get_courses_with_ids ولی وضعیتِ کورسِ چرخشی رو هم برمی‌گردونه:
    [{'id', 'name', 'is_active', 'activated_at', 'finished_at'}, ...].
    برای نمایشِ 🟢 کنارِ کورسِ جاری و جداسازیِ آرشیوِ کورس‌های تمام‌شده در لیستِ
    دروسِ یک ترم -- بدونِ اینکه ساختارِ get_courses_with_ids قدیمی رو تغییر بده."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            "SELECT id, name, is_active_course, activated_at, finished_at FROM courses "
            "WHERE term_id = ? ORDER BY sort_order",
            (row["id"],),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "is_active": bool(r["is_active_course"]),
                "activated_at": r["activated_at"],
                "finished_at": r["finished_at"],
            }
            for r in rows
        ]


def get_active_course_id(term_name: str):
    """course_id ای که همین الان به‌عنوانِ کورسِ فعالِ این ترم علامت خورده، یا
    None اگه هیچ‌کدوم فعال نباشن (مثلاً ترمی که اصلاً کورس‌بندی نمی‌شه)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT co.id FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.is_active_course = 1",
            (term_name,),
        ).fetchone()
        return row["id"] if row else None


def get_course_activation_info(course_id: int):
    """{'is_active', 'activated_at', 'finished_at'} برای یه کورسِ خاص، یا None
    اگه پیدا نشد."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_active_course, activated_at, finished_at FROM courses WHERE id = ?",
            (course_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "is_active": bool(row["is_active_course"]),
            "activated_at": row["activated_at"],
            "finished_at": row["finished_at"],
        }


def set_active_course(course_id: int):
    """این کورس رو «فعال» علامت می‌زنه. اگه کورسِ دیگه‌ای توی همون ترم از قبل
    فعال بود، خودکار غیرفعال می‌شه و finished_at می‌گیره (یعنی همیشه حداکثر یک
    کورسِ فعال در هر ترم داریم). خروجی: {'previous': {'id','name'} یا None,
    'current': {'id','name'}, 'term_id'} -- برای پیام دادن به ادمین/دانشجوها.
    اگه course_id پیدا نشه None برمی‌گردونه."""
    now = _now()
    with get_conn() as conn:
        row = conn.execute("SELECT term_id FROM courses WHERE id = ?", (course_id,)).fetchone()
        if row is None:
            return None
        term_id = row["term_id"]
        prev = conn.execute(
            "SELECT id, name FROM courses WHERE term_id = ? AND is_active_course = 1 AND id != ?",
            (term_id, course_id),
        ).fetchone()
        prev_info = {"id": prev["id"], "name": prev["name"]} if prev is not None else None
        if prev is not None:
            conn.execute(
                "UPDATE courses SET is_active_course = 0, finished_at = ? WHERE id = ?",
                (now, prev["id"]),
            )
        conn.execute(
            "UPDATE courses SET is_active_course = 1, activated_at = ?, finished_at = NULL WHERE id = ?",
            (now, course_id),
        )
        new_row = conn.execute("SELECT id, name FROM courses WHERE id = ?", (course_id,)).fetchone()
        return {
            "previous": prev_info,
            "current": {"id": new_row["id"], "name": new_row["name"]},
            "term_id": term_id,
        }


def unset_active_course(course_id: int) -> bool:
    """نشانِ «فعال» رو بدونِ جایگزین‌کردنش با کورسِ دیگه‌ای برمی‌داره (finished_at
    هم نمی‌گیره -- چون این یه لغوِ دستیه، نه پایانِ طبیعیِ کورس). خروجی: پیدا شد/نشد."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE courses SET is_active_course = 0 WHERE id = ? AND is_active_course = 1",
            (course_id,),
        )
        return cur.rowcount > 0


def get_has_group_split(term_name: str, course_name: str) -> bool:
    """آیا این درس گروه‌بندیِ عملی/کلاسی داره؟ (همون منطقِ get_has_practical)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT co.has_group_split FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.name = ?",
            (term_name, course_name),
        ).fetchone()
        return bool(row["has_group_split"]) if row else False


def set_has_group_split(course_id: int, value: bool) -> bool:
    """فلگِ گروه‌بندی رو مستقیم با course_id ست می‌کنه (برخلافِ توابعِ مشابهِ
    قدیمی‌تر که با term_name+course_name کار می‌کردن -- این‌جا چون از پنلِ
    مدیریتِ درس، که همیشه course_id داره، صدا زده می‌شه، ساده‌تره)."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE courses SET has_group_split = ? WHERE id = ?", (1 if value else 0, course_id))
        return cur.rowcount > 0


def get_has_group_split_by_id(course_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT has_group_split FROM courses WHERE id = ?", (course_id,)).fetchone()
        return bool(row["has_group_split"]) if row else False


def add_course_group(course_id: int, name: str, day: str, start: str, end: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM course_groups WHERE course_id = ?", (course_id,)
        )
        next_order = cur.fetchone()[0]
        cur = conn.execute(
            "INSERT INTO course_groups (course_id, name, day, start, end, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
            (course_id, name, day, start, end, next_order),
        )
        return cur.lastrowid


def get_course_groups(course_id: int) -> list:
    """[{'id','name','day','start','end'}, ...] گروه‌های یه درس، به ترتیبِ ثبت."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, day, start, end FROM course_groups WHERE course_id = ? ORDER BY sort_order, id",
            (course_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_course_group_by_id(group_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, course_id, name, day, start, end FROM course_groups WHERE id = ?", (group_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_course_group(group_id: int) -> bool:
    """حذفِ یه گروه -- انتخاب‌های دانشجویانی که همین گروه رو زده بودن هم خودکار
    پاک می‌شه (ON DELETE CASCADE رو student_course_groups.group_id)، چون دیگه
    معنی نداره."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM course_groups WHERE id = ?", (group_id,))
        return cur.rowcount > 0


def set_student_course_group(user_id: int, course_id: int, group_id: int) -> None:
    """انتخاب/تغییرِ گروهِ یه دانشجو برای یه درسِ خاص. بدونِ قفل یا محدودیتِ
    ظرفیت -- هر وقت بخواد می‌تونه عوضش کنه."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO student_course_groups (user_id, course_id, group_id) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, course_id) DO UPDATE SET group_id = excluded.group_id",
            (user_id, course_id, group_id),
        )


def get_student_course_group(user_id: int, course_id: int):
    """group_id ای که این دانشجو برای این درس انتخاب کرده، یا None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT group_id FROM student_course_groups WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        ).fetchone()
        return row["group_id"] if row else None


def get_student_group_status(user_id: int, term_name: str) -> list:
    """برای منوی «👥 گروه‌های عملیِ من»: لیستِ درس‌های گروه‌بندی‌شده‌ی این ترم،
    هرکدوم با گروه‌هاش و گروهی که این کاربر (اگه انتخاب کرده) توشه.
    [{'course_id','course_name','groups':[{'id','name','day','start','end'}],
      'selected_group_id': ... یا None}]"""
    with get_conn() as conn:
        term_row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
        if term_row is None:
            return []
        courses = conn.execute(
            "SELECT id, name FROM courses WHERE term_id = ? AND has_group_split = 1 ORDER BY sort_order",
            (term_row["id"],),
        ).fetchall()
        result = []
        for c in courses:
            groups = conn.execute(
                "SELECT id, name, day, start, end FROM course_groups WHERE course_id = ? ORDER BY sort_order, id",
                (c["id"],),
            ).fetchall()
            sel = conn.execute(
                "SELECT group_id FROM student_course_groups WHERE user_id = ? AND course_id = ?",
                (user_id, c["id"]),
            ).fetchone()
            result.append(
                {
                    "course_id": c["id"],
                    "course_name": c["name"],
                    "groups": [dict(g) for g in groups],
                    "selected_group_id": sel["group_id"] if sel else None,
                }
            )
        return result


def get_has_practical(term_name: str, course_name: str) -> bool:
    """آیا این درس بخشِ «🧪 عملی» داره؟ (بدون لود کردن کل درخت درس‌ها -- برای دکمه‌سازی سریع)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT co.has_practical FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.name = ?",
            (term_name, course_name),
        ).fetchone()
        return bool(row["has_practical"]) if row else False


def set_has_practical(term_name: str, course_name: str, value: bool) -> bool:
    """فلگ «🧪 عملی» رو مستقیم روی یه درس ست می‌کنه. خروجی: پیدا شد/نشد."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE courses SET has_practical = ? "
            "WHERE term_id = (SELECT id FROM terms WHERE name = ?) AND name = ?",
            (1 if value else 0, term_name, course_name),
        )
        return cur.rowcount > 0


def get_has_lesson_plan(term_name: str, course_name: str) -> bool:
    """آیا این درس بخشِ «📋 طرح درس» داره؟ (همون منطقِ get_has_practical)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT co.has_lesson_plan FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.name = ?",
            (term_name, course_name),
        ).fetchone()
        return bool(row["has_lesson_plan"]) if row else False


def set_has_lesson_plan(term_name: str, course_name: str, value: bool) -> bool:
    """فلگِ «📋 طرح درس» رو مستقیم روی یه درس ست می‌کنه. خروجی: پیدا شد/نشد."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE courses SET has_lesson_plan = ? "
            "WHERE term_id = (SELECT id FROM terms WHERE name = ?) AND name = ?",
            (1 if value else 0, term_name, course_name),
        )
        return cur.rowcount > 0


def get_single_category(term_name: str, course_name: str):
    """اگه این درس روی «تک‌دسته‌ای» تنظیم شده باشه، اسمِ همون یه دسته رو برمی‌گردونه
    (وگرنه None -- یعنی رفتار عادی با همه‌ی دسته‌بندی‌ها)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT co.single_category FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.name = ?",
            (term_name, course_name),
        ).fetchone()
        return row["single_category"] if row else None


def set_single_category(term_name: str, course_name: str, category_name):
    """درس رو «تک‌دسته‌ای» می‌کنه (فقط category_name نشون داده می‌شه، نه هر ۱۱ تا).
    برای برگردوندن به حالت عادی، category_name رو None بده. خروجی: پیدا شد/نشد."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE courses SET single_category = ? "
            "WHERE term_id = (SELECT id FROM terms WHERE name = ?) AND name = ?",
            (category_name, term_name, course_name),
        )
        return cur.rowcount > 0


def get_course_id_by_name(term_name: str, course_name: str):
    """id درس، فقط بر اساسِ نامِ ترم+درس (بدونِ نیاز به لودِ کلِ درخت). خروجی None
    اگه پیدا نشد. مکملِ get_course_by_id (که برعکس، از id به نام می‌رسه)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT co.id FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.name = ?",
            (term_name, course_name),
        ).fetchone()
        return row["id"] if row else None


def get_course_mode(course_id: int) -> str:
    """'standard' یا 'custom'. 'standard' یعنی ۱۱ دسته‌ی معمولِ MedVerse + منطقِ
    عملی/طرح‌درس/منابع (رفتارِ قبلی، دست‌نخورده). 'custom' یعنی این درس کاملاً
    database-driven عمل می‌کنه: هر دسته‌ای که واقعاً برای این course_id در جدولِ
    categories باشه -- با هر اسمی، هر تعداد -- دقیقاً همون‌جوری نمایش داده می‌شه،
    بدونِ فیلترِ CATEGORIES/عملی/طرح‌درس/منابعِ سراسری. برای ترم‌های «رفرنس» (یا هر
    ساختارِ کاملاً سفارشیِ دیگه‌ای) استفاده می‌شه."""
    with get_conn() as conn:
        row = conn.execute("SELECT category_mode FROM courses WHERE id = ?", (course_id,)).fetchone()
        return (row["category_mode"] if row and row["category_mode"] else "standard")


def set_course_mode(course_id: int, mode: str) -> bool:
    """حالتِ یک درس رو بینِ 'standard' و 'custom' عوض می‌کنه. تغییرِ حالت هیچ
    دسته/فایلی رو دست‌نمی‌زنه -- فقط تعیین می‌کنه کدوم منطقِ نمایش/فیلتر روی
    دسته‌های *موجودِ* همین درس اعمال بشه."""
    if mode not in ("standard", "custom"):
        return False
    with get_conn() as conn:
        cur = conn.execute("UPDATE courses SET category_mode = ? WHERE id = ?", (mode, course_id))
        return cur.rowcount > 0


def rename_course_by_id(course_id: int, new_name: str):
    """اسمِ یه درس رو مستقیماً با UPDATE عوض می‌کنه (بدون دست‌زدن به id درس یا فایل‌هاش).
    این تابعِ امنه -- بر خلاف رفتنِ از مسیرِ load_data()/save_data() که چون درس‌ها رو
    بر اساس اسم شناسایی می‌کنه، تغییرِ اسم رو معادلِ «درسِ جدید» می‌بینه و باعث
    تکرار/تصادمِ fid فایل‌ها می‌شه. خروجی: (ok: bool, message: str)."""
    with get_conn() as conn:
        row = conn.execute("SELECT term_id, name FROM courses WHERE id = ?", (course_id,)).fetchone()
        if row is None:
            return False, "این درس دیگه وجود نداره."
        if not new_name.strip():
            return False, "اسم خالی قابل قبول نیست."
        clash = conn.execute(
            "SELECT id FROM courses WHERE term_id = ? AND name = ? AND id != ?",
            (row["term_id"], new_name, course_id),
        ).fetchone()
        if clash is not None:
            return False, "درسی با این اسم از قبل توی همین ترم وجود داره."
        conn.execute("UPDATE courses SET name = ? WHERE id = ?", (new_name, course_id))
        return True, row["name"]  # اسم قبلی رو برمی‌گردونه (برای پیام/لاگ)


def delete_course_by_id(course_id: int):
    """درس رو با شناسه‌ش (نه اسم) به همراه همه‌ی فایل‌هاش حذف می‌کنه (cascade خودکار).
    خروجی: (ok: bool, course_name یا None, term_name یا None) -- برای پیام/لاگ."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.name, t.name AS term_name FROM courses c JOIN terms t ON c.term_id = t.id WHERE c.id = ?",
            (course_id,),
        ).fetchone()
        if row is None:
            return False, None, None
        conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
        return True, row["name"], row["term_name"]


def count_course_files_by_id(course_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM course_files WHERE course_id = ?", (course_id,)).fetchone()
        return row["n"] if row else 0


# ---------------------------------------------------------------------------
# مدیریتِ ترم (ویرایش نام / حذف) -- همون فلسفه‌ی id-based بالا (rename_course_by_id/
# delete_course_by_id) که با UPDATE/DELETE مستقیم روی id کار می‌کنه، نه از مسیرِ
# load_data()/save_data() که چون بر اساس اسم شناسایی می‌کنه، تغییرِ اسم رو
# معادلِ «ترمِ جدید» می‌بینه و باعثِ ساختِ ترمِ تکراری/گم‌شدنِ محتوا می‌شه.
# ---------------------------------------------------------------------------

def get_terms_with_ids() -> list:
    """[{'id', 'name', 'group_name'}, ...] همه‌ی ترم‌ها، به ترتیبِ گروه و sort_order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, g.name AS group_name FROM terms t JOIN groups g ON t.group_id = g.id "
            "ORDER BY g.sort_order, t.sort_order"
        ).fetchall()
        return [dict(r) for r in rows]


def get_term_by_id(term_id: int):
    """{'id', 'name', 'group_id', 'group_name'} یا None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT t.id, t.name, t.group_id, g.name AS group_name FROM terms t "
            "JOIN groups g ON t.group_id = g.id WHERE t.id = ?",
            (term_id,),
        ).fetchone()
        return dict(row) if row else None


_TERM_PERIOD_FIELDS = ("class_period_start", "class_period_end", "final_exam_start", "final_exam_end")


def get_term_periods(term_name: str):
    """{'class_period_start','class_period_end','final_exam_start','final_exam_end'}
    برای یه ترم -- هرکدوم می‌تونه None باشه (یعنی هنوز ثبت نشده). این‌ها متنِ
    آزادن (نه تاریخِ واقعی)، دقیقاً مثلِ exam_date."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT class_period_start, class_period_end, final_exam_start, final_exam_end "
            "FROM terms WHERE name = ?",
            (term_name,),
        ).fetchone()
        if row is None:
            return None
        return {f: row[f] for f in _TERM_PERIOD_FIELDS}


def set_term_period(term_name: str, field: str, value: str) -> bool:
    """یکی از ۴ فیلدِ بازه‌ی زمانیِ ترم رو ست می‌کنه. value خالی/None یعنی پاک‌کردن."""
    if field not in _TERM_PERIOD_FIELDS:
        raise ValueError(f"فیلدِ بازه‌ی زمانیِ نامعتبر: {field}")
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE terms SET {field} = ? WHERE name = ?", (value or None, term_name))
        return cur.rowcount > 0


def get_term_id_by_name(term_name: str):
    """id ترم فقط بر اساسِ اسمش (مکملِ get_term_by_id که برعکس عمل می‌کنه -- از id
    به رکورد می‌رسه). خروجی None اگه پیدا نشد. برای resolve کردنِ نامِ ترم به یه
    شناسه‌ی کوتاه قبل از گذاشتنش توی callback_data تلگرام (که سقفِ ۶۴ بایت داره
    و اسم‌های فارسیِ ترم می‌تونن این سقف رو رد کنن)."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
        return row["id"] if row else None


def rename_term_by_id(term_id: int, new_name: str):
    """اسمِ یه ترم رو مستقیماً با UPDATE عوض می‌کنه (بدون دست‌زدن به id ترم، درس‌ها یا فایل‌هاش).
    خروجی: (ok: bool, message_or_old_name: str)."""
    new_name = new_name.strip()
    with get_conn() as conn:
        row = conn.execute("SELECT name, group_id FROM terms WHERE id = ?", (term_id,)).fetchone()
        if row is None:
            return False, "این ترم دیگه وجود نداره."
        if not new_name:
            return False, "اسم خالی قابل قبول نیست."
        if new_name == row["name"]:
            return False, "این که همون اسمِ فعلیه."
        # فاز ۱۰: یکتاییِ اسمِ ترم فقط در سطحِ همین گروهه، نه کلِ بات -- یعنی می‌شه
        # مثلاً هم توی «📚 علوم پایه» و هم توی «🩺 فیزیوپات» ترمی به نامِ «رفرنس» داشت.
        clash = conn.execute(
            "SELECT id FROM terms WHERE group_id = ? AND name = ? AND id != ?",
            (row["group_id"], new_name, term_id),
        ).fetchone()
        if clash is not None:
            return False, "ترمی با این اسم از قبل توی همین گروه وجود داره."
        conn.execute("UPDATE terms SET name = ? WHERE id = ?", (new_name, term_id))
        return True, row["name"]  # اسم قبلی رو برمی‌گردونه (برای پیام/لاگ)


def delete_term_by_id(term_id: int):
    """ترم رو با شناسه‌ش، به همراه همه‌ی درس‌ها/فایل‌هاش (cascade خودکار)، حذف می‌کنه.
    خروجی: (ok: bool, term_name یا None)."""
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM terms WHERE id = ?", (term_id,)).fetchone()
        if row is None:
            return False, None
        conn.execute("DELETE FROM terms WHERE id = ?", (term_id,))
        return True, row["name"]


def count_term_courses_by_id(term_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM courses WHERE term_id = ?", (term_id,)).fetchone()
        return row["n"] if row else 0


def count_term_files_by_id(term_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM course_files cf JOIN courses c ON cf.course_id = c.id "
            "WHERE c.term_id = ?",
            (term_id,),
        ).fetchone()
        return row["n"] if row else 0


# ---------------------------------------------------------------------------
# فاز ۷ -- گروه‌های زیرِ دسته‌بندی + نوت
#
# این توابع عمداً id-based هستن، نه dict-diffing مثل save_data؛ همون فلسفه‌ای
# که rename_course_by_id/delete_course_by_id دنبال می‌کنن (نگاه کن به کامنتِ
# بالای save_data). یک گروه، لایه‌ای زیرِ (course_id, category_id) هست؛ حذفِ یک
# گروه هیچ‌وقت فایل/نوت داخلش رو حذف نمی‌کنه -- فقط group_id اون‌ها NULL می‌شه.
# ---------------------------------------------------------------------------

def get_category_id_for_course(course_id: int, name: str):
    """id ردیفِ categories که برای همین course_id مشخص این اسم رو داره -- جایگزینِ
    id-safe برای get_category_id_by_name قدیمی. چون از migrate_categories_per_course_v3
    به بعد هر درس دسته‌های مستقلِ خودش رو با id جدا داره (حتی اگه اسمِ دو دسته توی دو
    درسِ مختلف یکی باشه)، این تابع همیشه باید با course_id فراخوانی بشه، نه فقط اسم --
    وگرنه ممکنه دسته‌ی درسِ اشتباهی برگرده."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM categories WHERE course_id = ? AND name = ?", (course_id, name)
        ).fetchone()
        return row["id"] if row else None


def get_category_by_id(category_id: int):
    """{'id','course_id','name','sort_order'} یا None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        return dict(row) if row else None


def get_categories_for_course(course_id: int) -> list:
    """لیستِ دسته‌های یک درسِ خاص، به ترتیبِ sort_order -- برای پنلِ مدیریتِ دسته‌های هر درس."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, sort_order FROM categories WHERE course_id = ? ORDER BY sort_order, id",
            (course_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_category_names(term_name: str, course_name: str):
    """اسمِ دسته‌هایی که *واقعاً* در دیتابیس برای این درسِ خاص وجود دارن (یعنی
    ادمین از طریق /editcategories حذفشون نکرده). برای فیلترکردنِ منوهای
    آپلود/جابه‌جایی/تغییرنام (که با ایندکسِ CATEGORIES سراسری کار می‌کنن) استفاده
    می‌شه -- تا دسته‌ای که عمداً حذف شده، دوباره به‌عنوان مقصدِ آپلود قابل‌انتخاب
    نباشه (وگرنه اولین آپلودِ بعدی همون دسته‌ی «حذف‌شده» رو دوباره در دیتابیس
    می‌سازه و انگار اصلاً حذف نشده بوده).
    خروجی: set از اسم‌ها، یا None اگه خودِ درس پیدا نشد (یعنی چک نشدنیه --
    فراخوان باید با فرضِ «همه چی مجازه» رفتار کنه، نه اینکه همه چی رو مخفی کنه)."""
    with get_conn() as conn:
        course_row = conn.execute(
            "SELECT co.id FROM courses co JOIN terms t ON co.term_id = t.id "
            "WHERE t.name = ? AND co.name = ?",
            (term_name, course_name),
        ).fetchone()
        if course_row is None:
            return None
        rows = conn.execute(
            "SELECT name FROM categories WHERE course_id = ?", (course_row["id"],)
        ).fetchall()
        return {r["name"] for r in rows}


def add_category(course_id: int, name: str, sort_order: int = None):
    """دسته‌ی جدید برای یک درسِ مشخص می‌سازه. اگه هم‌نام از قبل توی همین درس بود،
    None برمی‌گردونه (برخلافِ add_file_group که silently همون id رو برمی‌گردونه --
    اینجا عمداً متفاوته چون «افزودن دسته‌ی تکراری» باید به‌وضوح fail بشه، نه بی‌صدا نو-اپ بشه)."""
    name = (name or "").strip()
    if not name:
        return None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM categories WHERE course_id = ? AND name = ?", (course_id, name)
        ).fetchone()
        if existing:
            return None
        if sort_order is None:
            sort_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS m FROM categories WHERE course_id = ?",
                (course_id,),
            ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO categories (course_id, name, sort_order) VALUES (?, ?, ?)",
            (course_id, name, sort_order),
        )
        return cur.lastrowid


def rename_category(category_id: int, new_name: str) -> tuple:
    """اسمِ یک دسته رو با id عوض می‌کنه -- فقط روی همین یک دسته اثر می‌ذاره، حتی اگه
    دسته‌ی هم‌نام توی درس‌های دیگه هم وجود داشته باشه (چون id مستقلن).
    خروجی: (ok: bool, message_or_old_name: str)."""
    new_name = (new_name or "").strip()
    if not new_name:
        return False, "اسم خالی قابل قبول نیست."
    with get_conn() as conn:
        row = conn.execute("SELECT course_id, name FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            return False, "این دسته دیگه وجود نداره."
        if new_name == row["name"]:
            return False, "این که همون اسمِ فعلیه."
        dup = conn.execute(
            "SELECT id FROM categories WHERE course_id = ? AND name = ? AND id != ?",
            (row["course_id"], new_name, category_id),
        ).fetchone()
        if dup:
            return False, "این درس از قبل دسته‌ای با این اسم داره."
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, category_id))
        return True, row["name"]


def delete_category(category_id: int, force: bool = False) -> dict:
    """یک دسته رو با id حذف می‌کنه -- فقط همین دسته، فقط توی همین درس.
    چون category_id توی course_files ستونِ NOT NULL هست (برخلافِ group_id که می‌شه
    NULL کرد)، پیش‌فرض این تابع اینه که اگه دسته فایل/زیردسته داشته باشه، از حذف
    امتناع کنه (force=False) تا محتوا گم نشه؛ ادمین باید صریحاً force=True بده یا
    اول محتوا رو جابه‌جا/حذف کنه."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        file_count = conn.execute(
            "SELECT COUNT(*) AS n FROM course_files WHERE category_id = ?", (category_id,)
        ).fetchone()["n"]
        group_count = conn.execute(
            "SELECT COUNT(*) AS n FROM course_file_groups WHERE category_id = ?", (category_id,)
        ).fetchone()["n"]
        if (file_count > 0 or group_count > 0) and not force:
            return {"ok": False, "reason": "not_empty", "files": file_count, "groups": group_count}
        if force:
            conn.execute("DELETE FROM course_files WHERE category_id = ?", (category_id,))
            conn.execute("DELETE FROM course_file_groups WHERE category_id = ?", (category_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        return {"ok": True, "deleted_files": file_count if force else 0, "deleted_groups": group_count if force else 0}


def delete_categories_bulk(course_id: int, category_ids: list, force: bool = False) -> dict:
    """چند تا دسته رو هم‌زمان حذف می‌کنه -- ولی *فقط* اگه همه‌شون واقعاً متعلق به
    همین course_id باشن؛ هر category_id که به این درس تعلق نداشته باشه (مثلاً به‌خاطرِ
    داده‌ی دستکاری‌شده یا callback قدیمی) به‌طور کامل نادیده گرفته می‌شه و اصلاً لمس
    نمی‌شه -- یعنی هیچ درس/ترمِ دیگه‌ای، حتی در صورت اشتباه در ورودی، آسیب نمی‌بینه.
    مثلِ delete_category تک‌تایی: force=False فقط پیش‌نمایشِ جمعِ فایل/زیردسته‌ی همه‌ی
    دسته‌های معتبر رو برمی‌گردونه (بدونِ حذف)؛ force=True همه رو تراکنشی حذف می‌کنه.
    خروجی همیشه شاملِ 'skipped_ids' هست (id هایی که رد شدن چون به این درس تعلق نداشتن)."""
    with get_conn() as conn:
        valid_rows = conn.execute(
            f"SELECT id, name FROM categories WHERE course_id = ? AND id IN "
            f"({','.join('?' * len(category_ids))})",
            (course_id, *category_ids),
        ).fetchall() if category_ids else []
        valid_ids = [r["id"] for r in valid_rows]
        names_by_id = {r["id"]: r["name"] for r in valid_rows}
        skipped_ids = [cid for cid in category_ids if cid not in valid_ids]

        if not valid_ids:
            return {"ok": False, "reason": "no_valid_categories", "skipped_ids": skipped_ids}

        placeholders = ",".join("?" * len(valid_ids))
        file_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM course_files WHERE category_id IN ({placeholders})", valid_ids
        ).fetchone()["n"]
        group_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM course_file_groups WHERE category_id IN ({placeholders})", valid_ids
        ).fetchone()["n"]

        if (file_count > 0 or group_count > 0) and not force:
            return {
                "ok": False,
                "reason": "not_empty",
                "files": file_count,
                "groups": group_count,
                "category_names": [names_by_id[i] for i in valid_ids],
                "skipped_ids": skipped_ids,
            }

        if force:
            conn.execute(f"DELETE FROM course_files WHERE category_id IN ({placeholders})", valid_ids)
            conn.execute(f"DELETE FROM course_file_groups WHERE category_id IN ({placeholders})", valid_ids)
        conn.execute(f"DELETE FROM categories WHERE id IN ({placeholders})", valid_ids)
        return {
            "ok": True,
            "deleted_categories": [names_by_id[i] for i in valid_ids],
            "deleted_files": file_count if force else 0,
            "deleted_groups": group_count if force else 0,
            "skipped_ids": skipped_ids,
        }


def copy_category(source_category_id: int, target_course_id: int, new_name: str = None):
    """تعریفِ یک دسته (فقط اسم -- نه فایل‌هاش) رو از یک درس به درسِ دیگه کپی می‌کنه؛
    برای ساختِ سریعِ دسته‌ی جدید بر اساسِ الگوی یه درسِ دیگه. اگه هم‌نام از قبل توی
    درسِ مقصد بود، همون id موجود رو برمی‌گردونه (بدونِ ساختِ تکراری)."""
    with get_conn() as conn:
        src = conn.execute("SELECT name FROM categories WHERE id = ?", (source_category_id,)).fetchone()
        if src is None:
            return None
        name = (new_name or src["name"]).strip()
        if not name:
            return None
        existing = conn.execute(
            "SELECT id FROM categories WHERE course_id = ? AND name = ?", (target_course_id, name)
        ).fetchone()
        if existing:
            return existing["id"]
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM categories WHERE course_id = ?",
            (target_course_id,),
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO categories (course_id, name, sort_order) VALUES (?, ?, ?)",
            (target_course_id, name, max_order + 1),
        )
        return cur.lastrowid


def move_category_order(category_id: int, direction: str) -> bool:
    """ترتیبِ نمایشِ یک دسته رو با همسایه‌ی بالا/پایینش (توی همون درس) جابه‌جا می‌کنه."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            return False
        if direction == "up":
            neighbor = conn.execute(
                "SELECT * FROM categories WHERE course_id = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1",
                (row["course_id"], row["sort_order"]),
            ).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT * FROM categories WHERE course_id = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                (row["course_id"], row["sort_order"]),
            ).fetchone()
        if neighbor is None:
            return False
        conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (neighbor["sort_order"], row["id"]))
        conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (row["sort_order"], neighbor["id"]))
        return True


# ---------------------------------------------------------------------------
# فاز ۹ -- مدیریتِ خودِ «بخش»‌ها (groups) با id -- بدونِ این‌ها، ساختنِ بخشِ جدید
# فقط از طریقِ INSERT دستی توی seed_group_structure ممکن بود. الگو دقیقاً مثلِ
# rename_term_by_id/delete_term_by_id (فاز قبل): id-based، نه اسم-based.
# ---------------------------------------------------------------------------

def get_groups_with_ids() -> list:
    """[{'id','name','sort_order'}, ...] همه‌ی بخش‌ها، به ترتیبِ sort_order."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, sort_order FROM groups ORDER BY sort_order, id").fetchall()
        return [dict(r) for r in rows]


def get_group_by_id(group_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT id, name, sort_order FROM groups WHERE id = ?", (group_id,)).fetchone()
        return dict(row) if row else None


def add_group(name: str) -> int:
    """بخشِ جدید می‌سازه (مثلاً «کارآموزی»، «کارورزی»). اگه هم‌نام از قبل بود، None برمی‌گردونه."""
    name = (name or "").strip()
    if not name:
        return None
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()
        if existing:
            return None
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM groups").fetchone()["m"]
        cur = conn.execute("INSERT INTO groups (name, sort_order) VALUES (?, ?)", (name, max_order + 1))
        return cur.lastrowid


def rename_group_by_id(group_id: int, new_name: str) -> tuple:
    """خروجی: (ok: bool, message_or_old_name: str)."""
    new_name = (new_name or "").strip()
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            return False, "این بخش دیگه وجود نداره."
        if not new_name:
            return False, "اسم خالی قابل قبول نیست."
        if new_name == row["name"]:
            return False, "این که همون اسمِ فعلیه."
        clash = conn.execute("SELECT id FROM groups WHERE name = ? AND id != ?", (new_name, group_id)).fetchone()
        if clash is not None:
            return False, "بخشی با این اسم از قبل وجود داره."
        conn.execute("UPDATE groups SET name = ? WHERE id = ?", (new_name, group_id))
        return True, row["name"]


def delete_group_by_id(group_id: int) -> tuple:
    """بخش رو با id، به همراه همه‌ی ترم‌ها/درس‌ها/فایل‌هاش (cascade خودکار)، حذف می‌کنه.
    خروجی: (ok: bool, group_name یا None)."""
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            return False, None
        conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return True, row["name"]


def count_group_terms_by_id(group_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM terms WHERE group_id = ?", (group_id,)).fetchone()
        return row["n"] if row else 0


def get_file_groups(course_id: int, category_id: int) -> list:
    """لیستِ گروه‌های یک درس+دسته‌بندی، به ترتیبِ sort_order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, sort_order FROM course_file_groups "
            "WHERE course_id = ? AND category_id = ? ORDER BY sort_order, id",
            (course_id, category_id),
        ).fetchall()
        return [dict(r) for r in rows]


def get_file_group(group_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM course_file_groups WHERE id = ?", (group_id,)).fetchone()
        return dict(row) if row else None


def add_file_group(course_id: int, category_id: int, name: str):
    """گروهِ جدید اضافه می‌کنه. اگه هم‌نام از قبل بود، همون id رو برمی‌گردونه (بدونِ تکراری‌سازی)."""
    name = (name or "").strip()
    if not name:
        return None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM course_file_groups WHERE course_id=? AND category_id=? AND name=?",
            (course_id, category_id, name),
        ).fetchone()
        if existing:
            return existing["id"]
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM course_file_groups "
            "WHERE course_id=? AND category_id=?",
            (course_id, category_id),
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO course_file_groups (course_id, category_id, name, sort_order) VALUES (?, ?, ?, ?)",
            (course_id, category_id, name, max_order + 1),
        )
        return cur.lastrowid


def rename_file_group(group_id: int, new_name: str) -> bool:
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT course_id, category_id FROM course_file_groups WHERE id=?", (group_id,)
        ).fetchone()
        if row is None:
            return False
        dup = conn.execute(
            "SELECT id FROM course_file_groups WHERE course_id=? AND category_id=? AND name=? AND id!=?",
            (row["course_id"], row["category_id"], new_name, group_id),
        ).fetchone()
        if dup:
            return False
        conn.execute("UPDATE course_file_groups SET name=? WHERE id=?", (new_name, group_id))
        return True


def delete_file_group(group_id: int) -> bool:
    """گروه رو حذف می‌کنه. فایل/نوتِ داخلش حذف نمی‌شه -- فقط بدون‌گروه (group_id=NULL) می‌شه."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM course_file_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE course_files SET group_id=NULL WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM course_file_groups WHERE id=?", (group_id,))
        return True


def move_file_group_order(group_id: int, direction: str) -> bool:
    """ترتیبِ یک گروه رو با همسایه‌ی بالا/پایینش جابه‌جا می‌کنه. direction: 'up' یا 'down'."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM course_file_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            return False
        if direction == "up":
            neighbor = conn.execute(
                "SELECT * FROM course_file_groups WHERE course_id=? AND category_id=? AND sort_order < ? "
                "ORDER BY sort_order DESC LIMIT 1",
                (row["course_id"], row["category_id"], row["sort_order"]),
            ).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT * FROM course_file_groups WHERE course_id=? AND category_id=? AND sort_order > ? "
                "ORDER BY sort_order ASC LIMIT 1",
                (row["course_id"], row["category_id"], row["sort_order"]),
            ).fetchone()
        if neighbor is None:
            return False
        conn.execute("UPDATE course_file_groups SET sort_order=? WHERE id=?", (neighbor["sort_order"], row["id"]))
        conn.execute("UPDATE course_file_groups SET sort_order=? WHERE id=?", (row["sort_order"], neighbor["id"]))
        return True


def get_group_content(course_id: int, category_id: int, group_id) -> list:
    """فایل‌ها/نوت‌های یک گروهِ خاص (group_id=None یعنی موارد بدون‌گروه)، به ترتیبِ sort_order."""
    with get_conn() as conn:
        if group_id is None:
            rows = conn.execute(
                "SELECT * FROM course_files WHERE course_id=? AND category_id=? AND group_id IS NULL "
                "ORDER BY sort_order, id",
                (course_id, category_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM course_files WHERE course_id=? AND category_id=? AND group_id=? "
                "ORDER BY sort_order, id",
                (course_id, category_id, group_id),
            ).fetchall()
        return [dict(r) for r in rows]


def move_file_to_group(fid: str, group_id) -> bool:
    """فایل/نوتِ موجود (با fid) رو به گروهِ دیگه منتقل می‌کنه (group_id=None یعنی بدون‌گروه).
    فقط ستونِ group_id عوض می‌شه -- نه فایلِ تلگرامی دوباره آپلود می‌شه، نه ردیف حذف/بازساخت می‌شه."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM course_files WHERE fid=?", (fid,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE course_files SET group_id=? WHERE fid=?", (group_id, fid))
        return True


def add_note(course_id: int, category_id: int, group_id, content: str, caption: str = "") -> str:
    """نوتِ متنیِ جدید (type='note') اضافه می‌کنه و fid یکتاش رو برمی‌گردونه."""
    fid = uuid.uuid4().hex[:8]
    with get_conn() as conn:
        if group_id is None:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) AS m FROM course_files "
                "WHERE course_id=? AND category_id=? AND group_id IS NULL",
                (course_id, category_id),
            ).fetchone()["m"]
        else:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) AS m FROM course_files "
                "WHERE course_id=? AND category_id=? AND group_id=?",
                (course_id, category_id, group_id),
            ).fetchone()["m"]
        conn.execute(
            """INSERT INTO course_files
               (course_id, category_id, group_id, fid, type, caption, content, downloads, sort_order)
               VALUES (?, ?, ?, ?, 'note', ?, ?, 0, ?)""",
            (course_id, category_id, group_id, fid, caption or "", content, max_order + 1),
        )
    return fid


def edit_note(fid: str, content: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM course_files WHERE fid=? AND type='note'", (fid,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE course_files SET content=? WHERE fid=?", (content, fid))
        return True


def edit_file_description(fid: str, description: str) -> bool:
    """توضیحِ (کپشنِ تلگرامیِ) یه فایلِ آپلودی رو ویرایش می‌کنه -- فقط ستونِ description
    عوض می‌شه؛ خودِ فایل روی تلگرام و caption (اسمِ نمایشی) دست‌نخورده می‌مونن (برخلافِ
    rename_file که آپلودِ مجدد لازم داره). دقیقاً همون فلسفه‌ی id-based مثلِ edit_note."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM course_files WHERE fid=? AND type='file'", (fid,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE course_files SET description=? WHERE fid=?", (description, fid))
        return True


def delete_note(fid: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM course_files WHERE fid=? AND type='note'", (fid,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM course_files WHERE fid=?", (fid,))
        return True


def add_program(name: str, group_name: str = PHYSIOPATH_GROUP_NAME) -> bool:
    """ترم جدید اضافه می‌کنه. اگه از قبل بود، False برمی‌گردونه.

    نکته‌ی مهم: این چک عمداً همچنان *سراسریه* (نه فقط داخل همون گروه)، با این‌که
    از migrate_terms_unique_per_group_v4 به بعد خودِ جدولِ terms این اجازه رو
    می‌ده که دو ترمِ هم‌نام در گروه‌های مختلف وجود داشته باشن. دلیلش اینه که
    load_data() (و بعد از اون تقریباً کل bot.py -- categories_for، get_has_practical،
    can_manage_term، و غیره) درختِ درس‌ها رو با یک دیکشنریِ تخت و کلیدشده با *فقط
    اسمِ ترم* نمایش می‌ده؛ اگه اینجا اجازه‌ی هم‌نامی داده بشه، دومین ترمِ هم‌نام
    توی همون دیکشنری جایگزینِ اولی می‌شه و درس‌ها/فایل‌های زیرِ اولی عملاً از دسترسِ
    بخشِ بزرگی از بات خارج می‌شن (بدونِ حذفِ واقعی از دیتابیس -- فقط دیگه از این
    مسیرها قابل‌دیدن نیستن). برای اجازه‌دادنِ واقعی و امنِ هم‌نامی در سطحِ کلِ بات،
    این تابع *و* همه‌ی توابعی که یک ترم رو صرفاً با نام (نه id) پیدا می‌کنن باید به
    شناساییِ id-based تبدیل بشن -- یه ریفکتورِ جدا و بزرگ‌تر از اسکوپِ همین فاز.
    فعلاً برای دو تا ساختارِ «رفرنس» زیرِ گروه‌های مختلف، از دو اسمِ متمایز
    (مثلاً «رفرنس (علوم پایه)» و «رفرنس (فیزیوپات)») استفاده کن -- کاملاً امن
    و بدونِ هیچ محدودیتی کار می‌کنه، چون این تابع فقط جلوی *هم‌نامیِ دقیق* رو می‌گیره."""
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM terms WHERE name = ?", (name,)).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, "
            "(SELECT COALESCE(MAX(sort_order), -1) + 1 FROM groups))",
            (group_name,),
        )
        group_id = conn.execute("SELECT id FROM groups WHERE name = ?", (group_name,)).fetchone()["id"]
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM terms WHERE group_id = ?", (group_id,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO terms (group_id, name, sort_order) VALUES (?, ?, ?)",
            (group_id, name, next_order),
        )
        return True


def load_data() -> dict:
    """کل درخت درس‌ها رو به همون شکل dict قدیمی برمی‌گردونه:
    {ترم: {درس: {guide, categories: {دسته: {files: [...]}}}}}

    نکته‌ی مهم (migrate_categories_per_course_v3 به بعد): دیکشنریِ cats برای هر درس
    جداگانه، فقط از رویِ دسته‌های خودِ همون course_id ساخته می‌شه -- نه از یک لیستِ
    سراسریِ اسم‌ها. یعنی حتی اگه دو درسِ مختلف دسته‌ای هم‌نام داشته باشن، هیچ اختلاطی
    بینشون پیش نمی‌آد چون هرکدوم id و ردیفِ جداگانه‌ی خودشون رو دارن."""
    with get_conn() as conn:
        terms = conn.execute(
            "SELECT t.id, t.name FROM terms t JOIN groups g ON t.group_id = g.id "
            "ORDER BY g.sort_order, t.sort_order"
        ).fetchall()

        data = {}
        for t in terms:
            term_courses = {}
            courses = conn.execute(
                "SELECT id, name, guide, has_practical, has_lesson_plan, single_category, category_mode "
                "FROM courses WHERE term_id = ? ORDER BY sort_order",
                (t["id"],),
            ).fetchall()
            for c in courses:
                # دسته‌های همینِ درس -- id-based، مستقل از هر درسِ دیگه
                course_categories = conn.execute(
                    "SELECT id, name FROM categories WHERE course_id = ? ORDER BY sort_order, id",
                    (c["id"],),
                ).fetchall()
                cat_name_by_id = {cc["id"]: cc["name"] for cc in course_categories}
                # فاز ۷: هر دسته علاوه بر files، لیستِ groups (گروه‌های زیرِ اون دسته
                # برای همین درس) رو هم داره. این فقط اطلاعاتِ نمایشیه -- CRUD گروه‌ها
                # از توابعِ id-based (add_file_group و...) انجام می‌شه، نه از اینجا.
                cats = {cc["name"]: {"files": [], "groups": []} for cc in course_categories}
                group_rows = conn.execute(
                    "SELECT id, category_id, name, sort_order FROM course_file_groups "
                    "WHERE course_id = ? ORDER BY sort_order, id",
                    (c["id"],),
                ).fetchall()
                for g in group_rows:
                    cat_name = cat_name_by_id.get(g["category_id"])
                    if cat_name is None:
                        continue
                    cats[cat_name]["groups"].append(
                        {"id": g["id"], "name": g["name"], "sort_order": g["sort_order"]}
                    )

                files = conn.execute(
                    "SELECT * FROM course_files WHERE course_id = ? ORDER BY sort_order", (c["id"],)
                ).fetchall()
                for f in files:
                    cat_name = cat_name_by_id.get(f["category_id"])
                    if cat_name is None:
                        continue
                    file_dict = {
                        "type": f["type"],
                        "caption": f["caption"] or "",
                        "downloads": f["downloads"],
                        "fid": f["fid"],
                    }
                    if f["file_id"]:
                        file_dict["file_id"] = f["file_id"]
                    if f["url"]:
                        file_dict["url"] = f["url"]
                    if f["badge"]:
                        file_dict["badge"] = f["badge"]
                    if f["description"]:
                        file_dict["description"] = f["description"]
                    if f["content"]:
                        file_dict["content"] = f["content"]
                    if f["group_id"] is not None:
                        file_dict["group_id"] = f["group_id"]
                    cats[cat_name]["files"].append(file_dict)
                term_courses[c["name"]] = {
                    "id": c["id"],
                    "guide": c["guide"] or "",
                    "has_practical": bool(c["has_practical"]),
                    "has_lesson_plan": bool(c["has_lesson_plan"]),
                    "single_category": c["single_category"],
                    "category_mode": c["category_mode"] or "standard",
                    "categories": cats,
                }
            data[t["name"]] = term_courses
        return data


def save_data(data: dict) -> None:
    """کل درخت درس‌ها رو (به همون شکل dict قدیمی) می‌گیره و در SQLite بازنویسی می‌کنه.
    معادلِ نوشتن کامل courses_data.json در نسخه‌ی قبلی -- تراکنشی و امن."""
    with get_conn() as conn:
        # نکته‌ی مهم (migrate_categories_per_course_v3 به بعد): این کش دیگه سراسری
        # نیست -- برای هر course_id جداگانه پر می‌شه (cat_ids_by_course[course_id][name])
        # تا دسته‌ی هم‌نام توی دو درسِ مختلف هرگز به یک id مشترک اشاره نکنه.
        cat_ids_by_course = {}

        default_group_id = conn.execute(
            "SELECT id FROM groups WHERE name = ?", (DEFAULT_GROUP_NAME,)
        ).fetchone()
        if default_group_id is None:
            conn.execute("INSERT INTO groups (name, sort_order) VALUES (?, 0)", (DEFAULT_GROUP_NAME,))
            default_group_id = conn.execute(
                "SELECT id FROM groups WHERE name = ?", (DEFAULT_GROUP_NAME,)
            ).fetchone()
        default_group_id = default_group_id["id"]

        for t_i, (term_name, courses) in enumerate(data.items()):
            row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO terms (group_id, name, sort_order) VALUES (?, ?, ?)",
                    (default_group_id, term_name, t_i),
                )
                term_id = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()["id"]
            else:
                term_id = row["id"]

            for c_i, (course_name, course) in enumerate(courses.items()):
                crow = conn.execute(
                    "SELECT id FROM courses WHERE term_id = ? AND name = ?", (term_id, course_name)
                ).fetchone()
                guide = course.get("guide", "")
                has_practical = 1 if course.get("has_practical") else 0
                has_lesson_plan = 1 if course.get("has_lesson_plan") else 0
                if crow is None:
                    conn.execute(
                        "INSERT INTO courses (term_id, name, guide, sort_order, has_practical, has_lesson_plan) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (term_id, course_name, guide, c_i, has_practical, has_lesson_plan),
                    )
                    course_id = conn.execute(
                        "SELECT id FROM courses WHERE term_id = ? AND name = ?", (term_id, course_name)
                    ).fetchone()["id"]
                else:
                    course_id = crow["id"]
                    conn.execute(
                        "UPDATE courses SET guide = ?, sort_order = ?, has_practical = ?, has_lesson_plan = ? "
                        "WHERE id = ?",
                        (guide, c_i, has_practical, has_lesson_plan, course_id),
                    )

                # فایل‌های فعلیِ این درس در دیتابیس رو با fid ورودی مقایسه می‌کنیم
                existing_fids = {
                    r["fid"]: r["id"]
                    for r in conn.execute("SELECT id, fid FROM course_files WHERE course_id = ?", (course_id,))
                }
                seen_fids = set()

                cat_ids = cat_ids_by_course.get(course_id)
                if cat_ids is None:
                    cat_ids = {
                        r["name"]: r["id"]
                        for r in conn.execute("SELECT id, name FROM categories WHERE course_id = ?", (course_id,))
                    }
                    cat_ids_by_course[course_id] = cat_ids

                for cat_name, cat_data in course.get("categories", {}).items():
                    if cat_name not in cat_ids:
                        conn.execute(
                            "INSERT INTO categories (course_id, name, sort_order) VALUES (?, ?, ?)",
                            (course_id, cat_name, len(cat_ids)),
                        )
                        cat_ids[cat_name] = conn.execute(
                            "SELECT id FROM categories WHERE course_id = ? AND name = ?", (course_id, cat_name)
                        ).fetchone()["id"]
                    category_id = cat_ids[cat_name]

                    for f_i, f in enumerate(cat_data.get("files", [])):
                        fid = f.get("fid") or uuid.uuid4().hex[:8]
                        seen_fids.add(fid)
                        # فاز ۷: group_id رو هم رفت‌وبرگشت می‌کنیم. اگه یه فایل قبلاً
                        # group_id داشت و کدی که این دیکشنری رو ساخته دست‌نخورده از
                        # load_data آورده باشدش، همون‌جا می‌مونه؛ اگه نداشت (کلید غایب
                        # یا None)، NULL می‌شه -- یعنی «بدون گروه»، رفتار قدیمی.
                        if fid in existing_fids:
                            conn.execute(
                                """UPDATE course_files SET
                                     category_id = ?, type = ?, file_id = ?, url = ?,
                                     caption = ?, description = ?, content = ?,
                                     downloads = ?, badge = ?, sort_order = ?, group_id = ?
                                   WHERE id = ?""",
                                (
                                    category_id,
                                    f.get("type", "file"),
                                    f.get("file_id"),
                                    f.get("url"),
                                    f.get("caption", ""),
                                    f.get("description", ""),
                                    f.get("content"),
                                    f.get("downloads", 0),
                                    f.get("badge"),
                                    f_i,
                                    f.get("group_id"),
                                    existing_fids[fid],
                                ),
                            )
                        else:
                            conn.execute(
                                """INSERT INTO course_files
                                   (course_id, category_id, fid, type, file_id, url,
                                    caption, description, content, downloads, badge, sort_order, group_id)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    course_id,
                                    category_id,
                                    fid,
                                    f.get("type", "file"),
                                    f.get("file_id"),
                                    f.get("url"),
                                    f.get("caption", ""),
                                    f.get("description", ""),
                                    f.get("content"),
                                    f.get("downloads", 0),
                                    f.get("badge"),
                                    f_i,
                                    f.get("group_id"),
                                ),
                            )

                # فایل‌هایی که دیگه توی دیکشنری ورودی نیستن یعنی حذف شدن
                gone = set(existing_fids) - seen_fids
                for fid in gone:
                    conn.execute("DELETE FROM course_files WHERE id = ?", (existing_fids[fid],))

            # درس‌هایی که توی این ترم در دیتابیس بودن ولی دیگه توی دیکشنریِ ورودی نیستن
            # (یعنی حذف شدن) -- این cleanup لازمه چون بالاتر فقط INSERT/UPDATE می‌شه، نه DELETE.
            # ⚠️ توجه: برای حذف/تغییرنامِ یک درسِ خاص، به‌جای دستکاری دیکشنری و صداکردنِ
            # save_data، از delete_course_by_id()/rename_course_by_id() استفاده کن -- چون
            # اون‌ها بر اساس id عمل می‌کنن، نه اسم، و ریسکِ گم‌شدن/تکرارِ فایل رو ندارن.
            existing_course_names = {
                r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM courses WHERE term_id = ?", (term_id,))
            }
            gone_courses = set(existing_course_names) - set(courses.keys())
            for name in gone_courses:
                conn.execute("DELETE FROM courses WHERE id = ?", (existing_course_names[name],))


def load_users() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
        users = {}
        for r in rows:
            entry = {
                "joined_channel": bool(r["joined_channel"]),
                "nickname_asked": bool(r["nickname_asked"]),
            }
            if r["nickname"] is not None:
                entry["nickname"] = r["nickname"]
            if r["class_program"] is not None:
                entry["class_program"] = r["class_program"]
            saved = json.loads(r["saved_fids"] or "[]")
            if saved:
                entry["saved_fids"] = saved
            if r["notify_prefs"]:
                try:
                    entry["notify_prefs"] = json.loads(r["notify_prefs"])
                except (TypeError, ValueError):
                    pass
            if r["program_asked"]:
                entry["program_asked"] = True
            users[str(r["user_id"])] = entry
        return users


def update_users_class_program(old_name: str, new_name: str) -> int:
    """بعد از تغییر نام ترم، class_program همه‌ی کاربران را اتمیک به نام جدید می‌برد.
    خروجی تعداد کاربرهای تغییرکرده است."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET class_program = ? WHERE class_program = ?",
            (new_name, old_name),
        )
        return cur.rowcount


def clear_users_class_program(term_name: str) -> int:
    """بعد از حذف ترم، انتخاب پیش‌فرض کاربرانی که روی همان ترم بوده‌اند پاک می‌شود."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET class_program = NULL WHERE class_program = ?",
            (term_name,),
        )
        return cur.rowcount


def save_users(users: dict) -> None:
    with get_conn() as conn:
        for uid, u in users.items():
            notify_prefs = u.get("notify_prefs")
            conn.execute(
                """INSERT INTO users (user_id, joined_channel, nickname_asked, nickname,
                                       class_program, saved_fids, notify_prefs, program_asked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     joined_channel = excluded.joined_channel,
                     nickname_asked = excluded.nickname_asked,
                     nickname = excluded.nickname,
                     class_program = excluded.class_program,
                     saved_fids = excluded.saved_fids,
                     notify_prefs = excluded.notify_prefs,
                     program_asked = excluded.program_asked""",
                (
                    int(uid),
                    1 if u.get("joined_channel") else 0,
                    1 if u.get("nickname_asked") else 0,
                    u.get("nickname"),
                    u.get("class_program"),
                    json.dumps(u.get("saved_fids", []), ensure_ascii=False),
                    json.dumps(notify_prefs, ensure_ascii=False) if notify_prefs is not None else None,
                    1 if u.get("program_asked") else 0,
                ),
            )


def touch_last_seen(user_id: int, timestamp_iso: str) -> None:
    """آخرین‌باری که کاربر با ربات تعامل داشته رو ثبت می‌کنه؛ یه UPDATE سبک روی
    همون ردیف (نه لود/سیوِ کاملِ جدولِ users). اگه کاربر هنوز ردیفی نداره، یه
    ردیفِ حداقلی می‌سازه تا بعداً save_users بقیه‌ی فیلدهاش رو تکمیل کنه."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users (user_id, last_seen) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen""",
            (int(user_id), timestamp_iso),
        )


def count_active_users_since(now_iso: str, days: int) -> int:
    """تعدادِ کاربرهایی که last_seenِ‌شون توی «days» روزِ گذشته (نسبت به now_iso)
    بوده. کاربرهایی که last_seen ندارن (چون قبل از اضافه‌شدنِ این ستون بودن و
    از اون موقع پیامی نداده‌ن) حساب نمی‌شن."""
    import datetime as _dt
    now_dt = _dt.datetime.fromisoformat(now_iso)
    with get_conn() as conn:
        rows = conn.execute("SELECT last_seen FROM users WHERE last_seen IS NOT NULL").fetchall()
    count = 0
    for r in rows:
        try:
            last_seen_dt = _dt.datetime.fromisoformat(r["last_seen"])
        except (TypeError, ValueError):
            continue
        if (now_dt - last_seen_dt).total_seconds() / 86400 <= days:
            count += 1
    return count


def load_subscribers() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM subscribers").fetchall()
        return [r["user_id"] for r in rows]


def save_subscribers(subscribers: list) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM subscribers")
        for uid in subscribers:
            conn.execute("INSERT OR IGNORE INTO subscribers (user_id) VALUES (?)", (int(uid),))


# ============================================================
# فاز ۵ -- اطلاع‌رسانیِ چند-دسته‌ای
#
# قبلاً یه دکمه‌ی «🔔 اطلاع‌رسانی» بود که هم پیامِ «فایل جدید» و هم پیامِ «روشن‌شدنِ
# ربات» رو با هم روشن/خاموش می‌کرد (جدولِ subscribers). حالا هر کاربر جدا برای هر
# دسته تصمیم می‌گیره: NOTIFY_KEYS. جدولِ subscribers قدیمی رو نگه می‌داریم (حذف
# نمی‌کنیم) و فقط به‌عنوانِ «مقدارِ پیش‌فرضِ سازگار با قبل» برای startup/new_files
# استفاده می‌کنیم -- یعنی کسی که قبلاً مشترک بوده، تا وقتی صریحاً خودش تغییری نده،
# همون‌طوری فعال می‌مونه. casual_chat و exam_chat کاملاً جدیدن و پیش‌فرضشون خاموشه.
# ============================================================

NOTIFY_KEYS = ("casual_chat", "exam_chat", "startup", "new_files", "schedule_change", "class_reminder")


def get_notify_prefs(user_id: int) -> dict:
    """ترجیحاتِ اطلاع‌رسانیِ این کاربر رو با مقادیرِ پیش‌فرضِ کامل برمی‌گردونه."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT notify_prefs FROM users WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        stored = {}
        if row and row["notify_prefs"]:
            try:
                stored = json.loads(row["notify_prefs"])
            except (TypeError, ValueError):
                stored = {}
        is_legacy_subscriber = (
            conn.execute(
                "SELECT 1 FROM subscribers WHERE user_id = ?", (int(user_id),)
            ).fetchone()
            is not None
        )

    defaults = {
        "casual_chat": False,
        "exam_chat": False,
        "startup": is_legacy_subscriber,
        "new_files": is_legacy_subscriber,
        "schedule_change": False,
        # یادآوریِ ۱۰ دقیقه قبل از کلاس: پیش‌فرض روشنه تا رفتارِ قبلیِ ربات عوض نشه؛
        # هر دانشجو می‌تونه از «🔔 اطلاع‌رسانی» خاموشش کنه.
        "class_reminder": True,
    }
    for key in NOTIFY_KEYS:
        if key not in stored:
            stored[key] = defaults[key]
    return {key: bool(stored[key]) for key in NOTIFY_KEYS}


def set_notify_pref(user_id: int, key: str, value: bool) -> dict:
    """یه دسته‌ی خاص رو برای این کاربر روشن/خاموش می‌کنه و کل dict به‌روزشده رو برمی‌گردونه."""
    if key not in NOTIFY_KEYS:
        raise ValueError(f"کلید اطلاع‌رسانیِ نامعتبر: {key}")
    prefs = get_notify_prefs(user_id)
    prefs[key] = bool(value)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, notify_prefs) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET notify_prefs = excluded.notify_prefs",
            (int(user_id), json.dumps(prefs, ensure_ascii=False)),
        )
    return prefs


def get_users_for_notify(key: str, class_program: str = None) -> list:
    """user_idِ کاربرانی که برای دسته‌ی «key» اطلاع‌رسانی رو روشن دارن.
    اگه class_program داده بشه، فقط کاربرانی که class_program‌شون دقیقاً همونه
    برمی‌گردن (برای «گپ امتحانیِ ترم خودم» و «فایل جدیدِ ترم خودم»)."""
    if key not in NOTIFY_KEYS:
        raise ValueError(f"کلید اطلاع‌رسانیِ نامعتبر: {key}")
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id, class_program, notify_prefs FROM users").fetchall()
        subscriber_ids = {r["user_id"] for r in conn.execute("SELECT user_id FROM subscribers")}

    result = []
    for r in rows:
        if class_program is not None and r["class_program"] != class_program:
            continue
        stored = {}
        if r["notify_prefs"]:
            try:
                stored = json.loads(r["notify_prefs"])
            except (TypeError, ValueError):
                stored = {}
        if key in stored:
            enabled = bool(stored[key])
        else:
            enabled = (key in ("startup", "new_files") and r["user_id"] in subscriber_ids) or key == "class_reminder"
        if enabled:
            result.append(r["user_id"])
    return result


# ============================================================
# فاز ۲ -- مدیریت چندسطحی (RBAC): SUPER_ADMIN / TERM_ADMIN
# ============================================================

def get_admin_roles(user_id: int) -> list:
    """همه‌ی نقش‌های ادمینیِ این کاربر رو برمی‌گردونه -- یک کاربر می‌تونه هم‌زمان
    ادمینِ چند ترمِ مختلف باشه، پس این تابع یه لیست برمی‌گردونه نه یه نقشِ تکی.
    هر آیتم: {'role': 'SUPER_ADMIN'|'TERM_ADMIN', 'term': نام‌ترم یا None}.
    اگه کاربر اصلاً ادمین نباشه، لیستِ خالی برمی‌گرده."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.role, t.name AS term_name FROM admins a "
            "LEFT JOIN terms t ON a.term_id = t.id WHERE a.user_id = ?",
            (user_id,),
        ).fetchall()
        return [{"role": r["role"], "term": r["term_name"]} for r in rows]


def get_admin(user_id: int):
    """سازگاری با کدِ قدیمی: فقط اولین نقشِ این کاربر رو برمی‌گردونه (یا None).
    برای منطقی که باید *همه‌ی* نقش‌های یه کاربر رو در نظر بگیره (دسترسی‌ها، پنل‌ها،
    ...) به‌جاش از get_admin_roles/can_manage_term/allowed_terms_for استفاده کن."""
    roles = get_admin_roles(user_id)
    return roles[0] if roles else None


def can_manage_term(user_id: int, term_name: str) -> bool:
    """آیا این کاربر اجازه‌ی مدیریت این ترمِ به‌خصوص رو داره؟
    (SUPER_ADMIN همیشه بله؛ TERM_ADMIN برای هرکدوم از ترم‌هایی که ادمینشونه.)"""
    roles = get_admin_roles(user_id)
    if any(r["role"] == "SUPER_ADMIN" for r in roles):
        return True
    return any(r["role"] == "TERM_ADMIN" and r["term"] == term_name for r in roles)


def allowed_terms_for(user_id: int) -> list:
    """لیست ترم‌هایی که این ادمین اجازه‌ی مدیریتشون رو داره (سوپرادمین: همه‌ی
    ترم‌ها؛ Term Adminِ چند-ترمی: همه‌ی ترم‌هایی که ادمینشونه، نه فقط یکی)."""
    roles = get_admin_roles(user_id)
    if any(r["role"] == "SUPER_ADMIN" for r in roles):
        return get_program_names()
    return [r["term"] for r in roles if r["role"] == "TERM_ADMIN" and r["term"]]


def add_admin(user_id: int, role: str, term_name: str = None):
    """ادمینیِ جدید اضافه می‌کنه، بدونِ اینکه ادمینی‌های قبلیِ همین کاربر (برای
    ترم‌های دیگه) حذف بشن -- هر (user_id, term_id) یه ردیفِ مستقله.
    خروجی: (ok: bool, message: str)."""
    if role not in ("SUPER_ADMIN", "TERM_ADMIN"):
        return False, "نقش نامعتبره."
    with get_conn() as conn:
        if role == "TERM_ADMIN":
            if not term_name:
                return False, "برای Term Admin باید اسم ترم مشخص بشه."
            row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
            if row is None:
                return False, f"ترمی به اسم «{term_name}» پیدا نشد."
            term_id = row["id"]
            # اگه این کاربر از قبل روی همین ترم ردیفی داشته (مثلاً قبلاً هم Term
            # Adminِ همین ترم بوده)، فقط همون ردیف رو به‌روز می‌کنیم؛ ردیف‌های
            # مربوط به ترم‌های دیگه‌ش دست‌نخورده می‌مونن.
            conn.execute(
                "INSERT INTO admins (user_id, role, term_id) VALUES (?, 'TERM_ADMIN', ?) "
                "ON CONFLICT(user_id, term_id) DO UPDATE SET role = excluded.role",
                (user_id, term_id),
            )
        else:
            # SUPER_ADMIN همیشه term_id=NULL داره. چون SQLite توی UNIQUE(user_id,
            # term_id) هر NULL رو جدا از بقیه حساب می‌کنه (نه یکتا)، خودمون قبل از
            # insert چک می‌کنیم که ردیفِ تکراری ساخته نشه.
            existing = conn.execute(
                "SELECT id FROM admins WHERE user_id = ? AND role = 'SUPER_ADMIN' AND term_id IS NULL",
                (user_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO admins (user_id, role, term_id) VALUES (?, 'SUPER_ADMIN', NULL)",
                    (user_id,),
                )
        return True, "ok"


def remove_admin(user_id: int, role: str = None, term_name: str = None) -> bool:
    """یه نقشِ ادمینیِ مشخص رو حذف می‌کنه، بدونِ اینکه به نقش‌های دیگه‌ی همون کاربر
    (اگه چندتا داشته باشه) دست بزنه:
      - remove_admin(uid, term_name="ترم ۳")       -> فقط ادمینیِ همون ترم
      - remove_admin(uid, role="SUPER_ADMIN")       -> فقط نقشِ سوپرادمین
      - remove_admin(uid)                           -> همه‌ی نقش‌های این کاربر (سازگاری قدیمی)
    خروجی: True اگه چیزی واقعاً حذف شده باشه."""
    with get_conn() as conn:
        if term_name:
            cur = conn.execute(
                "DELETE FROM admins WHERE user_id = ? AND role = 'TERM_ADMIN' AND "
                "term_id = (SELECT id FROM terms WHERE name = ?)",
                (user_id, term_name),
            )
        elif role == "SUPER_ADMIN":
            cur = conn.execute(
                "DELETE FROM admins WHERE user_id = ? AND role = 'SUPER_ADMIN'", (user_id,)
            )
        else:
            cur = conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


def list_admins() -> list:
    """همه‌ی ردیف‌های admins رو برمی‌گردونه -- اگه کاربری چند نقش داشته باشه،
    به همون تعداد بار توی این لیست ظاهر می‌شه (هر ردیف = یک نقش مستقل)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.user_id, a.role, t.name AS term_name FROM admins a "
            "LEFT JOIN terms t ON a.term_id = t.id "
            "ORDER BY a.user_id, (a.role = 'SUPER_ADMIN') DESC, t.name"
        ).fetchall()
        return [{"user_id": r["user_id"], "role": r["role"], "term": r["term_name"]} for r in rows]


def get_admins_for_term(term_name: str) -> list:
    """همه‌ی user_id هایی که مجازن این ترم رو مدیریت کنن: همه‌ی SUPER_ADMIN ها +
    TERM_ADMIN(های) همون ترم. برای روتینگ اعلان «فایل در انتظار تأیید» استفاده می‌شه.
    DISTINCT چون یه کاربر ممکنه هم‌زمان چند ردیف داشته باشه (مثلاً هم سوپرادمین
    باشه هم توی گذشته Term Admin همین ترم بوده) و نباید اعلان تکراری بگیره."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT a.user_id FROM admins a "
            "LEFT JOIN terms t ON a.term_id = t.id "
            "WHERE a.role = 'SUPER_ADMIN' OR t.name = ?",
            (term_name,),
        ).fetchall()
        return [r["user_id"] for r in rows]


def log_action(admin_id: int, action: str, term: str = None, detail: str = None) -> None:
    import datetime as _dt
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (admin_id, action, term, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, term, detail, _dt.datetime.now().isoformat(timespec="seconds")),
        )


def get_audit_log(limit: int = 20, term: str = None) -> list:
    with get_conn() as conn:
        if term:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE term = ? ORDER BY id DESC LIMIT ?", (term, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# فاز ۳ -- ساختار سلسله‌مراتبی گروه/مقطع → ترم
# ============================================================

def get_groups_with_terms() -> list:
    """[(نام‌گروه, [نام‌ترم, ...]), ...] به ترتیب sort_order. گروه‌های بدون ترم رد می‌شن."""
    with get_conn() as conn:
        groups = conn.execute("SELECT id, name FROM groups ORDER BY sort_order").fetchall()
        result = []
        for g in groups:
            terms = conn.execute(
                "SELECT name FROM terms WHERE group_id = ? ORDER BY sort_order", (g["id"],)
            ).fetchall()
            if terms:
                result.append((g["name"], [t["name"] for t in terms]))
        return result


def get_terms_in_group(group_name: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.name FROM terms t JOIN groups g ON t.group_id = g.id "
            "WHERE g.name = ? ORDER BY t.sort_order",
            (group_name,),
        ).fetchall()
        return [r["name"] for r in rows]


def seed_group_structure(physiopath_term_names: list) -> dict:
    """یک‌بار اجرا می‌شه (idempotent با meta['seeded_groups_v3']):
    ۱) ترم‌های فیزیوپاتِ موجود رو از گروه پیش‌فرض به گروه «🩺 فیزیوپات» منتقل می‌کنه.
    ۲) گروه «📚 علوم پایه» رو با ترم ۱ تا ۴ (بدون درس -- اسکلت آماده برای آینده) می‌سازه.
    داده‌ی درس‌های موجود دست‌نخورده می‌مونه؛ فقط چیدمانِ گروه‌بندی اضافه می‌شه."""
    with get_conn() as conn:
        if _meta_get(conn, "seeded_groups_v3") == "done":
            return {"skipped": True}

        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, 0)", (BASIC_SCIENCES_GROUP_NAME,)
        )
        conn.execute(
            "INSERT OR IGNORE INTO groups (name, sort_order) VALUES (?, 1)", (PHYSIOPATH_GROUP_NAME,)
        )
        basic_group_id = conn.execute(
            "SELECT id FROM groups WHERE name = ?", (BASIC_SCIENCES_GROUP_NAME,)
        ).fetchone()["id"]
        physio_group_id = conn.execute(
            "SELECT id FROM groups WHERE name = ?", (PHYSIOPATH_GROUP_NAME,)
        ).fetchone()["id"]

        # ترم‌های موجود فیزیوپات رو به گروه فیزیوپات منتقل کن
        moved = 0
        for i, name in enumerate(physiopath_term_names):
            cur = conn.execute(
                "UPDATE terms SET group_id = ?, sort_order = ? WHERE name = ?",
                (physio_group_id, i, name),
            )
            moved += cur.rowcount

        # اسکلت علوم پایه: ترم ۱ تا ۴ (بدون درس -- ادمین بعداً با addcourse تکمیل می‌کنه)
        created = 0
        for i, term_name in enumerate(BASIC_SCIENCES_TERMS):
            cur = conn.execute(
                "INSERT OR IGNORE INTO terms (group_id, name, sort_order) VALUES (?, ?, ?)",
                (basic_group_id, term_name, i),
            )
            created += cur.rowcount

        _meta_set(conn, "seeded_groups_v3", "done")
        return {"skipped": False, "physiopath_terms_moved": moved, "basic_sciences_terms_created": created}


def seed_physiopath1_defaults(term_name: str, course_names: list) -> dict:
    """یک‌بار (idempotent، meta['seeded_physiopath1_defaults_v1']) درس‌های پایه‌ایِ
    فیزیوپات۱ رو می‌سازه اگه نبودن. بعد از اولین اجرا دیگه هیچ‌وقت تکرار نمی‌شه --
    حتی اگه ادمین بعداً عمداً یکی از این درس‌ها رو حذف یا تغییرنام بده، دوباره سبز نمی‌شه.
    (قبلاً این کار توی هر load_data() تکرار می‌شد که باعث می‌شد حذف/ویرایشِ این درس‌های
    خاص هیچ‌وقت ماندگار نمونه -- همون لحظه‌ی بعد دوباره ساخته می‌شدن.)"""
    with get_conn() as conn:
        if _meta_get(conn, "seeded_physiopath1_defaults_v1") == "done":
            return {"skipped": True}
        term_row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
        if term_row is None:
            _meta_set(conn, "seeded_physiopath1_defaults_v1", "done")
            return {"skipped": True, "reason": "term_not_found"}
        term_id = term_row["id"]
        existing = {r["name"] for r in conn.execute("SELECT name FROM courses WHERE term_id = ?", (term_id,))}
        created = 0
        for i, name in enumerate(course_names):
            if name not in existing:
                conn.execute(
                    "INSERT INTO courses (term_id, name, guide, sort_order) VALUES (?, ?, '', ?)",
                    (term_id, name, i),
                )
                created += 1
        _meta_set(conn, "seeded_physiopath1_defaults_v1", "done")
        return {"skipped": False, "created": created}


def migrate_category_restructure_v2() -> dict:
    """مهاجرتِ ساختار جدید بخش‌های داخل درس‌ها (📚 منابع / 💡 پیشنهاد مطالعه /
    📘 Study_Guid / ❓ نمونه‌سوالات / 🩺 تجربه ارسالی دانشجویان / 🧪 عملی /
    📂 فایل‌های تکمیلی / 📋 طرح درس).

    فقط category_id فایل‌های موجود (و اسمِ خودِ دسته‌ها) تغییر می‌کنه؛ fid، file_id،
    caption، downloads و بقیه‌ی متادیتای هر فایل دست‌نخورده می‌مونه -- هیچ فایلی حذف،
    بازسازی یا دوباره آپلود نمی‌شه.

    کاملاً idempotent: هر بار اجرا بشه، وضعیتِ نهایی همونیه که یه‌بار اجرا شده باشه
    (rename فقط وقتی اسمِ قدیم هنوز هست انجام می‌شه، انتقالِ Study_Guid فقط فایل‌هایی
    که هنوز جابه‌جا نشدن رو می‌گیره، has_practical هر بار همون لیست رو ست می‌کنه).

    خروجی: گزارش قبل/بعد برای هر دسته + چک‌های صحت (تعداد کل فایل‌ها، مجموع دانلودها،
    یکتا بودن fid).

    نکته‌ی مهم (باگ‌فیکس): این تابع در واقع مالِ *قبل* از migrate_categories_per_course_v3
    هست -- یعنی از دورانی که categories سراسری بود (بدون course_id) و "اسمِ دسته"
    به‌تنهایی یکتا بود. کوئری‌های زیر (rename/merge/sort_order) عمداً فقط بر اساسِ name
    کار می‌کنن، نه (course_id, name). بعد از فاز ۸، اگه این تابع هر بار بدونِ قفل
    اجرا بشه، sort_order و merge رو روی *همه‌ی درس‌ها* یک‌جا اعمال می‌کنه (چون name
    دیگه یکتا نیست) -- یعنی هر بار بات بالا بیاد، ترتیبِ دستیِ ادمین برای دسته‌های
    پیش‌فرض توی همه‌ی درس‌ها به ترتیبِ ثابتِ DEFAULT_CATEGORIES ریست می‌شه. برای همین
    از این‌جا به بعد این تابع هم مثلِ بقیه‌ی migration ها با یک کلیدِ meta قفل می‌شه
    و فقط یک‌بار واقعاً کاری انجام می‌ده."""
    with get_conn() as conn:
        if _meta_get(conn, "migrated_category_restructure_v2") == "done":
            return {"skipped": True, "ok": True}

        def counts_by_category():
            rows = conn.execute(
                "SELECT c.name, COUNT(cf.id) AS n FROM categories c "
                "LEFT JOIN course_files cf ON cf.category_id = c.id "
                "GROUP BY c.id ORDER BY c.sort_order"
            ).fetchall()
            return {r["name"]: r["n"] for r in rows}

        def total_files():
            return conn.execute("SELECT COUNT(*) AS n FROM course_files").fetchone()["n"]

        def total_downloads():
            return conn.execute("SELECT COALESCE(SUM(downloads), 0) AS n FROM course_files").fetchone()["n"]

        def distinct_fids():
            return conn.execute("SELECT COUNT(DISTINCT fid) AS n FROM course_files").fetchone()["n"]

        before = {
            "by_category": counts_by_category(),
            "total_files": total_files(),
            "total_downloads": total_downloads(),
            "distinct_fids": distinct_fids(),
        }

        def merge_category(old_name, new_name):
            """اگه دسته‌ی old_name وجود داره: یا صرفاً rename می‌شه (اگه new_name نبود)،
            یا (اگه init_db() از قبل new_name رو به‌عنوان دسته‌ی خالیِ جدید ساخته بود)
            فایل‌هاش به new_name منتقل و ردیفِ خالی‌شده‌ی old_name حذف می‌شه.
            خروجی: تعداد فایلی که واقعاً جابه‌جا شد. کاملاً idempotent -- دفعه‌ی بعد
            old_name دیگه پیدا نمی‌شه و هیچ کاری انجام نمی‌شه."""
            old_row = conn.execute("SELECT id FROM categories WHERE name = ?", (old_name,)).fetchone()
            if old_row is None:
                return 0
            new_row = conn.execute("SELECT id FROM categories WHERE name = ?", (new_name,)).fetchone()
            if new_row is None:
                conn.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, old_row["id"]))
                return 0
            if old_row["id"] == new_row["id"]:
                return 0
            moved = conn.execute(
                "UPDATE course_files SET category_id = ? WHERE category_id = ?",
                (new_row["id"], old_row["id"]),
            ).rowcount
            conn.execute("DELETE FROM categories WHERE id = ?", (old_row["id"],))
            return moved

        # ---- ۱) رفرنس و پاور / خلاصه / نمونه‌سوالات: rename یا merge با placeholderِ جدید ----
        for old_name, new_name in _CATEGORY_RENAMES_V2.items():
            if old_name == "🩺 تجربه امتحان دانشجویان":
                continue  # این یکی بعد از جدا کردن Study_Guid انجام می‌شه (پایین‌تر)
            merge_category(old_name, new_name)

        # ---- ۲) مطمئن شو همه‌ی دسته‌های جدید (جزوه/پیشنهاد مطالعه/Study_Guid/عملی) وجود دارن ----
        existing_cats = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
        for i, cat in enumerate(DEFAULT_CATEGORIES):
            if cat not in existing_cats:
                conn.execute("INSERT INTO categories (name, sort_order) VALUES (?, ?)", (cat, i))

        # ترتیب نمایش رو با ساختار جدید یکی کن (بی‌ضرره چون bot.py خودش لیستِ CATEGORIES
        # پایتونی رو برای ترتیب دکمه‌ها استفاده می‌کنه، ولی برای تمیزی دیتابیس هم آپدیت می‌شه)
        for i, cat in enumerate(DEFAULT_CATEGORIES):
            conn.execute("UPDATE categories SET sort_order = ? WHERE name = ?", (i, cat))

        # ---- ۳) فایل‌های Study_Guid رو از «تجربه امتحان/ارسالی دانشجویان» جدا کن ----
        # ممکنه این دسته یا هنوز اسم قدیمش (اگه migration اولین‌باره) یا اسم جدیدش
        # (اگه قبلاً یه‌بار اجرا شده و rename زیر انجام شده) رو داشته باشه.
        tajrobe_row = (
            conn.execute("SELECT id FROM categories WHERE name = ?", ("🩺 تجربه امتحان دانشجویان",)).fetchone()
            or conn.execute("SELECT id FROM categories WHERE name = ?", ("🩺 تجربه ارسالی دانشجویان",)).fetchone()
        )
        study_guid_row = conn.execute("SELECT id FROM categories WHERE name = ?", ("📘 Study_Guid",)).fetchone()
        moved_study_guid = 0
        if tajrobe_row is not None and study_guid_row is not None:
            cur = conn.execute(
                "UPDATE course_files SET category_id = ? "
                "WHERE category_id = ? AND caption LIKE 'StudyGuide\\_%' ESCAPE '\\'",
                (study_guid_row["id"], tajrobe_row["id"]),
            )
            moved_study_guid = cur.rowcount

        # ---- ۴) حالا «تجربه امتحان دانشجویان» رو به «تجربه ارسالی دانشجویان» رنیم/merge کن ----
        merge_category("🩺 تجربه امتحان دانشجویان", "🩺 تجربه ارسالی دانشجویان")

        # ---- ۵) بخش «🧪 عملی» رو فقط برای لیستِ مشخص‌شده فعال کن ----
        # (مستقیم با همین conn -- نه با صدا زدنِ set_has_practical که کانکشنِ جدا باز می‌کنه
        # و وسطِ تراکنشِ commit‌نشده‌ی این تابع باعثِ قفل شدنِ دیتابیس می‌شه)
        practical_set = 0
        for term_name, course_name in PRACTICAL_COURSES_V2:
            cur = conn.execute(
                "UPDATE courses SET has_practical = 1 "
                "WHERE term_id = (SELECT id FROM terms WHERE name = ?) AND name = ?",
                (term_name, course_name),
            )
            if cur.rowcount > 0:
                practical_set += 1

        # ---- ۶) بخش «📋 طرح درس» رو فقط برای لیستِ مشخص‌شده فعال کن (همون منطقِ ۵) ----
        lesson_plan_set = 0
        for term_name, course_name in LESSON_PLAN_COURSES_V2:
            cur = conn.execute(
                "UPDATE courses SET has_lesson_plan = 1 "
                "WHERE term_id = (SELECT id FROM terms WHERE name = ?) AND name = ?",
                (term_name, course_name),
            )
            if cur.rowcount > 0:
                lesson_plan_set += 1

        after = {
            "by_category": counts_by_category(),
            "total_files": total_files(),
            "total_downloads": total_downloads(),
            "distinct_fids": distinct_fids(),
        }

        ok = (
            before["total_files"] == after["total_files"]
            and before["total_downloads"] == after["total_downloads"]
            and before["distinct_fids"] == after["distinct_fids"]
            and after["distinct_fids"] == after["total_files"]  # یعنی هیچ fid تکراری نیست
        )

        _meta_set(conn, "migrated_category_restructure_v2", "done")

        return {
            "ok": ok,
            "skipped": False,
            "before": before,
            "after": after,
            "moved_study_guid": moved_study_guid,
            "practical_courses_flagged": practical_set,
            "lesson_plan_courses_flagged": lesson_plan_set,
        }


def migrate_categories_per_course_v3() -> dict:
    """مهاجرتِ اصلیِ فاز ۸: دسته‌های سراسری (categories بدون course_id) رو به
    دسته‌های مستقلِ per-course تبدیل می‌کنه.

    قبل از این مهاجرت: یک ردیفِ categories (مثلاً «📝 جزوه» با id=8) بینِ همه‌ی
    درس‌های دیتابیس مشترک بود -- یعنی ویرایش/حذفِ اون روی همه‌ی درس‌ها همزمان اثر
    می‌ذاشت. بعد از این مهاجرت: هر درس، برای هر دسته‌ای که قبلاً استفاده می‌کرد،
    یک ردیفِ کاملاً جدا و مستقل (id متفاوت) می‌گیره؛ ویرایش/حذفِ دسته‌ی یک درس هیچ
    اثری روی دسته‌ی هم‌نامِ درسِ دیگه نداره.

    طبقِ تصمیمِ صریح: هر درس، حتی اگه فایلی توی یه دسته‌ی خاص نداشته باشه، همون
    مجموعه‌ی کاملِ دسته‌های پیش‌فرضِ سراسریِ قبلی رو به‌صورتِ مستقل می‌گیره (نه فقط
    دسته‌هایی که واقعاً فایل دارن) -- تا هیچ دکمه‌ای از منوی موجودِ ادمین/دانشجو
    ناگهان غیب نشه.

    fid، file_id، caption، downloads و بقیه‌ی متادیتای هر فایل دست‌نخورده می‌مونه؛
    فقط category_id فایل‌ها/گروه‌ها به id جدید ریمپ می‌شه. کاملاً idempotent
    (meta['migrated_categories_per_course_v3'])."""
    with get_conn() as conn:
        if _meta_get(conn, "migrated_categories_per_course_v3") == "done":
            return {"skipped": True}

        # چون init_db() از CREATE TABLE IF NOT EXISTS استفاده می‌کنه، اگه جدولِ
        # categories از قبل با schemaی قدیمی (بدونِ course_id) وجود داشته باشه،
        # init_db دست بهش نمی‌زنه -- این تابع باید خودش جدول رو بازسازی کنه.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
        table_has_course_id = "course_id" in cols

        if not table_has_course_id:
            # موقعِ بازسازیِ جدول (DROP + RENAME) باید FK checks خاموش باشه، وگرنه
            # UPDATE‌هایی که به جدولِ موقتِ categories_v3_new اشاره می‌کنن (قبل از
            # این‌که به اسمِ نهاییِ «categories» rename بشه) با FOREIGN KEY constraint
            # رد می‌شن. این pragma باید قبلِ شروعِ هر تراکنشی روی همین connection ست بشه.
            conn.execute("PRAGMA foreign_keys = OFF")

            # ۱) دسته‌های سراسریِ قدیمی رو از جدولِ فعلی بخون
            old_global_categories = conn.execute(
                "SELECT id, name, sort_order FROM categories ORDER BY sort_order, id"
            ).fetchall()

            # ۲) جدولِ جدید رو با نامِ موقت بساز
            conn.execute(
                """CREATE TABLE categories_v3_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(course_id, name)
                )"""
            )

            # ۳) به‌ازای هر درسِ موجود، یک کپیِ مستقل از هر دسته‌ی سراسریِ قدیمی بساز
            courses_for_seed = conn.execute("SELECT id FROM courses").fetchall()
            old_to_new_by_course = {}  # {course_id: {old_category_id: new_category_id}}
            for course in courses_for_seed:
                mapping = {}
                for old_cat in old_global_categories:
                    cur = conn.execute(
                        "INSERT INTO categories_v3_new (course_id, name, sort_order) VALUES (?, ?, ?)",
                        (course["id"], old_cat["name"], old_cat["sort_order"]),
                    )
                    mapping[old_cat["id"]] = cur.lastrowid
                old_to_new_by_course[course["id"]] = mapping

            # ۴) category_id فایل‌ها و زیردسته‌ها رو به id جدیدِ متعلق به همون course_id ریمپ کن
            for row in conn.execute("SELECT id, course_id, category_id FROM course_files").fetchall():
                new_id = old_to_new_by_course.get(row["course_id"], {}).get(row["category_id"])
                if new_id is not None:
                    conn.execute("UPDATE course_files SET category_id = ? WHERE id = ?", (new_id, row["id"]))

            for row in conn.execute("SELECT id, course_id, category_id FROM course_file_groups").fetchall():
                new_id = old_to_new_by_course.get(row["course_id"], {}).get(row["category_id"])
                if new_id is not None:
                    conn.execute(
                        "UPDATE course_file_groups SET category_id = ? WHERE id = ?", (new_id, row["id"])
                    )

            # ۵) جدولِ قدیمی رو با جدولِ جدید عوض کن
            conn.execute("DROP TABLE categories")
            conn.execute("ALTER TABLE categories_v3_new RENAME TO categories")

            total_files_after = conn.execute("SELECT COUNT(*) AS n FROM course_files").fetchone()["n"]
            total_downloads_after = conn.execute(
                "SELECT COALESCE(SUM(downloads), 0) AS n FROM course_files"
            ).fetchone()["n"]
            distinct_fids_after = conn.execute("SELECT COUNT(DISTINCT fid) AS n FROM course_files").fetchone()["n"]

            _meta_set(conn, "migrated_categories_per_course_v3", "done")
            return {
                "skipped": False,
                "ok": distinct_fids_after == total_files_after,
                "courses_touched": len(courses_for_seed),
                "old_global_categories": len(old_global_categories),
                "categories_created": sum(len(m) for m in old_to_new_by_course.values()),
                "total_files_after": total_files_after,
                "total_downloads_after": total_downloads_after,
            }

        # اگه به اینجا رسیدیم، یعنی جدول از قبل schemaی جدید (course_id) رو داره --
        # یا دیتابیسِ کاملاً تازه‌ست، یا این مهاجرت قبلاً یک‌بار (با نسخه‌ی قدیمی‌ترِ
        # همین تابع) روی جدول اجرا شده. فقط برای consistency‌ی گزارش، وضعیتِ فعلی رو
        # برمی‌گردونیم، بدونِ هیچ تغییرِ دیگه‌ای.
        total_files_before = conn.execute("SELECT COUNT(*) AS n FROM course_files").fetchone()["n"]
        total_downloads_before = conn.execute(
            "SELECT COALESCE(SUM(downloads), 0) AS n FROM course_files"
        ).fetchone()["n"]
        distinct_fids_before = conn.execute("SELECT COUNT(DISTINCT fid) AS n FROM course_files").fetchone()["n"]

        old_global_categories = conn.execute(
            "SELECT id, name, sort_order FROM categories WHERE course_id IS NULL ORDER BY sort_order, id"
        ).fetchall()

        if not old_global_categories:
            _meta_set(conn, "migrated_categories_per_course_v3", "done")
            return {"skipped": True, "reason": "already_new_schema"}

        courses = conn.execute("SELECT id FROM courses").fetchall()

        courses_touched = 0
        categories_created = 0
        files_remapped = 0
        groups_remapped = 0

        for course in courses:
            course_id = course["id"]
            # برای هر درس، به‌ازای هر دسته‌ی سراسریِ قدیمی، یه ردیفِ مستقلِ جدید بساز
            # (اگه هم‌نام از قبل برای همین course_id بود -- که با اجرای دوباره ممکنه
            # پیش بیاد -- ازش استفاده کن، تکراری نساز).
            old_to_new = {}
            for old_cat in old_global_categories:
                existing_new = conn.execute(
                    "SELECT id FROM categories WHERE course_id = ? AND name = ?",
                    (course_id, old_cat["name"]),
                ).fetchone()
                if existing_new:
                    new_id = existing_new["id"]
                else:
                    cur = conn.execute(
                        "INSERT INTO categories (course_id, name, sort_order) VALUES (?, ?, ?)",
                        (course_id, old_cat["name"], old_cat["sort_order"]),
                    )
                    new_id = cur.lastrowid
                    categories_created += 1
                old_to_new[old_cat["id"]] = new_id

            f_count = 0
            for row in conn.execute(
                "SELECT id, category_id FROM course_files WHERE course_id = ?", (course_id,)
            ).fetchall():
                new_cat_id = old_to_new.get(row["category_id"])
                if new_cat_id is not None and new_cat_id != row["category_id"]:
                    conn.execute("UPDATE course_files SET category_id = ? WHERE id = ?", (new_cat_id, row["id"]))
                    f_count += 1
            files_remapped += f_count

            g_count = 0
            for row in conn.execute(
                "SELECT id, category_id FROM course_file_groups WHERE course_id = ?", (course_id,)
            ).fetchall():
                new_cat_id = old_to_new.get(row["category_id"])
                if new_cat_id is not None and new_cat_id != row["category_id"]:
                    conn.execute(
                        "UPDATE course_file_groups SET category_id = ? WHERE id = ?", (new_cat_id, row["id"])
                    )
                    g_count += 1
            groups_remapped += g_count

            courses_touched += 1

        # حالا که هیچ course_files/course_file_groups‌ای دیگه به دسته‌های سراسریِ
        # قدیمی اشاره نمی‌کنه، خودِ ردیف‌های سراسری رو حذف کن.
        old_ids = [c["id"] for c in old_global_categories]
        remaining_refs = conn.execute(
            f"SELECT COUNT(*) AS n FROM course_files WHERE category_id IN "
            f"({','.join('?' * len(old_ids))})",
            old_ids,
        ).fetchone()["n"]
        deleted_old = 0
        if remaining_refs == 0:
            conn.execute(
                f"DELETE FROM categories WHERE id IN ({','.join('?' * len(old_ids))}) AND course_id IS NULL",
                old_ids,
            )
            deleted_old = len(old_ids)

        total_files_after = conn.execute("SELECT COUNT(*) AS n FROM course_files").fetchone()["n"]
        total_downloads_after = conn.execute(
            "SELECT COALESCE(SUM(downloads), 0) AS n FROM course_files"
        ).fetchone()["n"]
        distinct_fids_after = conn.execute("SELECT COUNT(DISTINCT fid) AS n FROM course_files").fetchone()["n"]

        ok = (
            total_files_before == total_files_after
            and total_downloads_before == total_downloads_after
            and distinct_fids_before == distinct_fids_after
            and distinct_fids_after == total_files_after
            and remaining_refs == 0
        )

        _meta_set(conn, "migrated_categories_per_course_v3", "done")

        return {
            "skipped": False,
            "ok": ok,
            "courses_touched": courses_touched,
            "categories_created": categories_created,
            "old_global_categories_deleted": deleted_old,
            "files_remapped": files_remapped,
            "groups_remapped": groups_remapped,
            "total_files_before": total_files_before,
            "total_files_after": total_files_after,
            "total_downloads_before": total_downloads_before,
            "total_downloads_after": total_downloads_after,
        }


def migrate_terms_unique_per_group_v4() -> dict:
    """مهاجرتِ فاز ۱۰: یکتاییِ نامِ ترم رو از UNIQUE(name) سراسری به
    UNIQUE(group_id, name) تغییر می‌ده -- تا مثلاً «📚 علوم پایه» و «🩺 فیزیوپات»
    هر کدوم بتونن ترمی به نامِ «رفرنس» داشته باشن.

    SQLite اجازه‌ی DROP کردنِ یه UNIQUE constraint با ALTER TABLE رو نمی‌ده؛ برای
    همین جدول باید بازسازی بشه (create جدولِ جدید با schemaی درست، کپیِ سطر به
    سطرِ داده‌ی قدیمی -- با همون id ها، DROP جدولِ قدیمی، RENAME جدولِ جدید).
    id هر ترم و در نتیجه هر FK که به‌ش اشاره می‌کنه (courses.term_id) دقیقاً
    دست‌نخورده می‌مونه -- هیچ درس/دسته/فایلی جابه‌جا یا گم نمی‌شه.

    کاملاً idempotent: اگه جدول از قبل constraint درست رو داشته باشه (چه دیتابیسِ
    تازه، چه اجرای دوباره‌ی همین تابع)، هیچ کاری نمی‌کنه."""
    with get_conn() as conn:
        # آیا جدولِ فعلی از قبل UNIQUE(group_id, name) داره؟ (به‌جای UNIQUE(name)
        # سراسری). با بررسیِ index_list/index_info تشخیص می‌دیم.
        indexes = conn.execute("PRAGMA index_list(terms)").fetchall()
        has_correct_constraint = False
        has_old_global_unique = False
        for idx in indexes:
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in conn.execute(f"PRAGMA index_info('{idx['name']}')")]
            if cols == ["group_id", "name"]:
                has_correct_constraint = True
            elif cols == ["name"]:
                has_old_global_unique = True

        if has_correct_constraint or not has_old_global_unique:
            _meta_set(conn, "migrated_terms_unique_per_group_v4", "done")
            return {"skipped": True}

        conn.execute("PRAGMA foreign_keys = OFF")

        old_rows = conn.execute("SELECT id, group_id, name, sort_order FROM terms").fetchall()

        conn.execute(
            """CREATE TABLE terms_v4_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(group_id, name)
            )"""
        )
        for r in old_rows:
            conn.execute(
                "INSERT INTO terms_v4_new (id, group_id, name, sort_order) VALUES (?, ?, ?, ?)",
                (r["id"], r["group_id"], r["name"], r["sort_order"]),
            )
        conn.execute("DROP TABLE terms")
        conn.execute("ALTER TABLE terms_v4_new RENAME TO terms")

        conn.execute("PRAGMA foreign_keys = ON")

        terms_after = conn.execute("SELECT COUNT(*) AS n FROM terms").fetchone()["n"]
        courses_orphaned = conn.execute(
            "SELECT COUNT(*) AS n FROM courses WHERE term_id NOT IN (SELECT id FROM terms)"
        ).fetchone()["n"]

        _meta_set(conn, "migrated_terms_unique_per_group_v4", "done")
        return {
            "skipped": False,
            "ok": terms_after == len(old_rows) and courses_orphaned == 0,
            "terms_migrated": len(old_rows),
        }


def migrate_admins_multi_term_v5() -> dict:
    """مهاجرتِ فاز ۱۱: یک کاربر بتونه هم‌زمان ادمینِ چند ترمِ مختلف باشه.

    قبلاً ستونِ admins.user_id خودش PRIMARY KEY بود -- یعنی هر کاربر فقط می‌تونست
    یک ردیف (یعنی یک نقش/یک ترم) داشته باشه. نتیجه‌ش این بود که اگه یه Term Admin
    بعداً به‌عنوانِ ادمینِ ترمِ دیگه‌ای هم انتخاب می‌شد، ردیفِ قبلی‌ش UPDATE (بازنویسی)
    می‌شد و دسترسیِ ترمِ اولش گم می‌شد.

    این تابع جدول رو با id مستقل + UNIQUE(user_id, term_id) بازسازی می‌کنه تا چند
    ردیف (چند نقش/ترم) به‌ازای یک user_id ممکن بشه. هیچ ادمینِ فعلی‌ای حذف نمی‌شه؛
    فقط ساختار جدول عوض می‌شه و داده‌های قبلی عیناً (با همون user_id/role/term_id)
    کپی می‌شن. کاملاً idempotent."""
    with get_conn() as conn:
        # اگه ستونِ id از قبل روی جدول هست، یعنی قبلاً migrate شده.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(admins)")]
        if "id" in cols:
            _meta_set(conn, "migrated_admins_multi_term_v5", "done")
            return {"skipped": True}

        conn.execute("PRAGMA foreign_keys = OFF")

        old_rows = conn.execute("SELECT user_id, role, term_id FROM admins").fetchall()

        conn.execute(
            """CREATE TABLE admins_v5_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN', 'TERM_ADMIN')),
                term_id INTEGER REFERENCES terms(id) ON DELETE CASCADE,
                UNIQUE(user_id, term_id)
            )"""
        )
        for r in old_rows:
            conn.execute(
                "INSERT INTO admins_v5_new (user_id, role, term_id) VALUES (?, ?, ?)",
                (r["user_id"], r["role"], r["term_id"]),
            )
        conn.execute("DROP TABLE admins")
        conn.execute("ALTER TABLE admins_v5_new RENAME TO admins")

        conn.execute("PRAGMA foreign_keys = ON")

        admins_after = conn.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]
        _meta_set(conn, "migrated_admins_multi_term_v5", "done")
        return {
            "skipped": False,
            "ok": admins_after == len(old_rows),
            "admins_migrated": len(old_rows),
        }


# ============================================================
# فاز ۶ -- برنامه امتحانات رسمی (هر ترم مستقل)
# ============================================================

def add_exam(term_name: str, course: str, exam_date: str, exam_time: str = None, location: str = None, exam_type: str = None):
    """امتحان جدید ثبت می‌کنه. exam_type: 'midterm' یا 'final' (یا None = نامشخص).
    خروجی: id امتحان، یا None اگه ترم پیدا نشد."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM terms WHERE name = ?", (term_name,)).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "INSERT INTO exam_schedule (term_id, course, exam_date, exam_time, location, exam_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], course, exam_date, exam_time, location, exam_type),
        )
        return cur.lastrowid


def list_exams(term_name: str) -> list:
    """امتحان‌های یک ترم، مرتب بر اساس تاریخ/ساعت."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT e.* FROM exam_schedule e JOIN terms t ON e.term_id = t.id "
            "WHERE t.name = ? ORDER BY e.exam_date, e.exam_time",
            (term_name,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_exam(exam_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exam_schedule WHERE id = ?", (exam_id,)).fetchone()
        return dict(row) if row else None


def delete_exam(exam_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM exam_schedule WHERE id = ?", (exam_id,))
        return cur.rowcount > 0


# ============================================================
# فاز ۴ -- گپ دانشجویی (💬)
# ============================================================

import datetime as _dt


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _redact_row(row: dict) -> dict:
    """یک ردیف پیام رو برای مصرف‌کننده‌ای که حق دیدن هویت ناشناس‌ها رو نداره پاک می‌کنه."""
    d = dict(row)
    if d.get("is_anonymous"):
        d["user_id"] = None
        d["display_snapshot"] = "دانشجوی ناشناس"
    return d


# ---------- دوره‌های امتحانی ----------

def get_open_exam_period(course_id: int):
    """دوره‌ی امتحانی بازِ فعلیِ این درس رو برمی‌گردونه؛ اگه نبود، خودکار یکی می‌سازه."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exam_periods WHERE course_id = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (course_id,),
        ).fetchone()
        if row:
            return dict(row)
        cur = conn.execute(
            "INSERT INTO exam_periods (course_id, label, phase, status, opened_at) "
            "VALUES (?, ?, 'prep', 'open', ?)",
            (course_id, "دوره‌ی جاری", _now()),
        )
        return {
            "id": cur.lastrowid, "course_id": course_id, "label": "دوره‌ی جاری",
            "phase": "prep", "status": "open", "opened_at": _now(), "closed_at": None,
        }


def set_exam_period_phase(period_id: int, phase: str) -> bool:
    if phase not in ("prep", "post"):
        return False
    with get_conn() as conn:
        cur = conn.execute("UPDATE exam_periods SET phase = ? WHERE id = ?", (phase, period_id))
        return cur.rowcount > 0


def close_exam_period(period_id: int, new_label: str = None) -> int:
    """دوره‌ی فعلی رو می‌بنده و یه دوره‌ی «prep» جدید برای همون درس باز می‌کنه. id دوره‌ی جدید رو برمی‌گردونه."""
    with get_conn() as conn:
        row = conn.execute("SELECT course_id FROM exam_periods WHERE id = ?", (period_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE exam_periods SET status = 'closed', closed_at = ? WHERE id = ?",
            (_now(), period_id),
        )
        if new_label is None:
            new_label = "دوره‌ی جاری"
        cur = conn.execute(
            "INSERT INTO exam_periods (course_id, label, phase, status, opened_at) "
            "VALUES (?, ?, 'prep', 'open', ?)",
            (row["course_id"], new_label, _now()),
        )
        return cur.lastrowid


def get_exam_periods_for_course(course_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM exam_periods WHERE course_id = ? ORDER BY id DESC", (course_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- عضویت در اتاق گپ (برای مسیر پیام‌رسانی) ----------
#
# نکته: PRIMARY KEY ترکیبی SQLite با NULL درست کار نمی‌کنه (چند NULL باهم برابر
# در نظر گرفته نمی‌شن، پس ON CONFLICT برای گپ دوستانه که course_id نداره فعال نمی‌شد).
# به‌جاش برای «گپ دوستانه» از مقدار ثابت CASUAL_ROOM_KEY = 0 استفاده می‌کنیم
# (چون id واقعیِ courses از ۱ شروع می‌شه، هیچ‌وقت تداخل نداره).
CASUAL_ROOM_KEY = 0


def _room_key(course_id):
    return course_id if course_id is not None else CASUAL_ROOM_KEY


def join_chat_room(chat_type: str, course_id, user_id: int, anonymous: bool = False) -> None:
    """اگه کاربر قبلاً عضو این اتاق بوده، فقط joined_at رو دست نمی‌زنیم (برای اینکه
    زمانِ اولین ورود حفظ بشه) ولی identity_mode رو با انتخابِ همین ورود به‌روز می‌کنیم؛
    چون کاربر ممکنه دفعه‌ی قبل یه‌جور دیگه وارد شده باشه."""
    mode = "anonymous" if anonymous else "nickname"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_room_members (chat_type, course_id, user_id, joined_at, identity_mode) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_type, course_id, user_id) DO UPDATE SET identity_mode = excluded.identity_mode",
            (chat_type, _room_key(course_id), user_id, _now(), mode),
        )


def set_chat_identity_mode(chat_type: str, course_id, user_id: int, anonymous: bool) -> None:
    """تغییرِ هویت در حین حضور در اتاق (بدون خروج و ورود دوباره)."""
    mode = "anonymous" if anonymous else "nickname"
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_room_members SET identity_mode = ? "
            "WHERE chat_type = ? AND course_id = ? AND user_id = ?",
            (mode, chat_type, _room_key(course_id), user_id),
        )


def get_chat_room_members(chat_type: str, course_id, exclude_user_id: int = None) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM chat_room_members WHERE chat_type = ? AND course_id = ?",
            (chat_type, _room_key(course_id)),
        ).fetchall()
        return [r["user_id"] for r in rows if r["user_id"] != exclude_user_id]


# ---------- محدودیت‌ها (سکوت/بن) ----------

def is_user_restricted(user_id: int, course_id=None) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM chat_restrictions WHERE user_id = ? AND lifted_at IS NULL "
            "AND (scope = 'global' OR (scope = 'course' AND course_id = ?)) LIMIT 1",
            (user_id, course_id),
        ).fetchone()
        return row is not None


def restrict_user(user_id: int, scope: str, course_id, restricted_by: int, reason: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_restrictions (user_id, scope, course_id, restricted_by, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, scope, course_id, restricted_by, reason, _now()),
        )
        return cur.lastrowid


def unrestrict_user(user_id: int, course_id=None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_restrictions SET lifted_at = ? WHERE user_id = ? AND lifted_at IS NULL "
            "AND (course_id = ? OR (? IS NULL AND scope = 'global'))",
            (_now(), user_id, course_id, course_id),
        )
        return cur.rowcount


def restrict_sender_of_message(message_id: int, scope: str, restricted_by: int, reason: str = None):
    """ادمین ترم می‌تونه فرستنده‌ی یک پیام (حتی ناشناس) رو محدود کنه، بدون اینکه هیچ‌وقت
    خودِ user_id واقعی به‌عنوان خروجی به‌اش برگرده -- lookup کاملاً داخل این تابع می‌مونه.
    خروجی: True/False (موفق بود یا نه)، نه هویت کاربر."""
    msg = get_chat_message(message_id)
    if not msg:
        return False
    course_id = msg["course_id"] if scope == "course" else None
    restrict_user(msg["user_id"], scope, course_id, restricted_by, reason)
    return True


# ---------- پیام‌ها ----------

def add_chat_message(chat_type: str, course_id, exam_period_id, user_id: int,
                      is_anonymous: bool, display_snapshot: str, text: str, reply_to_id: int = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages "
            "(chat_type, course_id, exam_period_id, user_id, is_anonymous, display_snapshot, text, "
            "created_at, reply_to_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_type, course_id, exam_period_id, user_id, int(is_anonymous), display_snapshot, text,
             _now(), reply_to_id),
        )
        return cur.lastrowid


def edit_own_chat_message(message_id: int, user_id: int, new_text: str) -> bool:
    """فقط اگه فرستنده‌ی واقعیِ پیام خودِ user_id باشه و پیام حذف نشده باشه، متن رو
    عوض می‌کنه. چک مالکیت عمداً همینجا (نه فقط توی bot.py) انجام می‌شه تا این تابع
    از هرجا صدا زده بشه امکان ویرایشِ پیامِ یکیِ دیگه نباشه."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_messages SET text = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND is_deleted = 0",
            (new_text, _now(), message_id, user_id),
        )
        return cur.rowcount > 0


def delete_own_chat_message(message_id: int, user_id: int) -> bool:
    """حذفِ خودِ کاربر از پیامِ خودش (متفاوت از soft_delete_chat_message که برای
    ادمینه و مالکیت رو چک نمی‌کنه)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_messages SET is_deleted = 1, deleted_by = ? WHERE id = ? AND user_id = ?",
            (user_id, message_id, user_id),
        )
        return cur.rowcount > 0


def get_chat_message(message_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None


def get_chat_messages_for_course(course_id, exam_period_id=None, chat_type: str = "exam", limit: int = 500) -> list:
    """پیام‌های خام یک اتاق (بدون حذف‌شده‌ها) -- فقط برای مصرف داخلی (مثلاً ساخت پیش‌نویس AI)،
    نه برای نمایش مستقیم به ادمین ترم؛ برای اون از get_chat_messages_for_admin استفاده کن."""
    with get_conn() as conn:
        if exam_period_id is not None:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE chat_type = ? AND course_id = ? "
                "AND exam_period_id = ? AND is_deleted = 0 ORDER BY id ASC LIMIT ?",
                (chat_type, course_id, exam_period_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE chat_type = ? AND course_id = ? "
                "AND is_deleted = 0 ORDER BY id ASC LIMIT ?",
                (chat_type, course_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_chat_messages_for_admin(chat_type: str, course_id, exam_period_id=None, limit: int = 100) -> list:
    """نسخه‌ی امن برای نمایش به ادمین ترم: هویت پیام‌های ناشناس همیشه پاک شده.
    سوپرادمین هم اگه بخواد هویت رو ببینه باید جدا از reveal_anonymous_sender استفاده کنه."""
    rows = get_chat_messages_for_course(course_id, exam_period_id, chat_type, limit)
    return [_redact_row(r) for r in rows]


def soft_delete_chat_message(message_id: int, deleted_by: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_messages SET is_deleted = 1, deleted_by = ? WHERE id = ?",
            (deleted_by, message_id),
        )
        return cur.rowcount > 0


def reveal_anonymous_sender(requester_id: int, message_id: int):
    """فقط SUPER_ADMIN می‌تونه هویت واقعیِ یک پیام ناشناس رو ببینه. هر فراخوانی موفق
    در audit_log ثبت می‌شه. برای هر نقش دیگه‌ای None برمی‌گردونه (نه Exception، تا
    فراخوانی‌کننده مجبور به هندل صریح نتیجه بشه)."""
    admin = get_admin(requester_id)
    if not admin or admin["role"] != "SUPER_ADMIN":
        return None
    msg = get_chat_message(message_id)
    if not msg:
        return None
    log_action(requester_id, "REVEAL_ANON_SENDER", detail=f"message_id={message_id}")
    return {"user_id": msg["user_id"], "display_snapshot": None}  # display_snapshot عمداً خالی؛ لقب واقعی رو از users بگیر


# ---------- موضوعات (برای شمارش تکرار) ----------

def add_chat_topics(message_id: int, topic_labels: list) -> None:
    with get_conn() as conn:
        for label in topic_labels:
            conn.execute(
                "INSERT INTO chat_topics (message_id, topic_label) VALUES (?, ?)",
                (message_id, label.strip()),
            )


def get_topic_counts_for_period(course_id: int, exam_period_id: int) -> list:
    """[(topic_label, تعداد دانشجوی مستقل), ...] مرتب نزولی. فقط پیام‌های غیرحذف‌شده حساب می‌شن."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ct.topic_label AS topic, COUNT(DISTINCT cm.user_id) AS n "
            "FROM chat_topics ct JOIN chat_messages cm ON ct.message_id = cm.id "
            "WHERE cm.course_id = ? AND cm.exam_period_id = ? AND cm.is_deleted = 0 "
            "GROUP BY ct.topic_label ORDER BY n DESC",
            (course_id, exam_period_id),
        ).fetchall()
        return [(r["topic"], r["n"]) for r in rows]


# ---------- فن‌اوتِ پیام‌ها (برای ویرایش/حذف/ریپلای روی کپی‌های ارسال‌شده) ----------

def record_chat_delivery(message_id: int, recipient_id: int, telegram_message_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_message_deliveries (message_id, recipient_id, telegram_message_id) "
            "VALUES (?, ?, ?) ON CONFLICT(message_id, recipient_id) DO UPDATE SET "
            "telegram_message_id = excluded.telegram_message_id",
            (message_id, recipient_id, telegram_message_id),
        )


def get_deliveries_for_message(message_id: int) -> list:
    """[(recipient_id, telegram_message_id), ...] -- برای پروپاگیت کردنِ ویرایش/حذف/ریکشن
    به همه‌ی کپی‌هایی که برای اعضای اتاق فرستاده شده."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT recipient_id, telegram_message_id FROM chat_message_deliveries WHERE message_id = ?",
            (message_id,),
        ).fetchall()
        return [(r["recipient_id"], r["telegram_message_id"]) for r in rows]


def get_message_id_by_delivery(recipient_id: int, telegram_message_id: int):
    """وقتی کاربر توی تلگرام روی یکی از پیام‌های ربات Reply می‌زنه، از روی
    (خودش، تلگرام‌مسیج‌آیدی) پیدا می‌کنیم که این کپی مربوط به کدوم chat_messages.id بوده."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT message_id FROM chat_message_deliveries WHERE recipient_id = ? AND telegram_message_id = ?",
            (recipient_id, telegram_message_id),
        ).fetchone()
        return row["message_id"] if row else None


# ---------- ریکشن‌ها ----------

CHAT_REACTION_EMOJIS = ["❤️", "😂", "🔥", "💡"]


def toggle_chat_reaction(message_id: int, user_id: int, emoji: str) -> dict:
    """اگه کاربر قبلاً همین ایموجی رو زده بود برمی‌داره، وگرنه اضافه می‌کنه.
    خروجی: شمارشِ فعلیِ همه‌ی ایموجی‌ها روی این پیام، برای بازسازیِ دکمه‌ها."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM chat_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (message_id, user_id, emoji),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM chat_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                (message_id, user_id, emoji),
            )
        else:
            conn.execute(
                "INSERT INTO chat_reactions (message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?)",
                (message_id, user_id, emoji, _now()),
            )
    return get_reaction_counts(message_id)


def get_reaction_counts(message_id: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT emoji, COUNT(*) AS n FROM chat_reactions WHERE message_id = ? GROUP BY emoji",
            (message_id,),
        ).fetchall()
        counts = {e: 0 for e in CHAT_REACTION_EMOJIS}
        for r in rows:
            counts[r["emoji"]] = r["n"]
        return counts


# ---------- نظرسنجی و دو‌راهی سخت ----------

def create_chat_poll(chat_type: str, course_id, creator_id: int, question: str, options: list,
                      kind: str = "poll") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_polls (chat_type, course_id, creator_id, question, options, kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_type, course_id, creator_id, question, json.dumps(options, ensure_ascii=False), kind, _now()),
        )
        return cur.lastrowid


def set_poll_chat_message(poll_id: int, chat_message_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE chat_polls SET chat_message_id = ? WHERE id = ?", (chat_message_id, poll_id))


def get_chat_poll(poll_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_polls WHERE id = ?", (poll_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = json.loads(d["options"])
        return d


def get_poll_by_chat_message(chat_message_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_polls WHERE chat_message_id = ?", (chat_message_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = json.loads(d["options"])
        return d


def vote_chat_poll(poll_id: int, user_id: int, option_index: int) -> bool:
    """رأی می‌ده یا (اگه قبلاً رأی داده بود) رأیش رو عوض می‌کنه. اگه نظرسنجی بسته
    شده باشه رأی ثبت نمی‌شه."""
    with get_conn() as conn:
        poll = conn.execute("SELECT closed_at FROM chat_polls WHERE id = ?", (poll_id,)).fetchone()
        if not poll or poll["closed_at"]:
            return False
        conn.execute(
            "INSERT INTO chat_poll_votes (poll_id, user_id, option_index, voted_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(poll_id, user_id) DO UPDATE SET option_index = excluded.option_index, "
            "voted_at = excluded.voted_at",
            (poll_id, user_id, option_index, _now()),
        )
        return True


def get_poll_results(poll_id: int) -> dict:
    """{option_index: count}. چون کلید (poll_id, user_id) یکتاست، هر کاربر همیشه
    دقیقاً یک رأی داره -- شمارشِ دوباره یا رأیِ تکراری از یه کاربر ممکن نیست."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT option_index, COUNT(*) AS n FROM chat_poll_votes WHERE poll_id = ? GROUP BY option_index",
            (poll_id,),
        ).fetchall()
        return {r["option_index"]: r["n"] for r in rows}


def close_chat_poll(poll_id: int, creator_id: int) -> bool:
    """فقط سازنده‌ی نظرسنجی می‌تونه ببندتش (چک مالکیت همینجا انجام می‌شه)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_polls SET closed_at = ? WHERE id = ? AND creator_id = ? AND closed_at IS NULL",
            (_now(), poll_id, creator_id),
        )
        return cur.rowcount > 0


# ---------- گزارش‌ها ----------

def report_chat_message(message_id: int, reporter_id: int, reason: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_reports (message_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (message_id, reporter_id, reason, _now()),
        )
        return cur.lastrowid


def get_reports_for_admin(course_id=None, status: str = "open", casual_only: bool = False) -> list:
    """گزارش‌های در انتظار، به‌همراه متن پیام -- هویتِ فرستنده‌ی پیام‌های ناشناس حذف شده،
    و هویتِ خودِ گزارش‌دهنده (reporter_id) هم هیچ‌وقت به ادمین ترم نشون داده نمی‌شه.
    casual_only=True یعنی فقط گزارش‌های «گپ دوستانه» (course_id IS NULL)."""
    with get_conn() as conn:
        base = (
            "SELECT r.id AS report_id, r.reason, r.status, r.created_at, "
            "cm.id AS message_id, cm.text, cm.is_anonymous, cm.user_id, cm.display_snapshot, cm.course_id "
            "FROM chat_reports r JOIN chat_messages cm ON r.message_id = cm.id WHERE r.status = ?"
        )
        if casual_only:
            rows = conn.execute(base + " AND cm.course_id IS NULL ORDER BY r.id DESC", (status,)).fetchall()
        elif course_id is not None:
            rows = conn.execute(base + " AND cm.course_id = ? ORDER BY r.id DESC", (status, course_id)).fetchall()
        else:
            rows = conn.execute(base + " ORDER BY r.id DESC", (status,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["is_anonymous"]:
                d["user_id"] = None
                d["display_snapshot"] = "دانشجوی ناشناس"
            out.append(d)
        return out


def resolve_report(report_id: int, resolved_by: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_reports SET status = 'resolved', resolved_by = ? WHERE id = ?",
            (resolved_by, report_id),
        )
        return cur.rowcount > 0


# ---------- پیش‌نویس تجربه‌ی امتحانی ----------

def create_experience_draft(course_id: int, exam_period_id: int, draft_content: str,
                             source_message_ids: list) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO exam_experience_drafts "
            "(course_id, exam_period_id, status, draft_content, source_message_ids, created_at) "
            "VALUES (?, ?, 'pending_review', ?, ?, ?)",
            (course_id, exam_period_id, draft_content, json.dumps(source_message_ids), _now()),
        )
        return cur.lastrowid


def get_experience_draft(draft_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM exam_experience_drafts WHERE id = ?", (draft_id,)).fetchone()
        return dict(row) if row else None


def update_experience_draft_content(draft_id: int, content: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE exam_experience_drafts SET draft_content = ?, version = version + 1 WHERE id = ?",
            (content, draft_id),
        )
        return cur.rowcount > 0


def approve_experience_draft(draft_id: int, approved_by: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT draft_content FROM exam_experience_drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            "UPDATE exam_experience_drafts SET status = 'approved', final_content = ?, "
            "approved_by = ?, approved_at = ? WHERE id = ?",
            (row["draft_content"], approved_by, _now(), draft_id),
        )
        return cur.rowcount > 0


def reject_experience_draft(draft_id: int, rejected_by: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE exam_experience_drafts SET status = 'rejected', approved_by = ?, approved_at = ? WHERE id = ?",
            (rejected_by, _now(), draft_id),
        )
        return cur.rowcount > 0


def list_drafts_for_course(course_id: int, status: str = "pending_review") -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM exam_experience_drafts WHERE course_id = ? AND status = ? ORDER BY id DESC",
            (course_id, status),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- بازخورد دانشجویان روی تجربه‌ی منتشرشده ----------

def add_experience_feedback(draft_id: int, user_id: int, vote: str) -> bool:
    if vote not in ("same", "different"):
        return False
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO exam_experience_feedback (draft_id, user_id, vote, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(draft_id, user_id) DO UPDATE SET vote = excluded.vote",
            (draft_id, user_id, vote, _now()),
        )
        return True


def get_experience_feedback_counts(draft_id: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT vote, COUNT(*) AS n FROM exam_experience_feedback WHERE draft_id = ? GROUP BY vote",
            (draft_id,),
        ).fetchall()
        return {r["vote"]: r["n"] for r in rows}


# ============================================================
# بازی Mafia -- MedVerse Hospital
# ============================================================
#
# همه‌ی این توابع به یک اتاقِ گپ محدود می‌شن (chat_type, course_id) -- دقیقاً
# همون کلیدی که chat_polls و chat_room_members استفاده می‌کنن؛ برای «گپ دوستانه»
# مقدارِ ذخیره‌شده‌ی course_id همیشه CASUAL_ROOM_KEY = 0 هست (نگاه کن به _room_key).

def get_active_mafia_game(chat_type: str, course_id) -> dict:
    """بازیِ لابی یا در حالِ اجرا‌یِ همین اتاق (اگه باشه). برای جلوگیری از
    شروعِ هم‌زمانِ چند بازی در یک گپ استفاده می‌شه."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mafia_games WHERE chat_type = ? AND course_id = ? "
            "AND status IN ('lobby', 'running') ORDER BY id DESC LIMIT 1",
            (chat_type, _room_key(course_id)),
        ).fetchone()
        return dict(row) if row else None


def get_mafia_game(game_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM mafia_games WHERE id = ?", (game_id,)).fetchone()
        return dict(row) if row else None


def create_mafia_game(chat_type: str, course_id, host_id: int, host_display_name: str) -> int:
    """یه بازیِ جدید توی وضعیتِ لابی می‌سازه و میزبان رو به‌عنوانِ اولین بازیکن
    اضافه می‌کنه. فراخوان باید قبلش با get_active_mafia_game مطمئن شده باشه که
    بازیِ فعالِ دیگه‌ای توی همین اتاق نیست."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO mafia_games (chat_type, course_id, host_id, status, phase, created_at) "
            "VALUES (?, ?, ?, 'lobby', 'lobby', ?)",
            (chat_type, _room_key(course_id), host_id, _now()),
        )
        game_id = cur.lastrowid
        conn.execute(
            "INSERT INTO mafia_players (game_id, user_id, display_name, alive, is_host, joined_at) "
            "VALUES (?, ?, ?, 1, 1, ?)",
            (game_id, host_id, host_display_name, _now()),
        )
        return game_id


def join_mafia_game(game_id: int, user_id: int, display_name: str, via_invite: bool = False) -> str:
    """خروجی یکی از این رشته‌هاست: 'joined', 'already_in', 'full', 'not_open'.
    via_invite=True یعنی این ورود از طریقِ دکمه‌ی «پیوستن به بازی»یِ دعوتِ عمومی
    بوده -- فقط برای آمار (invited_joins) استفاده می‌شه."""
    with get_conn() as conn:
        game = conn.execute("SELECT * FROM mafia_games WHERE id = ?", (game_id,)).fetchone()
        if not game or game["status"] != "lobby":
            return "not_open"
        existing = conn.execute(
            "SELECT 1 FROM mafia_players WHERE game_id = ? AND user_id = ?", (game_id, user_id)
        ).fetchone()
        if existing:
            return "already_in"
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM mafia_players WHERE game_id = ?", (game_id,)
        ).fetchone()["n"]
        if count >= MAFIA_MAX_PLAYERS:
            return "full"
        conn.execute(
            "INSERT INTO mafia_players (game_id, user_id, display_name, alive, is_host, joined_at) "
            "VALUES (?, ?, ?, 1, 0, ?)",
            (game_id, user_id, display_name, _now()),
        )
        if via_invite:
            conn.execute(
                "UPDATE mafia_games SET invited_joins = invited_joins + 1 WHERE id = ?", (game_id,)
            )
        return "joined"


def get_started_user_ids(exclude_user_ids=None) -> list:
    """همه‌یِ کاربرانی که تا الان بات رو Start کرده‌ن (یعنی توی جدولِ users ثبت
    شده‌ن) -- برای دعوتِ عمومیِ لابیِ مافیا استفاده می‌شه."""
    exclude = {int(u) for u in (exclude_user_ids or [])}
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [r["user_id"] for r in rows if r["user_id"] not in exclude]


def record_mafia_invites_sent(game_id: int, count: int) -> None:
    """تعدادِ دعوت‌هایی که برای این Lobby ارسال شده رو ثبت می‌کنه (یک‌بار، همون
    لحظه‌ی ساختِ Lobby)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE mafia_games SET invites_sent = invites_sent + ?, "
            "invite_sent_at = COALESCE(invite_sent_at, ?) WHERE id = ?",
            (count, _now(), game_id),
        )


def leave_mafia_game(game_id: int, user_id: int) -> bool:
    """فقط قبل از شروع (وضعیتِ لابی) معنی داره -- بعدِ شروع بازی خروج نداریم."""
    with get_conn() as conn:
        game = conn.execute("SELECT status FROM mafia_games WHERE id = ?", (game_id,)).fetchone()
        if not game or game["status"] != "lobby":
            return False
        cur = conn.execute(
            "DELETE FROM mafia_players WHERE game_id = ? AND user_id = ?", (game_id, user_id)
        )
        return cur.rowcount > 0


def get_mafia_players(game_id: int, alive_only: bool = False) -> list:
    with get_conn() as conn:
        query = "SELECT * FROM mafia_players WHERE game_id = ?"
        if alive_only:
            query += " AND alive = 1"
        query += " ORDER BY joined_at ASC"
        rows = conn.execute(query, (game_id,)).fetchall()
        return [dict(r) for r in rows]


def get_mafia_player(game_id: int, user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mafia_players WHERE game_id = ? AND user_id = ?", (game_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def cancel_mafia_game(game_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mafia_games SET status = 'cancelled', phase = 'ended', ended_at = ? WHERE id = ?",
            (_now(), game_id),
        )


def assign_mafia_roles(game_id: int, role_by_user: dict) -> None:
    """role_by_user: {user_id: role}. بازی رو به وضعیتِ running می‌بره."""
    with get_conn() as conn:
        for user_id, role in role_by_user.items():
            conn.execute(
                "UPDATE mafia_players SET role = ? WHERE game_id = ? AND user_id = ?",
                (role, game_id, user_id),
            )
        conn.execute(
            "UPDATE mafia_games SET status = 'running', phase = 'night', round_number = 1, started_at = ? "
            "WHERE id = ?",
            (_now(), game_id),
        )


def set_mafia_phase(game_id: int, phase: str, round_number: int = None) -> None:
    with get_conn() as conn:
        if round_number is None:
            conn.execute("UPDATE mafia_games SET phase = ? WHERE id = ?", (phase, game_id))
        else:
            conn.execute(
                "UPDATE mafia_games SET phase = ?, round_number = ? WHERE id = ?",
                (phase, round_number, game_id),
            )


def eliminate_mafia_player(game_id: int, user_id: int, round_number: int, by: str) -> None:
    """by: 'night' (حذف شبانه‌ی مافیا) یا 'vote' (رأی‌گیریِ روز)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE mafia_players SET alive = 0, eliminated_round = ?, eliminated_by = ? "
            "WHERE game_id = ? AND user_id = ?",
            (round_number, by, game_id, user_id),
        )


def record_mafia_night_action(game_id: int, round_number: int, actor_id: int, action: str, target_id) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mafia_night_actions (game_id, round_number, actor_id, action, target_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id, round_number, actor_id) DO UPDATE SET target_id = excluded.target_id",
            (game_id, round_number, actor_id, action, target_id, _now()),
        )


def get_mafia_night_actions(game_id: int, round_number: int, action: str = None) -> list:
    with get_conn() as conn:
        if action:
            rows = conn.execute(
                "SELECT * FROM mafia_night_actions WHERE game_id = ? AND round_number = ? AND action = ?",
                (game_id, round_number, action),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mafia_night_actions WHERE game_id = ? AND round_number = ?",
                (game_id, round_number),
            ).fetchall()
        return [dict(r) for r in rows]


def record_mafia_vote(game_id: int, round_number: int, voter_id: int, target_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mafia_votes (game_id, round_number, voter_id, target_id, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(game_id, round_number, voter_id) DO UPDATE SET target_id = excluded.target_id",
            (game_id, round_number, voter_id, target_id, _now()),
        )


def get_mafia_votes(game_id: int, round_number: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mafia_votes WHERE game_id = ? AND round_number = ?",
            (game_id, round_number),
        ).fetchall()
        return [dict(r) for r in rows]


def add_mafia_spectator(game_id: int, user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO mafia_spectators (game_id, user_id, joined_at) VALUES (?, ?, ?)",
            (game_id, user_id, _now()),
        )


def remove_mafia_spectator(game_id: int, user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM mafia_spectators WHERE game_id = ? AND user_id = ?", (game_id, user_id)
        )


def get_mafia_spectators(game_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM mafia_spectators WHERE game_id = ?", (game_id,)
        ).fetchall()
        return [r["user_id"] for r in rows]


def finish_mafia_game(game_id: int, winner: str, mvp_user_id=None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mafia_games SET status = 'finished', phase = 'ended', winner = ?, mvp_user_id = ?, "
            "ended_at = ? WHERE id = ?",
            (winner, mvp_user_id, _now(), game_id),
        )


def log_mafia_game_summary(game_id: int, chat_type: str, course_id, summary: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mafia_game_log (game_id, chat_type, course_id, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (game_id, chat_type, _room_key(course_id), json.dumps(summary, ensure_ascii=False), _now()),
        )
