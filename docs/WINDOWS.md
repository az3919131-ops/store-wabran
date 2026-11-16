# تعليمات التشغيل على ويندوز

## الخيار أ — Docker Desktop (الأسهل)
1. ثبّت **Docker Desktop for Windows** (يتطلب WSL2) و **Git for Windows**.
2. افتح PowerShell:
```powershell
git clone https://github.com/<الحساب>/alwabrn-smart-inventory.git
cd alwabrn-smart-inventory
copy deploy\.env.example .env
notepad .env        # غيّر DB_PASSWORD و ALWABRN_SECRET_KEY
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```
3. افتح المتصفح على `http://localhost:8069`.
4. من أجهزة الشبكة والجوال: `http://<IP-الجهاز>:8069` (اسمح بالمنفذ 8069 في جدار الحماية). الكاميرا في المتصفح تتطلب HTTPS خارج `localhost`؛ بدون HTTPS استخدم «اختيار صور» من معرض الجوال.
5. اختصار سطح المكتب (مثل اختصار Odoo الموجود لديك): Chrome ← ⋮ ← حفظ ومشاركة ← «تثبيت الصفحة كتطبيق».

أوامر يومية:
```powershell
docker compose -f deploy/docker-compose.yml ps        # الحالة
docker compose -f deploy/docker-compose.yml logs -f odoo
docker compose -f deploy/docker-compose.yml stop      # إيقاف
docker compose -f deploy/docker-compose.yml start     # تشغيل
```

## الخيار ب — مثبّت Odoo 19 لويندوز (موجود مسبقاً)
1. انسخ مجلد `addons\alwabrn_warehouse` إلى مجلد إضافات مخصص، مثلاً `C:\odoo-custom-addons\`.
2. في `C:\Program Files\Odoo 19...\server\odoo.conf` أضف المسار إلى `addons_path`.
3. ثبّت مكتبات بايثون داخل بايثون Odoo: `"C:\Program Files\Odoo 19...\python\python.exe" -m pip install openpyxl xlsxwriter pytesseract pdf2image pillow-heif`.
4. ثبّت Tesseract لويندوز مع اللغة العربية وأضفه إلى PATH (لميزة OCR)، و wkhtmltopdf (يأتي مع المثبّت).
5. أعد تشغيل خدمة Odoo، ثم **أنشئ قاعدة بيانات جديدة للتجربة** (لا تثبّت على قاعدة الإنتاج مباشرة)، فعّل وضع المطور ← التطبيقات ← تحديث القائمة ← ثبّت «نظام الوبران الذكي».

> لا تثبّت الإضافة على قاعدة بيانات إنتاجية قبل تجربتها على نسخة منها.

Project manager  Eng  Ahmed Zahran
