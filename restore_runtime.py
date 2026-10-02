#!/usr/bin/env python3
"""
Restore only MedVerse runtime JSON files into MEDVERSE_DATA_DIR.

Important:
- This script NEVER overwrites medverse.db.
- It downloads one .tar.gz backup archive and extracts only known runtime JSON files.
- Intended for one-time migration to a new Railway volume.
"""

import os
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RUNTIME_JSON_FILES = {
    "analytics.json",
    "class_exceptions.json",
    "class_schedule.json",
    "fixed_events.json",
    "pending_submissions.json",
    "personal_schedule.json",
    "reminder_log.json",
    "requests.json",
}

BACKUP_URL = os.environ.get("MEDVERSE_RUNTIME_BACKUP_URL", "").strip()
DATA_DIR = os.environ.get("MEDVERSE_DATA_DIR", "").strip() or "/data"


def _fail(message: str, code: int = 1) -> int:
    print(f"❌ {message}", file=sys.stderr)
    return code


def main() -> int:
    if not BACKUP_URL:
        return _fail("MEDVERSE_RUNTIME_BACKUP_URL تنظیم نشده.")

    data_dir = Path(DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="medverse_restore_") as tmpdir:
        archive_path = Path(tmpdir) / "backup.tar.gz"

        print("⬇️ در حال دانلود بکاپ Runtime ...")
        try:
            urllib.request.urlretrieve(BACKUP_URL, archive_path)
        except Exception as e:
            return _fail(f"دانلود بکاپ ناموفق بود: {e}")

        restored = []
        missing = []

        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                members_by_name = {}
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    base = Path(member.name).name
                    if base in RUNTIME_JSON_FILES:
                        members_by_name[base] = member

                for name in sorted(RUNTIME_JSON_FILES):
                    member = members_by_name.get(name)
                    if member is None:
                        missing.append(name)
                        continue

                    source = tar.extractfile(member)
                    if source is None:
                        missing.append(name)
                        continue

                    target = data_dir / name
                    with source, open(target, "wb") as out:
                        out.write(source.read())
                    restored.append(name)

        except tarfile.TarError as e:
            return _fail(f"آرشیو معتبر tar.gz نیست: {e}")
        except Exception as e:
            return _fail(f"خطا هنگام استخراج فایل‌های Runtime: {e}")

    print("✅ Restore فایل‌های Runtime تمام شد.")
    print("✅ medverse.db دست‌نخورده باقی ماند.")
    if restored:
        print("فایل‌های منتقل‌شده:")
        for name in restored:
            print(f"  - {name}")
    else:
        print("⚠️ هیچ فایل Runtime شناخته‌شده‌ای داخل بکاپ پیدا نشد.")

    if missing:
        print("ℹ️ فایل‌هایی که در بکاپ نبودند (ممکن است طبیعی باشد):")
        for name in missing:
            print(f"  - {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
