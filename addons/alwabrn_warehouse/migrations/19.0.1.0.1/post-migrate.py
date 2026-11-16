from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    from odoo.addons.alwabrn_warehouse.hooks import setup_units
    setup_units(api.Environment(cr, SUPERUSER_ID, {}))
