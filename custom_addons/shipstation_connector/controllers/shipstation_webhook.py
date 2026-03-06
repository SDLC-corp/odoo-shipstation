import json
import logging

from odoo import http
from odoo.http import request


_logger = logging.getLogger(__name__)


class ShipStationWebhookController(http.Controller):
    @http.route("/shipstation/webhook", type="json", auth="public", methods=["POST"], csrf=False)
    def shipstation_webhook(self, **kwargs):
        payload = request.jsonrequest or {}
        instance = request.env["shipstation.instance"].sudo().search(
            [("active", "=", True)], limit=1
        )
        if not instance:
            _logger.warning("ShipStation webhook received but no active instance found.")
            return {"status": "no_instance"}

        request.env["shipstation.report"].sudo().create({
            "instance_id": instance.id,
            "operation": "Webhook",
            "status": "success",
            "message": json.dumps(payload, ensure_ascii=True)[:2000],
            "mode": "webhook",
            "reference": payload.get("orderNumber") or payload.get("shipmentId"),
        })
        return {"status": "ok"}
