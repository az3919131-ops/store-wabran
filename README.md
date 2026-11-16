# IltizamSMART — نظام الوبران الذكي لإدارة المواد والمستودعات
**Al Wabran Smart Inventory** — شركة الوبران · العقد الموحد للشركة السعودية للكهرباء · فرع نجران

إضافة مستقلة لـ **Odoo 19 Community** باسم `alwabrn_warehouse`. محرك مخزون Odoo الأصلي هو دفتر الأستاذ الوحيد
(`stock.picking / stock.move / stock.quant`) وفوقه واجهة عربية RTL مبسطة. لا يُعدَّل كود Odoo الأساسي ولا تُكتب الكميات بـ SQL.

## ماذا يوجد في المستودع
| المسار | المحتوى |
|---|---|
| `addons/alwabrn_warehouse/` | الإضافة كاملة (نماذج، واجهات، تقارير، تكاملات، 34 اختباراً آلياً) |
| `addons/alwabrn_warehouse/data/materials_2021.xlsx` | **غير مرفوع** (يحتوي أسعار العقد والمستودع عام). ضعه محلياً في هذا المسار قبل التثبيت أو ارفعه من «الإعدادات ← استيراد كتالوج المواد» |
| `deploy/` | Dockerfile + docker-compose للتشغيل على أي جهاز/خادم |
| `scripts/` | نسخ احتياطي واستعادة |
| `docs/` | دليل المستخدم العربي، التثبيت، ويندوز، النسخ الاحتياطي، التقرير الفني |
| `site/` | صفحة تعريفية ثابتة (تنشر على Netlify) |

## التشغيل السريع (Docker)
```bash
cp deploy/.env.example .env      # ثم غيّر القيم
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
# افتح http://localhost:8069  — المستخدم admin / كلمة المرور admin (غيّرها فوراً)
```
أول تشغيل ينشئ قاعدة البيانات ويثبت الإضافة ويفعّل العربية وتوقيت الرياض، ويستورد الكتالوج إن وُجد ملف المواد في `data/` (وإلا يُرفع من الإعدادات).

## تشغيل الاختبارات
```bash
odoo-bin -c odoo.conf -d alwabrn_test -i alwabrn_warehouse --test-tags alwabrn --stop-after-init
# الاختبارات تحتاج كتالوج المواد: ضع الملف في data/ أو عرّف ALWABRN_CATALOG_FILE=/path/materials_2021.xlsx
# النتيجة المسجلة بتاريخ 2026-11-16: 0 failed, 0 error(s) of 34 tests
```

## حالة الميزات (بصدق)
انظر `docs/TECHNICAL_REPORT.md` — لكل ميزة: منفذة / مختبرة / بانتظار بيانات اعتماد خارجية / قيد محدود.

> **مهم:** Odoo تطبيق خادم (Python + PostgreSQL) ولا يمكن تشغيله على Netlify (استضافة ثابتة). Netlify تستضيف الصفحة التعريفية فقط؛
> النظام نفسه يعمل عبر Docker على خادم الشركة أو VPS أو منصة مثل Render.

الترخيص: LGPL-3 (متوافق مع Odoo Community). Odoo علامة تجارية لـ Odoo S.A.

---
Project manager  Eng  Ahmed Zahran
