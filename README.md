# educa24-data

معطيات «مساري المهني» في تطبيق Educa24 (ملف `masari.json`).

- يقرأ التطبيق الملف من `https://raw.githubusercontent.com/<الحساب>/educa24-data/main/masari.json`.
- الروبوت (`robot/masari_robot.py`) يشتغل مجاناً كل ساعتين عبر GitHub Actions: يتابع اللوائح والنتائج، ويحفظ فقط بصمات مشفرة لأرقام التأجير (لا أسماء ولا أرقام واضحة)، ويرسل إشعاراً للمعنيين.
- المفاتيح السرية في Settings → Secrets and variables → Actions:
  - `GEMINI_API_KEY` (اختياري، من مشروع Google بدون فوترة)
  - `FCM_SERVICE_ACCOUNT` (ملف حساب الخدمة لـ Firebase educa24-app)
- تشغيل يدوي: Actions → masari-robot → Run workflow.
