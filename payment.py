"""ZarinPal support-payment bridge for MedVerse.

The Telegram bot remains on long polling.  This module runs a tiny HTTP server in
another thread only for the public support page, payment redirects and ZarinPal
callback.

Required environment variables for automatic payments:
    ZARINPAL_MERCHANT_ID=<36-char merchant id>
    PAYMENT_BASE_URL=https://<public Railway domain>
    PAYMENT_SIGNING_SECRET=<long random secret>

Optional:
    PORT=<Railway supplied port, default 8080>

Amounts are stored and sent to ZarinPal in IRR (rial).  UI labels are shown in
toman to avoid ambiguity for users.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

import db as dbmod

logger = logging.getLogger(__name__)

REQUEST_URL = "https://payment.zarinpal.com/pg/v4/payment/request.json"
VERIFY_URL = "https://payment.zarinpal.com/pg/v4/payment/verify.json"
STARTPAY_BASE = "https://payment.zarinpal.com/pg/StartPay/"

# (label in toman, amount in rial)
SUPPORT_AMOUNTS = [
    ("۵۰ هزار تومان", 500_000),
    ("۱۰۰ هزار تومان", 1_000_000),
    ("۲۰۰ هزار تومان", 2_000_000),
    ("۵۰۰ هزار تومان", 5_000_000),
]
_ALLOWED_AMOUNTS = {amount for _, amount in SUPPORT_AMOUNTS}


def _merchant_id() -> str:
    return os.environ.get("ZARINPAL_MERCHANT_ID", "").strip()


def _base_url() -> str:
    return os.environ.get("PAYMENT_BASE_URL", "").strip().rstrip("/")


def _signing_secret() -> str:
    return os.environ.get("PAYMENT_SIGNING_SECRET", "").strip()


def is_configured() -> bool:
    base = _base_url()
    return bool(
        _merchant_id()
        and _signing_secret()
        and base
        and base.lower().startswith("https://")
    )


def public_support_url() -> str | None:
    return f"{_base_url()}/support" if is_configured() else None


def _signature(user_id: int, amount: int) -> str:
    message = f"{int(user_id)}:{int(amount)}".encode("utf-8")
    return hmac.new(_signing_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_personalized_start_url(user_id: int, amount: int) -> str:
    """Signed URL used by the Telegram bot for a known user."""
    if not is_configured():
        raise RuntimeError("ZarinPal payment is not configured")
    amount = int(amount)
    if amount not in _ALLOWED_AMOUNTS:
        raise ValueError("unsupported support amount")
    params = {
        "user_id": str(int(user_id)),
        "amount": str(amount),
        "sig": _signature(int(user_id), amount),
    }
    return f"{_base_url()}/payment/start?{urlencode(params)}"


def _verify_signature(user_id: int, amount: int, signature: str) -> bool:
    if not signature:
        return False
    expected = _signature(user_id, amount)
    return hmac.compare_digest(expected, signature)


def _request_payment(user_id: int | None, amount: int) -> str:
    callback_url = f"{_base_url()}/payment/callback"
    payload = {
        "merchant_id": _merchant_id(),
        "amount": int(amount),
        "currency": "IRR",
        "callback_url": callback_url,
        "description": "حمایت مالی از MedYar / MedVerse",
        "metadata": {
            "order_id": f"support-{int(user_id)}" if user_id else "support-public",
        },
    }
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            REQUEST_URL,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
    data = body.get("data") or {}
    if int(data.get("code") or 0) != 100 or not data.get("authority"):
        errors = body.get("errors") or {}
        raise RuntimeError(f"ZarinPal request rejected: {errors or data}")
    authority = str(data["authority"])
    dbmod.create_support_payment(user_id, int(amount), authority)
    return authority


def _verify_payment(authority: str, amount: int) -> dict:
    payload = {
        "merchant_id": _merchant_id(),
        "amount": int(amount),
        "authority": str(authority),
    }
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            VERIFY_URL,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


def _notify_telegram(user_id: int, amount_rial: int, ref_id) -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token or not user_id:
        return
    toman = int(amount_rial) // 10
    ref_line = f"\nشماره پیگیری: {ref_id}" if ref_id else ""
    text = (
        "⭐ حمایت شما با موفقیت ثبت شد. ممنون که کنار MedYar هستی 💛\n\n"
        f"مبلغ: {toman:,} تومان{ref_line}\n"
        "نشان حامی به‌صورت خودکار برای حسابت فعال شد."
    )
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": int(user_id), "text": text},
            ).raise_for_status()
    except Exception:
        logger.exception("Could not send supporter confirmation to Telegram user %s", user_id)


def _page(title: str, body: str, *, status: int = 200) -> tuple[int, bytes]:
    doc = f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:Tahoma,Arial,sans-serif;background:#f6f7f9;color:#202124;margin:0;padding:24px}}
.card{{max-width:560px;margin:40px auto;background:#fff;border-radius:18px;padding:28px;box-shadow:0 8px 28px rgba(0,0,0,.08)}}
h1{{font-size:24px;margin-top:0}}p{{line-height:1.9}}.btn{{display:block;text-decoration:none;text-align:center;background:#1f7a5b;color:#fff;padding:14px 18px;border-radius:12px;margin:12px 0;font-weight:700}}.muted{{color:#6b7280;font-size:14px}}.ok{{color:#137333}}.bad{{color:#b3261e}}
</style>
</head><body><div class="card"><h1>{html.escape(title)}</h1>{body}</div></body></html>"""
    return status, doc.encode("utf-8")


class PaymentHandler(BaseHTTPRequestHandler):
    server_version = "MedVersePayment/1.0"

    def log_message(self, fmt, *args):
        logger.info("payment-http: " + fmt, *args)

    def _send(self, status: int, content: bytes, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        if parsed.path == "/support":
            if not is_configured():
                status, page = _page(
                    "حمایت مالی MedYar",
                    '<p class="bad">درگاه پرداخت در حال حاضر فعال نیست.</p>',
                    status=503,
                )
                self._send(status, page)
                return
            buttons = "".join(
                f'<a class="btn" href="/payment/start?amount={amount}">{html.escape(label)}</a>'
                for label, amount in SUPPORT_AMOUNTS
            )
            status, page = _page(
                "🤍 حمایت مالی از MedYar",
                "<p>برای کمک به هزینه‌های سرور و پایداری سرویس، مبلغ حمایت را انتخاب کن.</p>"
                + buttons
                + '<p class="muted">پرداخت از طریق درگاه زرین‌پال انجام می‌شود.</p>',
            )
            self._send(status, page)
            return

        if parsed.path == "/payment/start":
            if not is_configured():
                status, page = _page("خطا", '<p class="bad">درگاه پرداخت فعال نیست.</p>', status=503)
                self._send(status, page)
                return
            try:
                amount = int((query.get("amount") or [""])[0])
            except ValueError:
                amount = 0
            if amount not in _ALLOWED_AMOUNTS:
                status, page = _page("خطا", '<p class="bad">مبلغ انتخاب‌شده معتبر نیست.</p>', status=400)
                self._send(status, page)
                return

            raw_uid = (query.get("user_id") or [""])[0].strip()
            user_id = None
            if raw_uid:
                try:
                    user_id = int(raw_uid)
                except ValueError:
                    user_id = None
                sig = (query.get("sig") or [""])[0].strip()
                if not user_id or not _verify_signature(user_id, amount, sig):
                    status, page = _page("خطا", '<p class="bad">لینک پرداخت نامعتبر یا دستکاری شده است.</p>', status=403)
                    self._send(status, page)
                    return

            try:
                authority = _request_payment(user_id, amount)
            except Exception as exc:
                logger.exception("ZarinPal payment request failed")
                status, page = _page(
                    "خطای درگاه",
                    '<p class="bad">ارتباط با درگاه برقرار نشد. لطفاً کمی بعد دوباره امتحان کن.</p>',
                    status=502,
                )
                self._send(status, page)
                return
            self._redirect(STARTPAY_BASE + authority)
            return

        if parsed.path == "/payment/callback":
            authority = (query.get("Authority") or query.get("authority") or [""])[0].strip()
            gateway_status = (query.get("Status") or query.get("status") or [""])[0].strip().upper()
            if not authority:
                status, page = _page("خطا", '<p class="bad">شناسه پرداخت دریافت نشد.</p>', status=400)
                self._send(status, page)
                return

            payment = dbmod.get_support_payment_by_authority(authority)
            if not payment:
                status, page = _page("خطا", '<p class="bad">این پرداخت در MedYar شناخته نشد.</p>', status=404)
                self._send(status, page)
                return

            if payment.get("status") == "paid":
                ref_id = payment.get("ref_id")
                body = '<p class="ok">✅ این پرداخت قبلاً با موفقیت ثبت شده است.</p>'
                if ref_id:
                    body += f'<p>شماره پیگیری: <b>{html.escape(str(ref_id))}</b></p>'
                status, page = _page("پرداخت موفق", body)
                self._send(status, page)
                return

            if gateway_status != "OK":
                dbmod.set_support_payment_status(
                    authority,
                    "cancelled",
                    gateway_message=f"Callback Status={gateway_status or 'N/A'}",
                )
                status, page = _page(
                    "پرداخت انجام نشد",
                    '<p class="bad">پرداخت لغو شد یا ناموفق بود و هیچ مبلغ موفقی ثبت نشد.</p>'
                    '<a class="btn" href="/support">تلاش دوباره</a>',
                )
                self._send(status, page)
                return

            try:
                verify_body = _verify_payment(authority, int(payment["amount"]))
            except Exception:
                logger.exception("ZarinPal verify failed for %s", authority)
                status, page = _page(
                    "بررسی پرداخت",
                    '<p class="bad">در بررسی نهایی پرداخت خطای ارتباطی رخ داد. پرداخت را دوباره انجام نده؛ '
                    'می‌توانی همین صفحه را دوباره باز کنی تا وضعیت مجدداً بررسی شود.</p>',
                    status=502,
                )
                self._send(status, page)
                return

            data = verify_body.get("data") or {}
            errors = verify_body.get("errors") or {}
            try:
                code = int(data.get("code"))
            except (TypeError, ValueError):
                try:
                    code = int(errors.get("code"))
                except (TypeError, ValueError, AttributeError):
                    code = 0
            message = data.get("message") or (errors.get("message") if isinstance(errors, dict) else None) or ""
            ref_id = data.get("ref_id")

            # Official ZarinPal docs: 100 = verified now, 101 = already verified.
            if code in (100, 101):
                paid = dbmod.mark_support_payment_paid(
                    authority,
                    ref_id=ref_id,
                    gateway_code=code,
                    gateway_message=message or "Verified",
                )
                if paid and paid.get("user_id"):
                    _notify_telegram(int(paid["user_id"]), int(paid["amount"]), paid.get("ref_id"))
                body = '<p class="ok">✅ حمایت شما با موفقیت ثبت شد. خیلی ممنون 💛</p>'
                if ref_id:
                    body += f'<p>شماره پیگیری: <b>{html.escape(str(ref_id))}</b></p>'
                if paid and paid.get("user_id"):
                    body += '<p>نشان حامی در MedYar برای حساب شما فعال شد.</p>'
                status, page = _page("پرداخت موفق", body)
                self._send(status, page)
                return

            dbmod.set_support_payment_status(
                authority,
                "failed",
                gateway_code=code,
                gateway_message=message or str(errors)[:500],
            )
            status, page = _page(
                "پرداخت تأیید نشد",
                '<p class="bad">زرین‌پال این تراکنش را تأیید نکرد. اگر مبلغی از حساب کم شده، '
                'برای بررسی از شماره پیگیری بانکی/زرین‌پال استفاده کن.</p>'
                '<a class="btn" href="/support">بازگشت به صفحه حمایت</a>',
                status=400,
            )
            self._send(status, page)
            return

        status, page = _page("یافت نشد", '<p>صفحه موردنظر پیدا نشد.</p>', status=404)
        self._send(status, page)


def start_payment_server() -> ThreadingHTTPServer | None:
    """Starts the callback/public-payment web server in a daemon thread."""
    if not is_configured():
        logger.warning(
            "ZarinPal payment bridge is disabled. Set ZARINPAL_MERCHANT_ID, "
            "PAYMENT_BASE_URL (https) and PAYMENT_SIGNING_SECRET to enable it."
        )
        return None
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), PaymentHandler)
    thread = threading.Thread(target=server.serve_forever, name="payment-http", daemon=True)
    thread.start()
    logger.info("Payment HTTP server started on 0.0.0.0:%s; public page: %s", port, public_support_url())
    return server
