#!/usr/bin/env python3
"""
اسکریپت ساده‌ی Backup برای MedVerse.

از Environment Variableهای همین پروژه استفاده می‌کنه:
    MEDVERSE_DATA_DIR  -- پوشه‌ی داده‌ی Runtime (روی Railway معمولاً /data)
    MEDVERSE_DB_FILE   -- مسیر کامل فایل دیتابیس (اگه ست نشه، از <data-dir>/medverse.db استفاده می‌شه)

استفاده (روی Railway، از طریق Shell/Run Command همون سرویس):
    python backup.py
    python backup.py --out /data/backups

استفاده‌ی محلی/VPS:
    python backup.py --data-dir . --out ./backups
    python backup.py --data-dir /opt/medverse/data --out ./backups

خروجی: یه پوشه‌ی timestamp-دار داخل --out شامل:
    - medverse.db  (کپیِ امن -- از طریق SQLite Backup API، حتی اگه هم‌زمان
      برنامه در حال نوشتن روی دیتابیس باشه هم مشکلی پیش نمی‌آد و فایل ناقص/خراب نمی‌شه)
    - هر کدوم از JSONهای Runtime که پیدا بشن (analytics.json, pending_submissions.json, ...)
و در آخر یه آرشیو .tar.gz از همون پوشه هم می‌سازه که یک‌جا قابل دانلود/انتقاله.

Restore: فقط کافیه پوشه رو باز کنی (یا tar.gz رو extract کنی) و:
    - medverse.db رو در مسیرِ MEDVERSE_DB_FILE بذاری
    - بقیه‌ی JSONها رو در MEDVERSE_DATA_DIR بذاری
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime

RUNTIME_JSON_FILES = [
    "analytics.json",
    "pending_submissions.json",
    "requests.json",
    "class_schedule.json",
    "class_exceptions.json",
    "fixed_events.json",
    "personal_schedule.json",
    "reminder_log.json",
]


def backup_sqlite(src_path: str, dst_path: str) -> None:
    """با SQLite Backup API کپی می‌گیره -- امن حتی اگه دیتابیس هم‌زمان در حال
    نوشتنه (برخلاف cp/shutil.copy ساده که ممکنه وسط یه Write فایل رو کپی کنه)."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup ساده برای داده‌های MedVerse")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("MEDVERSE_DATA_DIR", "").strip() or ".",
        help="پوشه‌ی داده‌ی Runtime (پیش‌فرض: MEDVERSE_DATA_DIR یا پوشه‌ی فعلی)",
    )
    parser.add_argument(
        "--db-file",
        default=os.environ.get("MEDVERSE_DB_FILE", "").strip() or None,
        help="مسیر کامل فایل دیتابیس (پیش‌فرض: MEDVERSE_DB_FILE یا <data-dir>/medverse.db)",
    )
    parser.add_argument(
        "--out",
        default="./backups",
        help="پوشه‌ای که Backupها توش ذخیره می‌شن (پیش‌فرض: ./backups)",
    )
    args = parser.parse_args()

    data_dir = args.data_dir
    db_file = args.db_file or os.path.join(data_dir, "medverse.db")

    if not os.path.exists(db_file):
        print(f"❌ فایل دیتابیس پیدا نشد: {db_file}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(args.out, f"medverse_backup_{stamp}")
    os.makedirs(backup_dir, exist_ok=True)

    print(f"📦 در حال Backup از دیتابیس: {db_file}")
    backup_sqlite(db_file, os.path.join(backup_dir, "medverse.db"))

    copied, missing = [], []
    for name in RUNTIME_JSON_FILES:
        src = os.path.join(data_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(backup_dir, name))
            copied.append(name)
        else:
            missing.append(name)

    archive_path = f"{backup_dir}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(backup_dir, arcname=os.path.basename(backup_dir))

    print(f"✅ Backup کامل شد: {backup_dir}")
    print(f"✅ آرشیو فشرده: {archive_path}")
    print(f"   فایل‌های JSON کپی‌شده: {', '.join(copied) if copied else '(هیچ‌کدام پیدا نشد -- طبیعیه اگه هنوز ساخته نشدن)'}")
    if missing:
        print(f"   (پیدا نشدن، مشکلی نیست چون فقط وقتی اون قابلیت استفاده بشه ساخته می‌شن: {', '.join(missing)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
