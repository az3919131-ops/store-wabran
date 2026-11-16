from odoo import api, fields, models


class AlwabrnProject(models.Model):
    _name = "alwabrn.project"
    _description = "مشروع"
    _order = "code, name"

    name = fields.Char("اسم المشروع", required=True)
    code = fields.Char("رمز المشروع", index=True)
    active = fields.Boolean(default=True)
    site_ids = fields.One2many("alwabrn.site", "project_id", "المواقع")
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)


class AlwabrnSite(models.Model):
    _name = "alwabrn.site"
    _description = "موقع مشروع (عهدة)"
    _inherit = ["mail.thread"]
    _order = "code, name"
    _rec_names_search = ["name", "code", "work_order"]

    name = fields.Char("اسم الموقع", required=True, tracking=True)
    code = fields.Char("رقم الموقع", required=True, index=True, tracking=True)
    project_id = fields.Many2one("alwabrn.project", "المشروع", index=True, tracking=True)
    work_order = fields.Char("أمر العمل", index=True, tracking=True)
    supervisor_id = fields.Many2one("res.users", "المشرف", tracking=True, index=True)
    employee_id = fields.Many2one("res.partner", "الموظف المستلم", tracking=True)
    location_id = fields.Many2one("stock.location", "موقع العهدة المخزني", readonly=True, copy=False)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)
    custody_confirmed_on = fields.Datetime("آخر تأكيد عهدة من المشرف", readonly=True, tracking=True)
    custody_confirmed_by = fields.Many2one("res.users", "أكد العهدة", readonly=True)
    custody_line_count = fields.Integer("أصناف بالعهدة", compute="_compute_custody_count")

    _code_uniq = models.Constraint("unique(code, company_id)", "رقم الموقع مستخدم مسبقاً.")

    @api.depends("code", "name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "[%s] %s" % (rec.code, rec.name) if rec.code else rec.name

    def _compute_custody_count(self):
        Quant = self.env["stock.quant"].sudo()
        for rec in self:
            rec.custody_line_count = Quant.search_count(
                [("location_id", "=", rec.location_id.id), ("quantity", ">", 0)]) if rec.location_id else 0

    @api.model_create_multi
    def create(self, vals_list):
        sites = super().create(vals_list)
        root = self.env["alwabrn.stock.service"].role_location("custody_root")
        for site in sites:
            loc = self.env["stock.location"].sudo().create({
                "name": "عهدة %s" % site.display_name, "usage": "internal",
                "location_id": root.id, "alw_role": "custody", "alw_site_id": site.id,
                "company_id": site.company_id.id,
            })
            site.location_id = loc
        return sites

    def action_view_custody(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": "عهدة %s" % self.display_name,
            "res_model": "alwabrn.custody.line", "view_mode": "list,pivot",
            "domain": [("site_id", "=", self.id)],
        }

    def action_confirm_custody(self):
        """Project supervisor confirms that the outstanding custody is correct."""
        for rec in self:
            rec.write({"custody_confirmed_on": fields.Datetime.now(), "custody_confirmed_by": self.env.user.id})
            self.env["alwabrn.approval"].log(rec, "custody_confirm", "تأكيد عهدة الموقع من المشرف")
        return True
