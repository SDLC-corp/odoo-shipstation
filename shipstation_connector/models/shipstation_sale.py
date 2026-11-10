from odoo import fields, models

# Every field below identifies a record *inside ShipStation* (or records what was pushed).
# They are all ``copy=False``: duplicating a sale order or partner, or splitting a delivery into
# a backorder, must never clone a remote identity - that would make two Odoo records claim the
# same ShipStation order, customer or shipment and break reconciliation.

PUSH_STATES = [
    ("draft", "Not Pushed"),
    ("success", "Pushed"),
    ("failed", "Failed"),
]


class SaleOrder(models.Model):
    _inherit = "sale.order"

    shipstation_instance_id = fields.Many2one("shipstation.instance", index=True, ondelete="set null", copy=False)
    shipstation_order_id = fields.Char(index=True, copy=False)
    shipstation_order_number = fields.Char(index=True, copy=False)
    shipstation_store_id = fields.Char(index=True, copy=False)
    shipstation_last_modified_date = fields.Datetime(copy=False)
    shipstation_status = fields.Char(copy=False)
    shipstation_pushed = fields.Boolean(default=False, copy=False)
    shipstation_push_date = fields.Datetime(copy=False)
    shipstation_push_state = fields.Selection(PUSH_STATES, default="draft", copy=False)
    shipstation_push_error = fields.Text(copy=False)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    shipstation_product_id = fields.Char(index=True, copy=False)
    shipstation_pushed = fields.Boolean(default=False, copy=False)
    shipstation_push_date = fields.Datetime(copy=False)
    shipstation_push_state = fields.Selection(PUSH_STATES, default="draft", copy=False)
    shipstation_push_error = fields.Text(copy=False)


class ResPartner(models.Model):
    _inherit = "res.partner"

    shipstation_customer_id = fields.Char(index=True, copy=False)
    shipstation_pushed = fields.Boolean(default=False, copy=False)
    shipstation_push_date = fields.Datetime(copy=False)
    shipstation_push_state = fields.Selection(PUSH_STATES, default="draft", copy=False)
    shipstation_push_error = fields.Text(copy=False)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shipstation_shipment_id = fields.Char(index=True, copy=False)
    shipstation_tracking_number = fields.Char(copy=False)
    shipstation_carrier_code = fields.Char(copy=False)
    shipstation_service_code = fields.Char(copy=False)
    shipstation_ship_date = fields.Datetime(copy=False)
