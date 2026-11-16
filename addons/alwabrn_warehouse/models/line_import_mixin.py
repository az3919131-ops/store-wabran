from odoo import models


class AlwabrnLineImportMixin(models.AbstractModel):
    _name = "alwabrn.line.import.mixin"
    _description = "أزرار استيراد الأسطر (Excel / صورة / كاميرا)"

    def _open_line_import(self, method):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "alwabrn.line.import.wizard", "view_mode": "form",
                "target": "new", "name": {"excel": "استيراد من Excel", "image": "استيراد من صورة / PDF",
                                          "camera": "تصوير بكاميرا الجوال"}[method],
                "context": {"default_res_model": self._name, "default_res_id": self.id, "default_method": method}}

    def action_import_excel(self):
        return self._open_line_import("excel")

    def action_import_image(self):
        return self._open_line_import("image")

    def action_import_camera(self):
        return self._open_line_import("camera")


class AlwabrnTransactionImport(models.Model):
    _name = "alwabrn.transaction"
    _inherit = ["alwabrn.transaction", "alwabrn.line.import.mixin"]


class AlwabrnSnapshotImport(models.Model):
    _name = "alwabrn.daily.snapshot"
    _inherit = ["alwabrn.daily.snapshot", "alwabrn.line.import.mixin"]
