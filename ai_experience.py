"""
ai_experience.py -- تحلیل گفتگوی «گپ امتحانی» با AI و ساخت پیش‌نویس «تجربه‌ی امتحان».

این ماژول عمداً از bot.py و db.py جداست تا:
  ۱) منطق فراخوانی مدل (پرامپت، پارس خروجی، خطاها) یک‌جا و قابل‌تست باشه.
  ۲) bot.py که همین الان خیلی بزرگه، بزرگ‌تر نشه.

نکته‌ی مهم درباره‌ی شمارش تکرار: این ماژول از مدل فقط می‌خواد بگه هر پیام
درباره‌ی چه موضوع‌هاییه (سطح پیام)، نه اینکه خودش تعداد دانشجوها رو بشمره.
شمارشِ «چند دانشجوی مستقل» همیشه در db.py با COUNT(DISTINCT user_id) انجام
می‌شه (نگاه کن به db.add_chat_topics / db.get_topic_counts_for_period)،
چون اعتماد به AI برای این شمارش قابل‌اطمینان نیست.

پیکربندی: کلید API باید در متغیر محیطی ANTHROPIC_API_KEY باشه. اگه نبود،
generate_topics_for_message و build_experience_draft با خطای واضح شکست
می‌خورن (به‌جای سکوت یا حدس زدن).
"""

import json
import os

import httpx

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
REQUEST_TIMEOUT = 30


class AIConfigError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIConfigError(
            "ANTHROPIC_API_KEY تنظیم نشده. برای فعال‌سازی ساخت خودکار پیش‌نویسِ تجربه‌ی "
            "امتحان، این متغیر محیطی رو ست کن."
        )
    return key


async def _call_claude(system: str, user_content: str) -> str:
    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": 2000,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(parts).strip()


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


TOPIC_SYSTEM_PROMPT = (
    "تو یه دستیار تحلیل پیام هستی. یک پیام دانشجویی درباره‌ی امتحان یک درس پزشکی رو "
    "می‌خونی و فقط موضوع‌های کلیدیِ مرتبط با امتحان که توش ذکر شده رو به‌صورت آرایه‌ی "
    "JSON از رشته‌های کوتاه فارسی برمی‌گردونی (مثلاً منبع مطالعه، نام یک مبحث، نوع سؤال). "
    "اگه پیام ربطی به امتحان نداشت، آرایه‌ی خالی برگردون. "
    "خروجی فقط و فقط یک آرایه‌ی JSON باشه، بدون هیچ توضیح یا Markdown اضافه."
)


async def extract_topics(message_text: str) -> list:
    """موضوع‌های یک پیام رو استخراج می‌کنه. خطای شبکه/پارس رو می‌بلعه و [] برمی‌گردونه
    (چون این تابع روی هر پیام صدا زده می‌شه؛ نباید یه خطای شبکه جریان چت رو خراب کنه)."""
    try:
        raw = await _call_claude(TOPIC_SYSTEM_PROMPT, message_text)
        parsed = json.loads(_strip_json_fences(raw))
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()][:8]
    except AIConfigError:
        raise
    except Exception:
        pass
    return []


DRAFT_SYSTEM_PROMPT = (
    "تو دستیار ساخت «پیش‌نویس تجربه‌ی امتحان» برای یک اپ آموزشی پزشکی هستی. "
    "مجموعه‌ای از پیام‌های ناشناس‌سازی‌شده‌ی دانشجویان درباره‌ی امتحانِ یک درس رو می‌گیری "
    "(هر پیام با شمارهٔ ردیف). باید یک خروجی JSON با این ساختار دقیق بسازی:\n"
    "{\n"
    '  "sources": ["..."],           // منابع مطالعه‌ای که ذکر شدن\n'
    '  "hot_topics": ["..."],        // مباحثی که چند پیام مستقل ذکر کردن\n'
    '  "question_types": "...",      // یک پاراگراف کوتاه درباره‌ی نوع سؤالات\n'
    '  "difficulty": "...",          // آسان/متوسط/سخت + یک جمله توضیح\n'
    '  "key_notes": ["..."],         // نکات مهمِ اجماعی\n'
    '  "unconfirmed_reports": ["..."] // ادعاهایی که فقط یک پیام مطرح کرده و نباید قطعی فرض بشن\n'
    "}\n"
    "فقط از محتوای پیام‌های داده‌شده استفاده کن، چیزی رو از خودت نساز. "
    "خروجی فقط یک آبجکت JSON باشه، بدون Markdown یا توضیح اضافه."
)


async def build_experience_draft(course_name: str, term_name: str, messages: list) -> dict:
    """messages: [{'id': int, 'text': str}, ...] (بدون user_id -- عمداً حذف شده تا حتی
    اگه این تابع رو یه توسعه‌دهنده‌ی دیگه صدا زد، امکان نشتِ هویت از این مسیر نباشه).
    خروجی: dict ساخت‌یافته طبق DRAFT_SYSTEM_PROMPT."""
    numbered = "\n".join(f"[{m['id']}] {m['text']}" for m in messages)
    user_content = f"درس: {course_name} ({term_name})\n\nپیام‌ها:\n{numbered}"
    raw = await _call_claude(DRAFT_SYSTEM_PROMPT, user_content)
    parsed = json.loads(_strip_json_fences(raw))
    if not isinstance(parsed, dict):
        raise ValueError("خروجی مدل یک آبجکت JSON نبود.")
    return parsed


def render_draft_text(course_name: str, period_label: str, draft: dict, topic_counts: list) -> str:
    """dict ساخت‌یافته رو به متن نهاییِ قابل‌نمایش (همون فرمتی که در spec خواسته شده) تبدیل می‌کنه."""
    lines = [f"🩺 تجربه امتحان {course_name}", f"📅 {period_label}", ""]

    sources = draft.get("sources") or []
    if sources:
        lines.append("📚 منابع مورد استفاده")
        lines += [f"• {s}" for s in sources]
        lines.append("")

    if topic_counts:
        lines.append("🔥 مباحث پرتکرار")
        for topic, n in topic_counts[:10]:
            icon = "🔥" if n >= 5 else ("⭐" if n >= 3 else "📌")
            lines.append(f"{icon} {topic} — {n} دانشجو")
        lines.append("")

    if draft.get("question_types"):
        lines += ["❓ تیپ سؤالات", draft["question_types"], ""]

    if draft.get("difficulty"):
        lines += ["📊 سطح دشواری", draft["difficulty"], ""]

    key_notes = draft.get("key_notes") or []
    if key_notes:
        lines.append("⭐️ نکات مهم")
        lines += [f"• {n}" for n in key_notes]
        lines.append("")

    unconfirmed = draft.get("unconfirmed_reports") or []
    if unconfirmed:
        lines.append("⚠️ گزارش‌های تأییدنشده (فقط یک دانشجو گفته)")
        lines += [f"• {u}" for u in unconfirmed]
        lines.append("")

    return "\n".join(lines).strip()
