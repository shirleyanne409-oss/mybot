import db as dbmod

TERM = "رفرنس‌های علوم‌پایه"
COURSES = [
    "آناتومی", "جنین‌شناسی", "بافت‌شناسی", "فیزیولوژی", "بیوشیمی",
    "اصول خدمات سلامت", "روان‌شناسی سلامت", "زبان تخصصی", "ایمنی شناسی", "سایر",
]

for course in COURSES:
    ok = dbmod.set_single_category(TERM, course, "📚 رفرنس‌ها")
    print(f"{course}: {'✅' if ok else '❌ پیدا نشد'}")
