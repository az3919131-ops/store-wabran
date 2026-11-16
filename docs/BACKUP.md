# النسخ الاحتياطي والاستعادة

البيانات في مكانين ويجب نسخهما معاً:
1. **قاعدة PostgreSQL** (المواد، الحركات، الأرصدة، الكشوف، سجل التدقيق).
2. **مخزن الملفات filestore** (صور الدفاتر وملفات Excel/PDF المصدر والتقارير).
3. احفظ قيمة `ALWABRN_SECRET_KEY` في مكان آمن منفصل (بدونها لا تُفك رموز واتساب/AI المحفوظة).

## نسخ احتياطي
```bash
./scripts/backup.sh /path/to/backups      # dump + filestore، ويحذف الأقدم من 30 يوماً
```
جدولة يومية (Linux): `0 23 * * * cd /opt/alwabrn-smart-inventory && ./scripts/backup.sh /backups`
ويندوز: Task Scheduler ينفذ:
```powershell
docker compose -f deploy/docker-compose.yml exec -T db pg_dump -U odoo -Fc alwabrn > D:\backups\alwabrn_%DATE%.dump
```
انسخ مجلد النسخ إلى وسيط خارجي/سحابي.

## استعادة
```bash
./scripts/restore.sh backups/alwabrn_20261116_2300.dump backups/filestore_20261116_2300.tgz
```
## التحقق
تم اختبار دورة `pg_dump -Fc` ← `pg_restore` على قاعدة التطوير بتاريخ 2026-11-16: القاعدة المستعادة احتوت 624 مادة كاملة.
اختبر الاستعادة شهرياً على قاعدة تجريبية — النسخة التي لم تُجرَّب استعادتها ليست نسخة.

## التصحيح دون حذف
الحركات المرحّلة لا تُحذف: استخدم «عكس الحركة» أو «طلب تسوية» من المطابقة. سجل التدقيق غير قابل للتعديل.

Project manager  Eng  Ahmed Zahran
