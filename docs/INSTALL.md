# دليل التثبيت

## المتطلبات
- Odoo 19.0 Community (الفرع `19.0` من https://github.com/odoo/odoo) و PostgreSQL 14+.
- Python: `openpyxl xlsxwriter pytesseract pdf2image pillow-heif` — نظام: `wkhtmltopdf tesseract-ocr tesseract-ocr-ara poppler-utils rtlcss` وخط عربي (Noto).

## الطريقة 1 — Docker (موصى بها)
1. `cp deploy/.env.example .env` وعدّل `DB_PASSWORD` و `ALWABRN_SECRET_KEY` (مفتاح تشفير رموز التكاملات — احتفظ به؛ تغييره يبطل الرموز المحفوظة).
2. `docker compose -f deploy/docker-compose.yml --env-file .env up -d --build`
3. افتح `http://SERVER:8069` وسجل الدخول `admin/admin` ثم غيّر كلمة المرور من «المستخدمون والصلاحيات».
4. للنشر على الإنترنت ضع الخدمة خلف HTTPS (Nginx/Caddy) — الحاوية تعمل بوضع `--proxy-mode`.

## الطريقة 2 — من المصدر (بيئة التطوير التي بُني عليها النظام)
```bash
git clone --depth 1 --branch 19.0 --single-branch https://github.com/odoo/odoo.git odoo-19
python3 -m venv venv && venv/bin/pip install -r odoo-19/requirements.txt openpyxl xlsxwriter pytesseract pdf2image pillow-heif
createuser -d alwabrn    # دور PostgreSQL مستقل
venv/bin/python odoo-19/odoo-bin --addons-path=odoo-19/addons,odoo-19/odoo/addons,./addons \
    -d alwabrn -i alwabrn_warehouse --without-demo=all --stop-after-init
venv/bin/python odoo-19/odoo-bin --addons-path=odoo-19/addons,odoo-19/odoo/addons,./addons -d alwabrn
```

## بعد التثبيت
0. **كتالوج المواد**: ملف «المواد الموحدة 2021.xlsx» غير مرفق بالمستودع العام (أسعار العقد). ضعه في `addons/alwabrn_warehouse/data/materials_2021.xlsx` قبل التثبيت ليستورد آلياً، أو بعد التثبيت: «الإعدادات ← استيراد / فحص كتالوج المواد» ← ارفع الملف ← معاينة ← استيراد.
1. **المستخدمون**: أنشئ المستخدمين وامنح كل واحد دوراً من «نظام الوبران»: مدير النظام / مدير المستودع / موظف مستودع / مشرف مشروع / مدقق.
2. **المواقع**: «عهد المواقع ← المواقع وأوامر العمل» (ينشأ موقع عهدة مخزني لكل موقع تلقائياً).
3. **الرصيد الافتتاحي**: «الإعدادات ← إدخال رصيد افتتاحي» أو رفع Excel بنوع «رصيد افتتاحي» ثم اعتماد المدير.
4. **التكاملات** (اختياري): «الإعدادات ← التكاملات».
   - البريد: أنشئ خادم بريد وارد Gmail/Outlook بمصادقة OAuth (إضافتا `google_gmail` و`microsoft_outlook` مثبتتان)، اجعل «إنشاء سجل جديد» = *دفعة إدخال موحدة*، وحدد المرسلين المصرح لهم.
   - واتساب للأعمال: أدخل Phone Number ID و Access Token و App Secret و Verify Token، وضع رابط الـ Webhook الظاهر بالشاشة في لوحة Meta. تبقى الحالة «يتطلب مصادقة» حتى ينجح تحقق Meta وتصل أول رسالة موقعة.
   - OCR: الافتراضي Tesseract محلي (عربي + إنجليزي). لقراءة أدق/خط اليد: مفتاح AI Vision ثم ضبط `alwabrn.ocr_provider = anthropic`.

## الترقية
`odoo-bin -d alwabrn -u alwabrn_warehouse --stop-after-init` (خذ نسخة احتياطية أولاً).

Project manager  Eng  Ahmed Zahran
