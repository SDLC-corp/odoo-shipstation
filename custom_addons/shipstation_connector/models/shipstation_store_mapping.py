from odoo import fields, models


class ShipStationStoreMapping(models.Model):
    _name = "shipstation.store.mapping"
    _description = "ShipStation Store Mapping"
    _order = "instance_id, shipstation_store_id"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    shipstation_store_id = fields.Char(required=True, index=True)
    name = fields.Char()
    warehouse_id = fields.Many2one("stock.warehouse", string="Odoo Warehouse")
    sales_team_id = fields.Many2one("crm.team", string="Sales Team")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        (
            "uniq_store_mapping",
            "unique(instance_id, shipstation_store_id)",
            "Store mapping must be unique per instance.",
        ),
    ]
