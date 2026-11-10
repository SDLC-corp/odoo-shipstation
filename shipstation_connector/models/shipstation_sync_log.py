import ast

from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


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

    @api.autovacuum
    def _gc_old_requests(self):
        """Request logs carry payloads: keep successes 14 days and failures 90 days."""
        now = fields.Datetime.now()
        self.search([("status", "=", "success"), ("create_date", "<", now - timedelta(days=14))],
                    limit=10000).unlink()
        self.search([("status", "=", "failed"), ("create_date", "<", now - timedelta(days=90))],
                    limit=10000).unlink()

    @api.depends("method", "endpoint", "status")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s %s (%s)" % (rec.method or "", rec.endpoint or _("request"), rec.status or "")

    def _parse_payload(self, text):
        if not text:
            return None
        try:
            return ast.literal_eval(text)
        except Exception:
            return None

    def action_retry_sync(self):
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can retry raw API requests."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.status != "failed":
            raise UserError(_("Only failed sync logs can be retried."))
        if not self.method or not self.endpoint:
            raise UserError(_("Missing method/endpoint for retry."))
        # Replaying a failed write might create a second paid label or order.
        # Only allow explicitly read-only V1 endpoints; business-specific
        # write retries must verify remote idempotency before they can be enabled.
        if self.method.upper() != "GET":
            raise UserError(_("Automatic retry of shipping writes is disabled to prevent duplicate labels or orders."))
        path = self.endpoint.split("?", 1)[0]
        if not (path.startswith("/") and not path.startswith("//") and
                path.split("/", 2)[1] in {"orders", "shipments", "products", "customers", "stores"}):
            raise UserError(_("Automatic retry is restricted to known ShipStation read endpoints."))

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
