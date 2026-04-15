from odoo import fields, models


class ShipStationRate(models.Model):
    _name = "shipstation.rate"
    _description = "ShipStation Rate"
    _order = "shipment_cost asc"

    order_id = fields.Many2one("shipstation.order.sync", ondelete="cascade", required=True)
    carrier_code = fields.Char()
    service_code = fields.Char()
    package_code = fields.Char()
    confirmation = fields.Char()
    carrier_friendly_name = fields.Char()
    service_friendly_name = fields.Char()
    package_friendly_name = fields.Char()
    shipment_cost = fields.Float()
    other_cost = fields.Float()
    tax_amount = fields.Float()
    delivery_days = fields.Integer()
    estimated_delivery_date = fields.Datetime()
    payload = fields.Text()
