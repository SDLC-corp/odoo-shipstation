import ast

from odoo import fields, models, _
from odoo.exceptions import UserError


class ShipStationSyncLog(models.Model):
    _name = "shipstation.sync.log"
    _description = "ShipStation Sync Log"
    _order = "create_date desc"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    operation = fields.Selection([("push", "Push"), ("pull", "Pull"), ("request", "Request")], default="request", required=True)
    model_name = fields.Char(string="Model")
    status = fields.Selection([("success", "Success"), ("failed", "Failed")], required=True, default="failed")
    method = fields.Char()
    endpoint = fields.Char()
    request_payload = fields.Text()
    response_payload = fields.Text()
    error_message = fields.Text()
    retry_count = fields.Integer(default=0)
    last_retry_at = fields.Datetime()

    def _parse_payload(self, text):
        if not text:
            return None
        try:
            return ast.literal_eval(text)
        except Exception:
            return None

    def action_retry_sync(self):
        self.ensure_one()
        if self.status != "failed":
            raise UserError(_("Only failed sync logs can be retried."))
        if not self.method or not self.endpoint:
            raise UserError(_("Missing method/endpoint for retry."))

        payload = self._parse_payload(self.request_payload)
        params = payload if self.method.upper() == "GET" else None
        data = payload if self.method.upper() != "GET" else None

        response = self.instance_id._ss_request(
            self.method.upper(),
            self.endpoint,
            params=params,
            data=data,
        )
        self.write(
            {
                "status": "success",
                "response_payload": str(response),
                "error_message": False,
                "retry_count": self.retry_count + 1,
                "last_retry_at": fields.Datetime.now(),
            }
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Retry Completed"),
                "message": _("Failed sync retried successfully."),
                "type": "success",
                "sticky": False,
            },
        }
