from odoo import fields, models


class ShipStationInstanceFields(models.Model):
    _inherit = "shipstation.instance"

    cron_sync_orders = fields.Boolean(default=True)
    cron_sync_shipments = fields.Boolean(default=True)
    cron_sync_products = fields.Boolean(default=True)
    cron_sync_customers = fields.Boolean(default=False)
    cron_sync_inventory = fields.Boolean(default=False)
