import base64

from odoo import api, fields, models
from odoo.exceptions import UserError

from .common import riyadh_today
from .report_engine import REPORTS, FOOTER

import pytz
from datetime import datetime, timedelta


class AlwabrnReportHistory(models.Model):
    _name = "alwabrn.report.history"
    _description = "سجل التقارير المصدرة والمرسلة"
    _order = "id desc"

    name = fields.Char("التقرير", required=True, readonly=True)
    report_code = fields.Selection(REPORTS, "نوع التقرير", readonly=True)
    date_from = fields.Date("من", readonly=True)
    date_to = fields.Date("إلى", readonly=True)
    filters_text = fields.Char("الفلاتر", readonly=True)
    user_id = fields.Many2one("res.users", "المستخدم", readonly=True, default=lambda s: s.env.user)
    action = fields.Selection([("export", "تصدير"), ("email", "إرسال بريد"), ("scheduled", "إرسال مجدول")],
                              "العملية", readonly=True)
    recipients = fields.Char("المستلمون", readonly=True)
    delivery_state = fields.Selection([("na", "—"), ("queued", "بقائمة الإرسال"), ("sent", "أُرسل"),
                                       ("failed", "فشل")], "حالة التسليم", default="na", readonly=True)
    error = fields.Text("تفاصيل الخطأ", readonly=True)
    mail_id = fields.Many2one("mail.mail", "رسالة البريد", readonly=True)
    attachment_ids = fields.Many2many("ir.attachment", string="الملفات", readonly=True)
    schedule_id = fields.Many2one("alwabrn.report.schedule", "الجدولة", readonly=True)
    run_key = fields.Char("مفتاح التشغيل", readonly=True, index=True, copy=False)

    _run_key_uniq = models.UniqueIndex("(run_key) WHERE run_key IS NOT NULL", "تم تنفيذ هذا التشغيل المجدول مسبقاً.")

    def action_refresh_delivery(self):
        for rec in self.filtered("mail_id"):
            st = rec.mail_id.state
            rec.delivery_state = "sent" if st == "sent" else "failed" if st == "exception" else "queued"
            rec.error = rec.mail_id.failure_reason if st == "exception" else False
        for rec in self.filtered(lambda r: not r.mail_id and r.delivery_state == "queued"):
            rec.delivery_state = "sent"  # mail auto-deleted after successful send
        return True

    @api.model
    def build_files(self, code, filters, fmt):
        Engine = self.env["alwabrn.report.engine"]
        report = Engine.get_report(code, filters)
        stamp = "%s_%s" % (code, report["date_to"])
        files = []
        if fmt in ("xlsx", "both"):
            files.append(("%s.xlsx" % stamp, Engine.render_xlsx(report),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        if fmt in ("pdf", "both"):
            files.append(("%s.pdf" % stamp, Engine.render_pdf(report), "application/pdf"))
        return report, files

    @api.model
    def export_and_log(self, code, filters, fmt, action="export", recipients=None, message=None,
                       schedule=None, run_key=None):
        """Generate files, store history, optionally e-mail to explicit recipients."""
        report, files = self.build_files(code, filters, fmt)
        atts = self.env["ir.attachment"].sudo().create([{
            "name": n, "type": "binary", "datas": base64.b64encode(c), "mimetype": m,
            "res_model": self._name} for n, c, m in files])
        hist = self.sudo().create({
            "name": report["title"], "report_code": code, "date_from": report["date_from"],
            "date_to": report["date_to"], "filters_text": report["filters"], "action": action,
            "user_id": self.env.user.id, "attachment_ids": [(6, 0, atts.ids)],
            "schedule_id": schedule.id if schedule else False, "run_key": run_key,
        })
        atts.write({"res_id": hist.id})
        if action in ("email", "scheduled"):
            emails = [e for e in (recipients or []) if e]
            if not emails:
                raise UserError("حدد مستلماً واحداً على الأقل.")
            summary = "".join("<li>%s: <b>%s</b></li>" % (k, v) for k, v in report["summary"][:8])
            body = "<div dir='rtl'><p>%s</p><p><b>%s</b> — %s</p><ul>%s</ul><p>%s</p></div>" % (
                (message or "مرفق التقرير.").replace("\n", "<br/>"), report["title"],
                "%s — %s" % (report["date_from"], report["date_to"]), summary, FOOTER)
            mail = self.env["mail.mail"].sudo().create({
                "subject": "%s — %s" % (report["title"], report["date_to"]),
                "body_html": body, "email_to": ",".join(emails),
                "attachment_ids": [(6, 0, atts.ids)], "auto_delete": False,
            })
            hist.write({"recipients": ", ".join(emails), "mail_id": mail.id, "delivery_state": "queued"})
            self.env["alwabrn.approval"].log(hist, "send", "إرسال تقرير إلى: %s" % ", ".join(emails))
            if not self.env.context.get("alw_no_send"):
                mail.send(raise_exception=False)
                if mail.exists():
                    hist.action_refresh_delivery()
        return hist


class AlwabrnReportSchedule(models.Model):
    _name = "alwabrn.report.schedule"
    _description = "تقرير مجدول"
    _order = "frequency, hour"

    name = fields.Char("الاسم", required=True)
    report_code = fields.Selection(REPORTS, "التقرير", required=True)
    frequency = fields.Selection([("daily", "يومي"), ("weekly", "أسبوعي"), ("monthly", "شهري")],
                                 "التكرار", required=True, default="daily")
    hour = fields.Integer("الساعة (توقيت الرياض 0-23)", default=18, required=True)
    weekday = fields.Selection([("6", "الأحد"), ("0", "الاثنين"), ("1", "الثلاثاء"), ("2", "الأربعاء"),
                                ("3", "الخميس"), ("4", "الجمعة"), ("5", "السبت")], "يوم الأسبوع", default="3")
    monthday = fields.Integer("يوم الشهر (1-28)", default=1)
    file_format = fields.Selection([("xlsx", "Excel"), ("pdf", "PDF"), ("both", "كلاهما")],
                                   "الصيغة", default="xlsx", required=True)
    recipient_ids = fields.Many2many("res.partner", string="المستلمون المصرح لهم")
    site_id = fields.Many2one("alwabrn.site", "فلتر الموقع")
    project_id = fields.Many2one("alwabrn.project", "فلتر المشروع")
    categ_id = fields.Many2one("product.category", "فلتر الفئة")
    active = fields.Boolean("مفعل (باعتماد مدير النظام)", default=False)
    last_run_key = fields.Char("آخر تشغيل", readonly=True)
    last_run_on = fields.Datetime("وقت آخر تشغيل", readonly=True)
    history_ids = fields.One2many("alwabrn.report.history", "schedule_id", "السجل")

    def _due_key(self, now_local):
        """Return the period key if the schedule is due at ``now_local`` else None."""
        self.ensure_one()
        if now_local.hour < self.hour:
            return None
        if self.frequency == "daily":
            return now_local.strftime("D%Y-%m-%d")
        if self.frequency == "weekly":
            return now_local.strftime("W%G-%V") if str(now_local.weekday()) == self.weekday else None
        return now_local.strftime("M%Y-%m") if now_local.day == max(1, min(self.monthday, 28)) else None

    def _period(self, now_local):
        today = now_local.date()
        if self.frequency == "daily":
            return today, today
        if self.frequency == "weekly":
            return today - timedelta(days=6), today
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev

    @api.model
    def cron_run(self, now=None):
        now_local = (now or datetime.now(pytz.utc)).astimezone(pytz.timezone("Asia/Riyadh"))
        History = self.env["alwabrn.report.history"]
        for sch in self.search([("active", "=", True)]):
            key = sch._due_key(now_local)
            if not key or sch.last_run_key == key:
                continue
            run_key = "SCH-%s-%s" % (sch.id, key)
            if History.sudo().search_count([("run_key", "=", run_key)]):
                sch.last_run_key = key  # retry after crash: already delivered
                continue
            emails = [e for e in sch.recipient_ids.mapped("email") if e]
            if not emails:
                continue
            d_from, d_to = sch._period(now_local)
            filters = {"date_from": d_from, "date_to": d_to, "site_id": sch.site_id.id,
                       "project_id": sch.project_id.id, "categ_id": sch.categ_id.id}
            History.export_and_log(sch.report_code, filters, sch.file_format, action="scheduled",
                                   recipients=emails, schedule=sch, run_key=run_key)
            sch.write({"last_run_key": key, "last_run_on": fields.Datetime.now()})
        return True

    @api.model
    def cron_notify(self):
        """Daily WhatsApp summary — only through the approved/connected channel."""
        d = self.env["alwabrn.dashboard"].get_data({})
        k = {x["key"]: x["display"] for x in d["kpis"]}
        text = "ملخص المستودع %s\nوارد اليوم: %s | منصرف اليوم: %s\nفروقات مفتوحة: %s | بانتظار الاعتماد: %s" % (
            riyadh_today(), k.get("in_today"), k.get("out_today"), k.get("discrepancies"), k.get("pending_tx"))
        return self.env["alwabrn.integration.account"].notify(text)
