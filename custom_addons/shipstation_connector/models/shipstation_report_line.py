from odoo import fields, models


class ShipStationReportLine(models.Model):
    _name = "shipstation.report.line"
    _description = "ShipStation Report Line"
    _order = "create_date desc"

    report_id = fields.Many2one("shipstation.report", ondelete="cascade", required=True)
    record_type = fields.Char()
    name = fields.Char()
    status = fields.Selection(
        [("success", "Success"), ("error", "Error")],
        default="success",
    )
    error_message = fields.Text()
    reference = fields.Char()
