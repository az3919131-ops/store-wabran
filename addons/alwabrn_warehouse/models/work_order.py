import json
import logging
import os

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..tools.wo_excel import STAGE_SEL

_logger = logging.getLogger(__name__)
ISSUE_TYPES = ("issue_site", "issue")
RETURN_TYPES = ("return_in", "return_legacy")


class AlwabrnWorkOrder(models.Model):
    """أمر عمل — يُستورد من ملف «أوامر عمل المشاريع»، ويُنشأ آلياً من «محاضر الاستلام»، أو يدوياً.
    الصرف والإرجاع حركات مخزون حقيقية على موقع عهدة خاص بأمر العمل."""
    _name = "alwabrn.work.order"
    _description = "أمر عمل"
    _inherit = ["mail.thread"]
    _order = "name desc"
    _rec_names_search = ["name", "contractor", "location"]

    name = fields.Char("رقم أمر العمل", required=True, index=True, tracking=True)
    stage = fields.Selection(STAGE_SEL, "المرحلة", tracking=True, index=True, default="manual")
    source = fields.Selection([("excel", "ملف أوامر العمل"), ("minutes", "منصة المحاضر"), ("manual", "يدوي")],
                              "المصدر", default="manual", required=True, readonly=True)
    type_summary = fields.Char("أنواع أمر العمل", compute="_compute_summary", store=True)
    status_note = fields.Char("الموقف", compute="_compute_summary", store=True)
    responsible = fields.Char("المسؤول", tracking=True)
    contractor = fields.Char("المقاول", tracking=True)
    consultant = fields.Char("الاستشاري")
    location = fields.Char("الموقع / الحي", tracking=True)
    labor_cost = fields.Float("تكلفة العمالة", compute="_compute_summary", store=True)
    note = fields.Text("ملاحظات")
    active = fields.Boolean(default=True)
    item_ids = fields.One2many("alwabrn.work.order.item", "work_order_id", "بنود أمر العمل (من الملف)")
    material_ids = fields.One2many("alwabrn.work.order.material", "work_order_id", "المواد")
    minutes_ids = fields.One2many("alwabrn.minutes.record", "work_order_id", "محاضر الاستلام")
    transaction_ids = fields.One2many("alwabrn.transaction", "work_order_id", "حركات الصرف والإرجاع")
    site_id = fields.Many2one("alwabrn.site", "موقع العهدة", readonly=True, copy=False)
    minutes_count = fields.Integer("محاضر", compute="_compute_counts")
    tx_count = fields.Integer("حركات", compute="_compute_counts")
    last_minutes_sync = fields.Datetime("آخر تحديث من المحاضر", readonly=True)
    company_id = fields.Many2one("res.company", default=lambda s: s.env.company, required=True)

    _name_uniq = models.Constraint("unique(name, company_id)", "رقم أمر العمل موجود مسبقاً.")

    @api.depends("item_ids.type_code", "item_ids.type_name", "item_ids.status_note", "item_ids.labor_cost")
    def _compute_summary(self):
        for wo in self:
            items = wo.item_ids
            wo.type_summary = " · ".join(dict.fromkeys(
                "%s %s" % (i.type_code or "", i.type_name or "") for i in items if i.type_code or i.type_name)).strip()[:250]
            wo.status_note = " | ".join(dict.fromkeys(i.status_note for i in items if i.status_note))[:250]
            wo.labor_cost = sum(items.mapped("labor_cost"))

    def _compute_counts(self):
        for wo in self:
            wo.minutes_count = len(wo.minutes_ids)
            wo.tx_count = len(wo.transaction_ids)

    @api.constrains("name")
    def _check_name(self):
        import re
        for wo in self:
            if not re.fullmatch(r"\d{6,14}", wo.name or ""):
                raise UserError("رقم أمر العمل يجب أن يكون أرقاماً فقط (6 إلى 14 رقماً).")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name"):
                vals["name"] = "".join(str(vals["name"]).split())
        return super().create(vals_list)

    # ------------------------------------------------------------ custody
    def _ensure_site(self):
        self.ensure_one()
        if not self.site_id:
            Site = self.env["alwabrn.site"].sudo()
            site = Site.search([("code", "=", self.name)], limit=1) or Site.create({
                "name": "أمر عمل %s" % self.name, "code": self.name, "work_order": self.name})
            self.sudo().site_id = site
        return self.site_id

    def _open_tx(self, tx_type):
        self.ensure_one()
        site = self._ensure_site()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.transaction", "view_mode": "form",
                "name": "%s — أمر عمل %s" % ("صرف مواد" if tx_type == "issue_site" else "إرجاع مواد", self.name),
                "context": {"default_tx_type": tx_type, "default_site_id": site.id, "default_work_order_id": self.id,
                            "default_work_order": self.name, "default_document_ref": self.name}}

    def action_issue(self):
        return self._open_tx("issue_site")

    def action_return(self):
        return self._open_tx("return_in")

    def action_return_legacy(self):
        return self._open_tx("return_legacy")

    def action_view_transactions(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.transaction", "view_mode": "list,form",
                "name": "حركات أمر العمل %s" % self.name, "domain": [("work_order_id", "=", self.id)],
                "context": {"default_work_order_id": self.id}}

    def action_view_minutes(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.minutes.record", "view_mode": "list,form",
                "name": "محاضر أمر العمل %s" % self.name, "domain": [("work_order_id", "=", self.id)]}

    # ------------------------------------------------------------ materials
    def _recompute_minutes_materials(self):
        """Sum the material lines of every (non-deleted) minute of the work order."""
        Product = self.env["product.template"]
        Mat = self.env["alwabrn.work.order.material"].sudo()
        for wo in self:
            agg = {}
            for rec in wo.minutes_ids:
                for m in rec.materials_data or []:
                    sec = str(m.get("sec") or "").strip()
                    if not sec:
                        continue
                    a = agg.setdefault(sec, {"name": m.get("name") or "", "unit": m.get("unit") or "", "estimated": 0.0,
                                             "issued": 0.0, "returned": 0.0, "erected": 0.0, "remaining": 0.0, "refs": []})
                    for k in ("estimated", "issued", "returned", "erected", "remaining"):
                        a[k] += float(m.get(k) or 0.0)
                    a["refs"].append(rec.request_no or rec.mahdar_id)
            existing = {l.sec_code: l for l in wo.material_ids}
            for sec, a in agg.items():
                vals = {"sec_estimated": a["estimated"], "sec_issued": a["issued"], "sec_returned": a["returned"],
                        "sec_erected": a["erected"], "sec_remaining": a["remaining"], "from_minutes": True,
                        "minutes_refs": ", ".join(dict.fromkeys(a["refs"]))[:250], "unit_text": a["unit"]}
                if sec in existing:
                    existing[sec].write(vals)
                else:
                    product = Product.alw_find_by_sec(sec)
                    Mat.create(dict(vals, work_order_id=wo.id, sec_code=sec, product_id=product.id or False,
                                    name=(product.name if product else a["name"]) or sec))
            for sec, line in existing.items():
                if sec not in agg and line.from_minutes:
                    line.write({"sec_estimated": 0, "sec_issued": 0, "sec_returned": 0, "sec_erected": 0,
                                "sec_remaining": 0, "from_minutes": False, "minutes_refs": False})
            wo.last_minutes_sync = fields.Datetime.now()

    # ------------------------------------------------------------ minutes sync
    @api.model
    def _minutes_conf(self):
        acc = self.env["alwabrn.integration.account"].sudo().search([("kind", "=", "minutes")], limit=1)
        url = (acc.minutes_url if acc else "") or os.environ.get("ALWABRN_MINUTES_URL") or ""
        key = (acc._secrets().get("api_key") if acc else None) or os.environ.get("ALWABRN_MINUTES_KEY")
        return acc, url.rstrip("/"), key

    @api.model
    def sync_minutes(self, raise_errors=False):
        """Pull work orders and material quantities from «محاضر الاستلام» (read-only API)."""
        acc, url, key = self._minutes_conf()
        Account = self.env["alwabrn.integration.account"].sudo()
        if not url or not key:
            msg = "ربط منصة المحاضر غير مهيأ (الرابط أو المفتاح)."
            if raise_errors:
                raise UserError(msg)
            return {"error": msg}
        Rec = self.env["alwabrn.minutes.record"].sudo()
        headers = {"x-integration-key": key, "user-agent": "IltizamSMART-warehouse/1.0"}
        try:
            r = requests.get(url + "/api/integration/minutes", headers=headers, timeout=60)
            r.raise_for_status()
            remote = r.json().get("items") or {}
            known = {x.mahdar_id: x for x in Rec.search([])}
            changed = [i for i, etag in remote.items() if not known.get(i) or known[i].etag != etag]
            gone = [x for i, x in known.items() if i not in remote]
            touched = self.browse()
            for x in gone:
                touched |= x.work_order_id
                x.unlink()
            stats = {"remote": len(remote), "changed": len(changed), "removed": len(gone), "new_work_orders": 0}
            for i in range(0, len(changed), 40):
                ids = changed[i:i + 40]
                rr = requests.post(url + "/api/integration/minutes", headers=headers, json={"ids": ids}, timeout=90)
                rr.raise_for_status()
                docs = rr.json().get("docs") or {}
                for mid in ids:
                    d = docs.get(mid)
                    rec = known.get(mid) or Rec.search([("mahdar_id", "=", mid)], limit=1)
                    if not d or d.get("deleted") or not d.get("wo"):
                        if rec:
                            touched |= rec.work_order_id
                            rec.unlink()
                        continue
                    wo = self.sudo().search([("name", "=", d["wo"])], limit=1)
                    if not wo:
                        try:
                            wo = self.sudo().create({"name": d["wo"], "source": "minutes", "stage": "minutes",
                                                     "contractor": d.get("contractor") or d.get("contractorShort"),
                                                     "consultant": d.get("consultant"), "location": d.get("location")})
                            stats["new_work_orders"] += 1
                        except Exception as e:  # invalid number in a minute: keep going
                            _logger.warning("minutes %s: bad work order %s (%s)", mid, d.get("wo"), e)
                            continue
                    else:
                        upd = {k: v for k, v in (("contractor", d.get("contractor") or d.get("contractorShort")),
                                                 ("location", d.get("location"))) if v and not wo[k]}
                        if upd:
                            wo.write(upd)
                    vals = {"mahdar_id": mid, "etag": remote.get(mid), "work_order_id": wo.id,
                            "request_no": d.get("requestNo"), "status": d.get("status"), "location": d.get("location"),
                            "contractor": d.get("contractor"), "work_date": d.get("workDate"),
                            "remote_updated": d.get("updatedAt"), "materials_data": d.get("materials") or []}
                    if rec:
                        touched |= rec.work_order_id
                        rec.write(vals)
                    else:
                        Rec.create(vals)
                    touched |= wo
            touched.exists()._recompute_minutes_materials()
            Account.log_event("minutes", "info", "مزامنة المحاضر: %s" % json.dumps(stats, ensure_ascii=False), ok=True)
            return stats
        except requests.RequestException as e:
            Account.log_event("minutes", "error", "فشل الاتصال بمنصة المحاضر: %s" % e)
            if raise_errors:
                raise UserError("تعذر الاتصال بمنصة المحاضر: %s" % e)
            return {"error": str(e)}

    def action_sync_minutes(self):
        stats = self.sync_minutes(raise_errors=True)
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": "تمت المزامنة مع منصة المحاضر", "type": "success", "sticky": False,
            "message": "محاضر بالمنصة: %(remote)s · تحدّث: %(changed)s · حُذف: %(removed)s · أوامر عمل جديدة: %(new_work_orders)s" % stats,
            "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    @api.model
    def cron_sync_minutes(self):
        return self.sync_minutes()

    # ------------------------------------------------------------ excel
    @api.model
    def import_items(self, items, source="excel"):
        """Upsert work orders and their rows from the workbook parser. Returns stats."""
        Item = self.env["alwabrn.work.order.item"].sudo()
        stats = {"work_orders_new": 0, "work_orders_updated": 0, "rows_new": 0, "rows_updated": 0}
        cache = {w.name: w for w in self.sudo().with_context(active_test=False).search([("name", "in", list({i["wo"] for i in items}))])}
        for it in items:
            wo = cache.get(it["wo"])
            if not wo:
                wo = self.sudo().create({"name": it["wo"], "source": source, "stage": it["stage"],
                                         "responsible": it.get("responsible") or False,
                                         "consultant": it.get("consultant") or False})
                cache[it["wo"]] = wo
                stats["work_orders_new"] += 1
            else:
                stats["work_orders_updated"] += 1
                if wo.source == "minutes" or wo.stage in ("manual", "minutes"):
                    wo.write({"stage": it["stage"]})
            vals = {k: it.get(k) or False for k in (
                "type_name", "delivery_date", "receipt_date", "completion_date", "extract_date", "labor_cost",
                "completion_amount", "extract_no", "status_note", "responsible", "plan", "location_details",
                "consultant", "license", "site_text", "sheet")}
            vals["stage"] = it["stage"]
            row = Item.search([("work_order_id", "=", wo.id), ("type_code", "=", it["type_code"]),
                               ("stage", "=", it["stage"])], limit=1)
            if row:
                row.write(vals)
                stats["rows_updated"] += 1
            else:
                Item.create(dict(vals, work_order_id=wo.id, type_code=it["type_code"]))
                stats["rows_new"] += 1
        return stats


class AlwabrnWorkOrderItem(models.Model):
    _name = "alwabrn.work.order.item"
    _description = "بند أمر عمل (من ملف أوامر العمل)"
    _order = "work_order_id, type_code"

    work_order_id = fields.Many2one("alwabrn.work.order", required=True, ondelete="cascade", index=True)
    type_code = fields.Char("رمز النوع")
    type_name = fields.Char("نوع أمر العمل")
    stage = fields.Selection(STAGE_SEL, "المرحلة")
    sheet = fields.Char("الورقة")
    delivery_date = fields.Date("التسليم للمقاول")
    receipt_date = fields.Date("الاستلام من المقاول")
    completion_date = fields.Date("شهادة الإنجاز")
    completion_amount = fields.Float("مبلغ شهادة الإنجاز")
    extract_no = fields.Char("رقم المستخلص")
    extract_date = fields.Date("تاريخ المستخلص")
    labor_cost = fields.Float("تكلفة العمالة")
    status_note = fields.Char("الموقف")
    responsible = fields.Char("المسؤول")
    plan = fields.Char("الخطة")
    location_details = fields.Char("تفاصيل الموقع")
    consultant = fields.Char("الاستشاري")
    license = fields.Char("الرخصة")
    site_text = fields.Char("الموقع")


class AlwabrnWorkOrderMaterial(models.Model):
    """مواد أمر العمل: كميات الكهرباء من المحاضر مقابل الصرف والإرجاع الفعلي من المستودع."""
    _name = "alwabrn.work.order.material"
    _description = "مادة أمر عمل"
    _order = "work_order_id, sec_code"

    work_order_id = fields.Many2one("alwabrn.work.order", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", "المادة", index=True)
    sec_code = fields.Char("كود SEC", required=True, index=True)
    name = fields.Char("الوصف")
    unit_text = fields.Char("الوحدة بالمحضر")
    uom_id = fields.Many2one(related="product_id.uom_id", string="وحدة المستودع")
    from_minutes = fields.Boolean("من المحاضر", readonly=True)
    minutes_refs = fields.Char("المحاضر", readonly=True)
    sec_estimated = fields.Float("التقديرية (محضر)", digits="Product Unit", readonly=True)
    sec_issued = fields.Float("المصروف ISSUE (محضر)", digits="Product Unit", readonly=True)
    sec_returned = fields.Float("المرتجع (محضر)", digits="Product Unit", readonly=True)
    sec_erected = fields.Float("المنفذ (محضر)", digits="Product Unit", readonly=True)
    sec_remaining = fields.Float("المتبقي (محضر)", digits="Product Unit", readonly=True)
    wh_issued = fields.Float("صرف المستودع", digits="Product Unit", compute="_compute_wh")
    wh_returned = fields.Float("إرجاع للمستودع", digits="Product Unit", compute="_compute_wh")
    wh_net = fields.Float("صافي المصروف", digits="Product Unit", compute="_compute_wh")
    diff_issue = fields.Float("فرق الصرف (مستودع − محضر)", digits="Product Unit", compute="_compute_wh")

    _uniq = models.Constraint("unique(work_order_id, sec_code)", "المادة مكررة في أمر العمل.")

    @api.depends("work_order_id.transaction_ids.state", "product_id")
    def _compute_wh(self):
        Line = self.env["alwabrn.transaction.line"].sudo()
        for m in self:
            iss = ret = 0.0
            if m.product_id and m.work_order_id.id:
                for l in Line.search([("transaction_id.work_order_id", "=", m.work_order_id.id),
                                      ("product_id", "=", m.product_id.id), ("state", "=", "posted")]):
                    if l.tx_type in ISSUE_TYPES:
                        iss += l.qty_product_uom
                    elif l.tx_type in RETURN_TYPES:
                        ret += l.qty_product_uom
            m.wh_issued, m.wh_returned, m.wh_net = iss, ret, iss - ret
            m.diff_issue = iss - m.sec_issued


class AlwabrnMinutesRecord(models.Model):
    """نسخة مرجعية (قراءة فقط) من محضر في منصة «محاضر الاستلام»."""
    _name = "alwabrn.minutes.record"
    _description = "محضر استلام (من منصة المحاضر)"
    _order = "remote_updated desc"

    mahdar_id = fields.Char("معرّف المحضر", required=True, index=True, readonly=True)
    etag = fields.Char(readonly=True)
    work_order_id = fields.Many2one("alwabrn.work.order", "أمر العمل", ondelete="cascade", index=True, readonly=True)
    request_no = fields.Char("رقم الطلب بالمحضر", readonly=True)
    status = fields.Char("حالة المحضر", readonly=True)
    location = fields.Char("الموقع", readonly=True)
    contractor = fields.Char("المقاول", readonly=True)
    work_date = fields.Char("تاريخ العمل", readonly=True)
    remote_updated = fields.Char("آخر تعديل بالمنصة", readonly=True)
    materials_data = fields.Json("المواد", readonly=True)
    material_count = fields.Integer("عدد المواد", compute="_compute_count")

    _mahdar_uniq = models.Constraint("unique(mahdar_id)", "المحضر مسجل مسبقاً.")

    def _compute_count(self):
        for r in self:
            r.material_count = len(r.materials_data or [])


class AlwabrnTransactionWO(models.Model):
    _inherit = "alwabrn.transaction"

    work_order_id = fields.Many2one("alwabrn.work.order", "أمر العمل", index=True, tracking=True)

    @api.onchange("work_order_id")
    def _onchange_work_order_id(self):
        if self.work_order_id:
            self.work_order = self.work_order_id.name
            if self.tx_type in ("issue_site", "return_in", "consume") and not self.site_id:
                self.site_id = self.work_order_id._ensure_site()

    def _check_ready(self):
        res = super()._check_ready()
        for rec in self.filtered(lambda r: r.tx_type == "return_legacy" and not r.work_order_id):
            raise UserError("حدد أمر العمل لإرجاع المواد المصروفة قبل تشغيل النظام.")
        return res


class AlwabrnIntegrationAccountMinutes(models.Model):
    _inherit = "alwabrn.integration.account"

    kind = fields.Selection(selection_add=[("minutes", "منصة محاضر الاستلام (التزام)")], ondelete={"minutes": "cascade"})
    minutes_url = fields.Char("رابط منصة المحاضر", default="https://iltizam-najran.netlify.app")
