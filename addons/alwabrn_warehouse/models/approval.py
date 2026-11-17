import json

from odoo import api, fields, models
from odoo.exceptions import UserError


class AlwabrnApproval(models.Model):
    """Immutable audit record for every approval / rejection / reversal."""
    _name = "alwabrn.approval"
    _description = "سجل الاعتمادات والتدقيق"
    _order = "id desc"

    name = fields.Char("المرجع", required=True, readonly=True)
    res_model = fields.Char("النموذج", required=True, readonly=True, index=True)
    res_id = fields.Integer("السجل", required=True, readonly=True, index=True)
    action = fields.Selection([
        ("submit", "إرسال للاعتماد"), ("approve", "اعتماد"), ("post", "ترحيل للمخزون"),
        ("reject", "رفض"), ("reverse", "عكس"), ("adjust", "تسوية جردية"),
        ("custody_confirm", "تأكيد عهدة"), ("import", "استيراد"), ("send", "إرسال تقرير"),
        ("config", "تعديل إعدادات"), ("duplicate_override", "تجاوز تحذير تكرار"),
        ("user_admin", "إدارة المستخدمين والصلاحيات"),
    ], string="الإجراء", required=True, readonly=True, index=True)
    user_id = fields.Many2one("res.users", "المستخدم", required=True, readonly=True,
                              default=lambda s: s.env.user)
    date = fields.Datetime("التوقيت", required=True, readonly=True, default=fields.Datetime.now, index=True)
    note = fields.Text("ملاحظات", readonly=True)
    details = fields.Text("تفاصيل (JSON)", readonly=True)

    @api.model
    def log(self, record, action, note=None, details=None):
        return self.sudo().create({
            "name": record.display_name or "%s,%s" % (record._name, record.id),
            "res_model": record._name, "res_id": record.id, "action": action,
            "user_id": self.env.user.id, "note": note,
            "details": json.dumps(details, ensure_ascii=False, default=str) if details else False,
        })

    def write(self, vals):
        raise UserError("سجل التدقيق غير قابل للتعديل.")

    def unlink(self):
        if not self.env.context.get("alw_test_cleanup"):
            raise UserError("سجل التدقيق غير قابل للحذف.")
        return super().unlink()

    def action_open_record(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": self.res_model,
                "res_id": self.res_id, "view_mode": "form"}
