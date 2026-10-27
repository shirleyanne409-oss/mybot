"""📦 دانلود همه‌ی فایل‌های یک محدوده (درس / دسته / استاد) به‌صورت ZIP.

اصول:
- فقط خواندن: هیچ جدول/فایلی در DB تغییر نمی‌کند؛ فایل‌ها با file_id تلگرام نگه‌داری می‌شوند و
  اینجا یکی‌یکی دانلود، در ZIP نوشته و بعد از ارسال حذف می‌شوند.
- محدودیت‌های Bot API: دانلودِ هر فایل تا ۲۰MB، ارسالِ هر ZIP تا ۵۰MB. اگر مجموع زیاد بود چند ZIP
  پشت‌هم ارسال می‌شود (هر بخش ≤ ۴۵MB) و فایل‌های >۲۰MB در گزارشِ «ارسال نشد» می‌آیند.
- منابع: فقط یک کار هم‌زمان (Semaphore)، یک درخواستِ هم‌زمان برای هر کاربر، حداکثر ~۱۱۰MB روی دیسک
  (یک بخش + فایلِ در حالِ دانلود)، پوشه‌ی موقت همیشه پاک می‌شود.
- متن/نوت/لینک‌ها (که فایل نیستند) در یک README.txt داخل ZIP می‌آیند.
"""
import asyncio
import logging
import os
import re
import tempfile
import time
import zipfile

from telegram.error import TelegramError

import db as dbmod

logger = logging.getLogger(__name__)

MAX_PART_BYTES = 45 * 1024 * 1024      # زیر سقفِ ۵۰MB ارسالِ ربات
MAX_SINGLE_BYTES = 20 * 1024 * 1024    # سقفِ دانلودِ Bot API
MAX_TOTAL_BYTES = 400 * 1024 * 1024    # سقفِ کلِ یک درخواست
MAX_FILES = 150
MAX_WAITING = 3
USER_COOLDOWN_SECONDS = 90

_semaphore = asyncio.Semaphore(1)
_waiting = 0
active_users: set = set()
_last_request: dict = {}

_EMOJI_RE = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b00-\u2bff\ufe0f\u200d\u200c]")
_BAD_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_name(name: str, default: str = "بدون‌نام", maxlen: int = 60) -> str:
    n = _EMOJI_RE.sub("", name or "")
    n = _BAD_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip(" .")
    return (n[:maxlen].strip(" .")) or default


def unique_path(used: set, folder: str, base: str, ext: str) -> str:
    """مسیرِ داخلِ ZIP؛ اگر تکراری بود « (2)»، « (3)»… اضافه می‌شود تا چیزی روی هم نوشته نشود."""
    prefix = f"{folder}/" if folder else ""
    candidate = f"{prefix}{base}{ext}"
    i = 2
    while candidate.lower() in used:
        candidate = f"{prefix}{base} ({i}){ext}"
        i += 1
    used.add(candidate.lower())
    return candidate


def cooldown_remaining(user_id: int) -> int:
    last = _last_request.get(user_id)
    if not last:
        return 0
    return max(0, int(USER_COOLDOWN_SECONDS - (time.monotonic() - last)))


def mark_request(user_id: int) -> None:
    _last_request[user_id] = time.monotonic()


def busy() -> bool:
    return _waiting >= MAX_WAITING


def collect(scope: str, ids: list):
    """(title, rows) یا (None, []) اگر محدوده وجود ندارد. فقط همان محدوده‌ی خواسته‌شده برمی‌گردد."""
    with dbmod.get_conn() as conn:
        course = conn.execute(
            "SELECT c.id, c.name, t.name AS term FROM courses c JOIN terms t ON t.id = c.term_id WHERE c.id = ?",
            (ids[0],),
        ).fetchone()
        if not course:
            return None, []
        title = course["name"]
        where, params = "f.course_id = ?", [ids[0]]
        if scope in ("cat", "grp"):
            cat = conn.execute("SELECT id, name, course_id FROM categories WHERE id = ?", (ids[1],)).fetchone()
            if not cat or cat["course_id"] != ids[0]:
                return None, []
            where += " AND f.category_id = ?"
            params.append(ids[1])
            title = f"{course['name']} - {cat['name']}"
        if scope == "grp":
            grp = conn.execute(
                "SELECT id, name, course_id, category_id FROM course_file_groups WHERE id = ?", (ids[2],)
            ).fetchone()
            if not grp or grp["course_id"] != ids[0] or grp["category_id"] != ids[1]:
                return None, []
            where += " AND f.group_id = ?"
            params.append(ids[2])
            title = f"{title} - {grp['name']}"
        rows = conn.execute(
            f"""
            SELECT f.fid, f.type, f.file_id, f.url, f.caption, f.content,
                   cat.name AS category, sg.name AS grp
            FROM course_files f
            JOIN categories cat ON cat.id = f.category_id
            LEFT JOIN course_file_groups sg ON sg.id = f.group_id
            WHERE {where}
            ORDER BY cat.sort_order, cat.id, COALESCE(sg.sort_order, -1), sg.id, f.sort_order, f.id
            """,
            params,
        ).fetchall()
    return clean_name(title, "MedVerse"), [dict(r) for r in rows]


def _folder_for(scope: str, row: dict) -> str:
    parts = []
    if scope == "crs":
        parts.append(clean_name(row["category"], "دسته"))
    if scope in ("crs", "cat") and row.get("grp"):
        parts.append(clean_name(row["grp"], "گروه"))
    return "/".join(parts)


def _build_readme(scope: str, rows: list) -> str:
    lines = ["MedVerse — متن‌ها، نوت‌ها و لینک‌ها", "=" * 36, ""]
    for r in rows:
        where = " / ".join(x for x in (r["category"], r.get("grp")) if x)
        lines.append(f"[{where}] {r['caption']}".strip())
        if r["type"] == "link":
            lines.append(f"  {r['url']}")
        else:
            lines.append((r.get("content") or "").strip())
        lines.append("")
    return "\n".join(lines)


async def _send_part(bot, chat_id: int, path: str, filename: str, caption: str) -> None:
    for attempt in range(2):
        try:
            with open(path, "rb") as fh:
                await bot.send_document(
                    chat_id, fh, filename=filename, caption=caption,
                    read_timeout=240, write_timeout=240, connect_timeout=30,
                )
            return
        except TelegramError as e:
            if attempt == 1 or "retry" not in type(e).__name__.lower():
                raise
            await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 1)


async def run_zip_job(bot, chat_id: int, scope: str, ids: list) -> dict:
    """نتیجه: {'status': 'ok'|'empty'|'missing'|'busy'|'too_many'|'send_failed', 'parts', 'included', 'skipped': [(نام، دلیل)]}"""
    global _waiting
    title, rows = collect(scope, ids)
    if title is None:
        return {"status": "missing", "parts": 0, "included": 0, "skipped": []}
    if not rows:
        return {"status": "empty", "parts": 0, "included": 0, "skipped": []}
    files = [r for r in rows if r["type"] == "file" and r["file_id"]]
    texts = [r for r in rows if not (r["type"] == "file" and r["file_id"])]
    if len(files) > MAX_FILES:
        return {"status": "too_many", "parts": 0, "included": 0, "skipped": [], "count": len(files)}
    if _waiting >= MAX_WAITING:
        return {"status": "busy", "parts": 0, "included": 0, "skipped": []}

    _waiting += 1
    try:
        async with _semaphore:
            return await _build_and_send(bot, chat_id, scope, title, files, texts)
    finally:
        _waiting -= 1


async def _build_and_send(bot, chat_id: int, scope: str, title: str, files: list, texts: list) -> dict:
    skipped, included, parts_sent = [], 0, 0
    used_names: set = set()
    total = 0
    state = {"zf": None, "path": None, "bytes": 0, "count": 0}

    with tempfile.TemporaryDirectory(prefix="mvzip_") as tmp:
        def open_part(n: int) -> None:
            state["path"] = os.path.join(tmp, f"part{n}.zip")
            state["zf"] = zipfile.ZipFile(state["path"], "w", zipfile.ZIP_STORED, allowZip64=True)
            state["bytes"], state["count"] = 0, 0

        async def close_and_send(n: int) -> bool:
            nonlocal parts_sent
            zf = state["zf"]
            if zf is None:
                return True
            await asyncio.to_thread(zf.close)
            state["zf"] = None
            if state["count"] == 0:
                os.remove(state["path"])
                return True
            fname = f"{title}.zip" if n == 1 else f"{title}_part{n}.zip"
            cap = f"📦 {title}" + (f" — بخش {n}" if n > 1 or parts_sent else "")
            try:
                await _send_part(bot, chat_id, state["path"], fname, cap)
            except TelegramError:
                logger.exception("ارسالِ ZIP ناموفق بود")
                return False
            finally:
                try:
                    os.remove(state["path"])
                except OSError:
                    pass
            parts_sent += 1
            return True

        part_no = 1
        open_part(part_no)
        try:
            if texts:
                readme = _build_readme(scope, texts).encode("utf-8")
                await asyncio.to_thread(state["zf"].writestr, "README.txt", readme)
                used_names.add("readme.txt")
                state["bytes"] += len(readme)
                state["count"] += 1

            for idx, r in enumerate(files):
                label = r["caption"] or r["fid"]
                if total >= MAX_TOTAL_BYTES:
                    skipped.append((label, "سقفِ حجمِ کلِ درخواست"))
                    continue
                dl_path = os.path.join(tmp, f"dl_{idx}")
                try:
                    tf = await bot.get_file(r["file_id"])
                    size = getattr(tf, "file_size", None) or 0
                    if size > MAX_SINGLE_BYTES:
                        skipped.append((label, "بزرگ‌تر از ۲۰MB (محدودیت تلگرام)"))
                        continue
                    ext = os.path.splitext(getattr(tf, "file_path", "") or "")[1][:8]
                    await tf.download_to_drive(custom_path=dl_path)
                    size = os.path.getsize(dl_path)
                except TelegramError as e:
                    skipped.append((label, "دانلود نشد: " + str(e)[:60]))
                    continue
                except Exception:
                    logger.exception("خطا در دانلودِ فایل برای ZIP")
                    skipped.append((label, "خطای دانلود"))
                    continue

                if state["bytes"] + size > MAX_PART_BYTES and state["count"] > 0:
                    if not await close_and_send(part_no):
                        return {"status": "send_failed", "parts": parts_sent, "included": included, "skipped": skipped}
                    part_no += 1
                    open_part(part_no)
                arc = unique_path(used_names, _folder_for(scope, r), clean_name(label), ext)
                try:
                    await asyncio.to_thread(state["zf"].write, dl_path, arc)
                except Exception:
                    logger.exception("خطا در نوشتنِ فایل داخل ZIP")
                    skipped.append((label, "خطای فشرده‌سازی"))
                    continue
                finally:
                    try:
                        os.remove(dl_path)
                    except OSError:
                        pass
                state["bytes"] += size
                state["count"] += 1
                included += 1
                total += size

            if not await close_and_send(part_no):
                return {"status": "send_failed", "parts": parts_sent, "included": included, "skipped": skipped}
        finally:
            if state["zf"] is not None:
                try:
                    state["zf"].close()
                except Exception:
                    pass
    return {"status": "ok", "parts": parts_sent, "included": included, "skipped": skipped, "texts": len(texts)}
