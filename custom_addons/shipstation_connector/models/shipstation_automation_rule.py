from odoo import fields, models


class ShipStationAutomationRule(models.Model):
    _name = "shipstation.automation.rule"
    _description = "ShipStation Automation Rule"
    _order = "sequence, id"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    min_weight = fields.Float()
    max_weight = fields.Float()
    country_code = fields.Char(help="2-letter country code, e.g. US, IN")

    set_shipping_method = fields.Char(help="Desired shipping method/service name.")
    set_warehouse_id = fields.Many2one("stock.warehouse", string="Set Warehouse")
    set_sales_team_id = fields.Many2one("crm.team", string="Set Sales Team")
