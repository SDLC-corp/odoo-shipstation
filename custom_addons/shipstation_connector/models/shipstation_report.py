from odoo import fields, models


class ShipStationReport(models.Model):
    _name = "shipstation.report"
    _description = "ShipStation Report"
    _order = "create_date desc"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    operation = fields.Char()
    status = fields.Selection(
        [("running", "Running"), ("success", "Success"), ("failed", "Failed")],
        default="running",
    )
    message = fields.Text()
    mode = fields.Selection(
        [("manual", "Manual"), ("cron", "Cron"), ("webhook", "Webhook")],
        default="manual",
    )
    auto = fields.Boolean(default=False)
    reference = fields.Char()
