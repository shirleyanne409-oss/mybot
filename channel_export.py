"""
export به چنل آرشیو -- تمام فایل‌های بات رو دسته‌بندی‌شده داخل یه چنل تلگرامی می‌فرسته.

ساختار ارسال: گروه › ترم › درس › دسته (› زیرگروه)
- برای هر درس یه پیام عنوان، بعد برای هر دسته یه پیام عنوان و پشتش فایل‌ها
- فایل‌ها با file_id ذخیره‌شده ارسال می‌شن (آپلود دوباره نیست)
- زیر هر فایل هشتگ درس/دسته هست تا داخل چنل قابل جستجو باشه
- لاگ ارسال‌شده‌ها داخل channel_export_log.json ذخیره می‌شه؛ اجرای دوباره فقط
  فایل‌های جدید رو می‌فرسته (و اگه وسط کار قطع شد، از همون‌جا ادامه می‌ده)
"""
import asyncio
import json
import os
import re

from telegram.error import RetryAfter, TelegramError

import db as dbmod

ARCHIVE_CHANNEL_ID = int(os.environ.get("ARCHIVE_CHANNEL_ID", "-1004312732334"))
SEND_DELAY = 3.2  # ثانیه بین پیام‌ها؛ سقف تلگرام برای چنل ~۲۰ پیام در دقیقه‌ست

_data_dir = (
    os.environ.get("MEDVERSE_DATA_DIR", "").strip()
    or os.path.dirname(dbmod.DB_FILE)
    or "."
)
LOG_PATH = os.path.join(_data_dir, "channel_export_log.json")

_running = False


def _hashtag(text: str) -> str:
    t = re.sub(r"[^\w\s]", "", text or "", flags=re.UNICODE).strip()
    t = re.sub(r"\s+", "_", t)
    return f"#{t}" if t else ""


def _load_log() -> set:
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_log(sent: set) -> None:
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(sent), f, ensure_ascii=False)
    os.replace(tmp, LOG_PATH)


def build_plan() -> list:
    """لیستِ مرتب‌شده‌ی درس‌ها با دسته‌ها و فایل‌هاشون از دیتابیس."""
    plan = []
    with dbmod.get_conn() as conn:
        courses = conn.execute(
            """
            SELECT c.id, c.name AS course, t.name AS term, g.name AS grp
            FROM courses c
            JOIN terms t ON t.id = c.term_id
            JOIN groups g ON g.id = t.group_id
            ORDER BY g.sort_order, g.id, t.sort_order, t.id, c.sort_order, c.id
            """
        ).fetchall()
        for c in courses:
            cats = conn.execute(
                "SELECT id, name FROM categories WHERE course_id=? ORDER BY sort_order, id",
                (c["id"],),
            ).fetchall()
            cat_items = []
            for cat in cats:
                files = conn.execute(
                    """
                    SELECT f.fid, f.type, f.file_id, f.url, f.caption, f.description,
                           f.content, sg.name AS subgroup
                    FROM course_files f
                    LEFT JOIN course_file_groups sg ON sg.id = f.group_id
                    WHERE f.course_id=? AND f.category_id=?
                    ORDER BY COALESCE(sg.sort_order, -1), COALESCE(sg.id, -1), f.sort_order, f.id
                    """,
                    (c["id"], cat["id"]),
                ).fetchall()
                files = [dict(f) for f in files]
                if files:
                    cat_items.append({"category": cat["name"], "files": files})
            if cat_items:
                plan.append(
                    {
                        "group": c["grp"],
                        "term": c["term"],
                        "course": c["course"],
                        "categories": cat_items,
                    }
                )
    return plan


def count_plan(plan: list, sent: set) -> tuple:
    total = new = 0
    for c in plan:
        for cat in c["categories"]:
            for f in cat["files"]:
                total += 1
                if f["fid"] not in sent:
                    new += 1
    return total, new


async def _send(coro_factory):
    """ارسال با رعایت RetryAfter؛ بعد از هر ارسال تاخیر."""
    while True:
        try:
            msg = await coro_factory()
            await asyncio.sleep(SEND_DELAY)
            return msg
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)


async def run_export(bot, notify_chat_id: int) -> None:
    global _running
    if _running:
        return
    _running = True
    sent = _load_log()
    plan = build_plan()
    total, new = count_plan(plan, sent)
    done = failed = 0
    failed_items = []
    try:
        await bot.send_message(
            notify_chat_id,
            f"🚀 شروع export: {new} فایل جدید از {total} فایل (حدود {int(new * SEND_DELAY / 60) + 1} دقیقه)",
        )
        for c in plan:
            pending_cats = [
                {
                    "category": cat["category"],
                    "files": [f for f in cat["files"] if f["fid"] not in sent],
                }
                for cat in c["categories"]
            ]
            pending_cats = [x for x in pending_cats if x["files"]]
            if not pending_cats:
                continue

            tags = " ".join(
                x for x in (_hashtag(c["group"]), _hashtag(c["term"]), _hashtag(c["course"])) if x
            )
            header = f"━━━━━━━━━━\n📚 {c['course']}\n{c['group']} › {c['term']}\n\n{tags}"
            await _send(lambda h=header: bot.send_message(ARCHIVE_CHANNEL_ID, h[:4000]))

            for cat in pending_cats:
                cat_tag = _hashtag(cat["category"])
                course_tag = _hashtag(c["course"])
                await _send(
                    lambda t=f"▫️ {cat['category']}\n{course_tag} {cat_tag}": bot.send_message(
                        ARCHIVE_CHANNEL_ID, t[:4000]
                    )
                )
                for f in cat["files"]:
                    cap = (f["caption"] or "").strip()
                    if f["subgroup"]:
                        cap = f"[{f['subgroup']}] {cap}"
                    tail = f"\n\n{course_tag} {cat_tag}".rstrip()
                    ftype = f["type"]
                    try:
                        if ftype == "link":
                            text = f"🔗 {cap}\n{f['url']}{tail}"
                            await _send(lambda t=text: bot.send_message(ARCHIVE_CHANNEL_ID, t[:4000]))
                        elif ftype in ("text", "note"):
                            body = f["content"] or cap
                            text = f"📝 {cap}\n\n{body}" if cap and ftype == "note" else body
                            text = f"{text}{tail}"
                            await _send(lambda t=text: bot.send_message(ARCHIVE_CHANNEL_ID, t[:4000]))
                        elif f["file_id"]:
                            desc = (f["description"] or "").strip()
                            text = cap + (f"\n\n📝 {desc}" if desc else "")
                            budget = 1024 - len(tail)
                            text = text[:budget] + tail
                            await _send(
                                lambda fi=f["file_id"], t=text: bot.send_document(
                                    ARCHIVE_CHANNEL_ID, fi, caption=t
                                )
                            )
                        else:
                            continue
                        sent.add(f["fid"])
                        done += 1
                        if done % 20 == 0:
                            _save_log(sent)
                        if done % 100 == 0:
                            await bot.send_message(notify_chat_id, f"⏳ {done}/{new} ارسال شد")
                    except TelegramError as e:
                        failed += 1
                        failed_items.append(f"{c['course']} / {cat['category']} / {cap[:40]}: {e}")
        _save_log(sent)
        report = f"✅ تمام شد. ارسال‌شده: {done} | خطا: {failed}"
        if failed_items:
            report += "\n\n" + "\n".join(failed_items[:15])
        await bot.send_message(notify_chat_id, report[:4000])
    except Exception as e:  # noqa: BLE001
        _save_log(sent)
        await bot.send_message(notify_chat_id, f"❌ export قطع شد: {e}\nارسال‌شده تا اینجا: {done}. دوباره اجرا کن، ادامه می‌ده.")
    finally:
        _running = False


def is_running() -> bool:
    return _running
