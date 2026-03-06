from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    shipstation_order_id = fields.Char(index=True)
    shipstation_order_number = fields.Char(index=True)
    shipstation_store_id = fields.Char(index=True)
    shipstation_last_modified_date = fields.Datetime()
    shipstation_status = fields.Char()
    shipstation_pushed = fields.Boolean(default=False)
    shipstation_push_date = fields.Datetime()
    shipstation_push_state = fields.Selection(
        [
            ("draft", "Not Pushed"),
            ("success", "Pushed"),
            ("failed", "Failed"),
        ],
        default="draft",
    )
    shipstation_push_error = fields.Text()


class ProductTemplate(models.Model):
    _inherit = "product.template"

    shipstation_product_id = fields.Char(index=True)
    shipstation_pushed = fields.Boolean(default=False)
    shipstation_push_date = fields.Datetime()
    shipstation_push_state = fields.Selection(
        [
            ("draft", "Not Pushed"),
            ("success", "Pushed"),
            ("failed", "Failed"),
        ],
        default="draft",
    )
    shipstation_push_error = fields.Text()


class ResPartner(models.Model):
    _inherit = "res.partner"

    shipstation_customer_id = fields.Char(index=True)
    shipstation_pushed = fields.Boolean(default=False)
    shipstation_push_date = fields.Datetime()
    shipstation_push_state = fields.Selection(
        [
            ("draft", "Not Pushed"),
            ("success", "Pushed"),
            ("failed", "Failed"),
        ],
        default="draft",
    )
    shipstation_push_error = fields.Text()


class StockPicking(models.Model):
    _inherit = "stock.picking"

    shipstation_shipment_id = fields.Char(index=True)
    shipstation_tracking_number = fields.Char()
    shipstation_carrier_code = fields.Char()
    shipstation_service_code = fields.Char()
    shipstation_ship_date = fields.Datetime()
