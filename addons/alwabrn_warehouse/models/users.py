"""User & permission management for the IltizamSMART system administrator.

One role per user (selection) mapped onto the module's security groups, a
password field with a minimum policy, supervised sites, archive / restore and
an audit trail. Passwords are never logged or stored in clear text: they go
straight to Odoo's hashed ``password`` field.
"""
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

ROLE_GROUPS = [
    # (key, label, xmlid) — ordered from highest to lowest privilege
    ("admin", "مدير النظام", "alwabrn_warehouse.group_alw_admin"),
    ("manager", "مدير المستودع", "alwabrn_warehouse.group_alw_manager"),
    ("employee", "موظف مستودع", "alwabrn_warehouse.group_alw_employee"),
    ("supervisor", "مشرف مشروع", "alwabrn_warehouse.group_alw_supervisor"),
    ("auditor", "مدقق (قراءة فقط)", "alwabrn_warehouse.group_alw_auditor"),
]
ROLE_SELECTION = [(k, label) for k, label, _x in ROLE_GROUPS] + [("none", "بدون صلاحية على النظام")]
ROLE_LABEL = dict(ROLE_SELECTION)
MIN_PASSWORD_LEN = 8


def check_password_policy(password, login=None):
    """Raise UserError when the password is too weak. Never echoes it back."""
    pw = password or ""
    problems = []
    if len(pw) < MIN_PASSWORD_LEN:
        problems.append("لا تقل عن %s خانات" % MIN_PASSWORD_LEN)
    if not re.search(r"[A-Za-z؀-ۿ]", pw):
        problems.append("تحتوي على حرف واحد على الأقل")
    if not re.search(r"\d", pw):
        problems.append("تحتوي على رقم واحد على الأقل")
    if login and pw.strip().lower() == (login or "").strip().lower():
        problems.append("لا تطابق اسم الدخول")
    if problems:
        raise UserError("كلمة المرور ضعيفة. يجب أن: " + "، ".join(problems) + ".")


class ResUsers(models.Model):
    _inherit = "res.users"

    alw_role = fields.Selection(
        ROLE_SELECTION, "الدور في IltizamSMART", compute="_compute_alw_role", inverse="_inverse_alw_role", search="_search_alw_role",
        groups="alwabrn_warehouse.group_alw_admin",
        help="مدير النظام: كل الصلاحيات وإدارة المستخدمين.\n"
             "مدير المستودع: اعتماد وترحيل الحركات والتسويات والتقارير والاستيراد.\n"
             "موظف مستودع: إدخال الحركات والأرصدة والصور والاستيراد (بدون اعتماد).\n"
             "مشرف مشروع: يرى مواقعه وعهدها وحركاتها فقط.\n"
             "مدقق: قراءة فقط لكل البيانات والتقارير.")
    alw_new_password = fields.Char(
        "كلمة المرور", compute="_compute_alw_new_password", inverse="_inverse_alw_new_password",
        groups="alwabrn_warehouse.group_alw_admin", copy=False,
        help="تُحفظ مشفرة ولا يمكن لأحد قراءتها. 8 خانات على الأقل وتحتوي حرفًا ورقمًا.")
    alw_site_ids = fields.Many2many(
        "alwabrn.site", string="المواقع التي يشرف عليها", compute="_compute_alw_site_ids",
        inverse="_inverse_alw_site_ids", groups="alwabrn_warehouse.group_alw_admin")

    # ------------------------------------------------------------------ guards
    def _alw_check_admin(self):
        if self.env.su:
            return
        if not self.env.user.has_group("alwabrn_warehouse.group_alw_admin"):
            raise AccessError("إدارة المستخدمين والصلاحيات متاحة لمدير النظام فقط.")
        protected = self.env.ref("base.user_root") | self.env.ref("base.public_user", raise_if_not_found=False)
        if self & protected:
            raise UserError("لا يمكن تعديل حسابات النظام الداخلية.")

    def _alw_log(self, note, details=None):
        approval = self.env["alwabrn.approval"]
        for user in self:
            approval.log(user, "user_admin", note=note, details=details)

    # ------------------------------------------------------------------ role
    @api.depends("all_group_ids")
    def _compute_alw_role(self):
        groups = [(k, self.env.ref(x, raise_if_not_found=False)) for k, _l, x in ROLE_GROUPS]
        for user in self:
            role = "none"
            for key, group in groups:
                if group and group in user.sudo().all_group_ids:
                    role = key
                    break
            user.alw_role = role

    def _search_alw_role(self, operator, value):
        if operator not in ("=", "!=", "in", "not in"):
            raise UserError("بحث غير مدعوم على الدور.")
        values = [value] if isinstance(value, str) or value is False else list(value)
        users = self.sudo().with_context(active_test=False).search([("share", "=", False)])
        ids = users.filtered(lambda u: u.alw_role in values).ids
        return [("id", "in" if operator in ("=", "in") else "not in", ids)]

    def _inverse_alw_role(self):
        self._alw_check_admin()
        all_groups = self.env["res.groups"].browse(
            [self.env.ref(x).id for _k, _l, x in ROLE_GROUPS])
        admin_group = self.env.ref("alwabrn_warehouse.group_alw_admin")
        for user in self:
            new_role = user.alw_role or "none"
            if user == self.env.user and not self.env.su and new_role != "admin":
                raise UserError("لا يمكنك سحب صلاحية «مدير النظام» من حسابك الحالي.")
            commands = [fields.Command.unlink(g.id) for g in all_groups]
            if new_role != "none":
                xmlid = dict((k, x) for k, _l, x in ROLE_GROUPS)[new_role]
                commands.append(fields.Command.link(self.env.ref(xmlid).id))
            user.sudo().write({"group_ids": commands})
            user._alw_log("تغيير الدور إلى: %s" % ROLE_LABEL[new_role], {"role": new_role})
        remaining = self.env["res.users"].sudo().search_count(
            [("all_group_ids", "in", admin_group.id), ("share", "=", False), ("active", "=", True),
             ("id", "!=", self.env.ref("base.user_root").id)])
        if not remaining:
            raise UserError("يجب أن يبقى مدير نظام واحد نشط على الأقل.")

    # ------------------------------------------------------------------ password
    def _compute_alw_new_password(self):
        for user in self:
            user.alw_new_password = False

    def _inverse_alw_new_password(self):
        for user in self.filtered("alw_new_password"):
            user.alw_set_password(user.alw_new_password)

    def alw_set_password(self, password):
        """Set a new password after policy checks (admin only)."""
        self._alw_check_admin()
        for user in self:
            check_password_policy(password, user.login)
            user.sudo().with_context(alw_password_admin=True).write({"password": password})
            user._alw_log("تعيين كلمة مرور جديدة بواسطة مدير النظام")
        return True

    # ------------------------------------------------------------------ sites
    def _compute_alw_site_ids(self):
        Site = self.env["alwabrn.site"].sudo()
        for user in self:
            user.alw_site_ids = Site.search([("supervisor_id", "=", user.id)]) if user.id else False

    def _inverse_alw_site_ids(self):
        self._alw_check_admin()
        Site = self.env["alwabrn.site"].sudo()
        for user in self:
            current = Site.search([("supervisor_id", "=", user.id)])
            wanted = user.alw_site_ids.sudo()
            (current - wanted).write({"supervisor_id": False})
            (wanted - current).write({"supervisor_id": user.id})

    # ------------------------------------------------------------------ actions
    def action_alw_set_password(self):
        self._alw_check_admin()
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": "تعيين كلمة مرور جديدة",
            "res_model": "alwabrn.user.password.wizard", "view_mode": "form", "target": "new",
            "context": {"default_user_id": self.id},
        }

    def action_alw_archive(self):
        self._alw_check_admin()
        if self.env.user in self and not self.env.su:
            raise UserError("لا يمكنك إيقاف حسابك الحالي.")
        self.sudo().write({"active": False})
        self._alw_log("إيقاف الحساب")
        admin_group = self.env.ref("alwabrn_warehouse.group_alw_admin")
        if not self.env["res.users"].sudo().search_count(
                [("all_group_ids", "in", admin_group.id), ("share", "=", False),
                 ("id", "!=", self.env.ref("base.user_root").id)]):
            raise UserError("يجب أن يبقى مدير نظام واحد نشط على الأقل.")
        return True

    def action_alw_restore(self):
        self._alw_check_admin()
        self.sudo().write({"active": True})
        self._alw_log("إعادة تفعيل الحساب")
        return True


class AlwabrnUserPasswordWizard(models.TransientModel):
    _name = "alwabrn.user.password.wizard"
    _description = "تعيين كلمة مرور مستخدم"

    user_id = fields.Many2one("res.users", "المستخدم", required=True, readonly=True)
    new_password = fields.Char("كلمة المرور الجديدة")
    confirm_password = fields.Char("تأكيد كلمة المرور")

    def action_apply(self):
        self.ensure_one()
        if not self.new_password:
            raise UserError("اكتب كلمة المرور الجديدة.")
        if self.new_password != self.confirm_password:
            raise UserError("كلمتا المرور غير متطابقتين.")
        self.user_id.alw_set_password(self.new_password)
        # never keep the clear-text value in the transient table
        self.write({"new_password": False, "confirm_password": False})
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": "success", "title": "تم",
                           "message": "تم تعيين كلمة المرور الجديدة للمستخدم %s." % self.user_id.name,
                           "next": {"type": "ir.actions.act_window_close"}}}
