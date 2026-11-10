"""Authenticated, fast and bounded ingress for ShipStation V1 notifications.

The callback URL includes an instance-scoped random secret because ShipStation
V1 does not document a request-signature protocol. This route intentionally does
not accept V2 signed webhooks; those need a separate verifier and endpoint.
"""
import hmac
import json
import logging

from psycopg2 import IntegrityError
from odoo import http
from odoo.http import request
from odoo.tools import date_utils

from ..services.shipstation_webhook_security import (
    WebhookValidationError,
    validate_v1_notification,
)

_logger = logging.getLogger(__name__)


def _json_response(data, status=200):
    """Odoo 15 has no request.make_json_response (16+): same JSON body, header and status."""
    body = json.dumps(data, ensure_ascii=False, default=date_utils.json_default)
    response = request.make_response(body, headers=[("Content-Type", "application/json; charset=utf-8")])
    response.status_code = status
    return response


# Odoo 15 turns every application/json request into a JSON-RPC request and then refuses it
# on type="http" routes (400 before the handler runs); 16+ dispatches on the route type.
# ShipStation posts its webhooks as application/json, so the webhook paths keep a plain HTTP
# request (raw body, the controller's own JSON responses and status codes), as on 16+.
_WEBHOOK_PATH = "/shipstation/webhook"


def _is_webhook_path(path):
    return path == _WEBHOOK_PATH or (path or "").startswith(_WEBHOOK_PATH + "/")


if not getattr(http.Root.get_request, "_shipstation_webhook_http", False):
    _root_get_request = http.Root.get_request

    def _get_request(self, httprequest):
        if _is_webhook_path(httprequest.path):
            return http.HttpRequest(httprequest)
        return _root_get_request(self, httprequest)

    _get_request._shipstation_webhook_http = True
    http.Root.get_request = _get_request


class ShipStationWebhookController(http.Controller):
    @http.route("/shipstation/webhook", type="http", auth="public", methods=["POST"], csrf=False,
                save_session=False)
    def deprecated_shipstation_webhook(self, **kwargs):
        # Old anonymous ingress is deliberately disabled, not redirected.
        return _json_response({"error": "not_found"}, status=404)

    @http.route("/shipstation/webhook/v1/<int:instance_id>/<string:token>", type="http",
                auth="public", methods=["POST"], csrf=False, save_session=False)
    def shipstation_webhook_v1(self, instance_id, token, **kwargs):
        # Establish identity before parsing or writing the supplied body.
        instance = request.env["shipstation.instance"].sudo().browse(instance_id).exists()
        expected = instance.webhook_token if instance and instance.active else ""
        # Compare bytes: hmac.compare_digest raises TypeError on non-ASCII str, which an
        # attacker could use to turn a bad token into a server error.
        if not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return _json_response({"error": "not_found"}, status=404)

        content_length = request.httprequest.content_length
        if content_length is not None and content_length > 65536:
            return _json_response({"error": "invalid_body"}, status=400)
        raw = request.httprequest.data or b""
        if not raw or len(raw) > 65536 or request.httprequest.mimetype != "application/json":
            return _json_response({"error": "invalid_body"}, status=400)
        try:
            payload = json.loads(raw.decode("utf-8"))
            event_type, _endpoint, _params, fingerprint = validate_v1_notification(
                payload, expected_store_id=instance.store_id,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, WebhookValidationError):
            return _json_response({"error": "invalid_notification"}, status=422)

        if event_type in ("ORDER_NOTIFY", "ITEM_ORDER_NOTIFY"):
            enabled = instance.webhook_order_create or instance.webhook_order_update
        else:
            enabled = instance.webhook_shipment_update
        if not enabled:
            # Authenticated but deliberately switched off: acknowledge so ShipStation does not
            # keep retrying (and eventually deactivate) the subscription.
            return _json_response({"status": "ignored"}, status=200)

        inbox = request.env["shipstation.webhook.inbox"].sudo()
        domain = [("instance_id", "=", instance.id), ("fingerprint", "=", fingerprint)]
        if inbox.search(domain, limit=1):
            return _json_response({"status": "duplicate"}, status=200)
        try:
            with request.env.cr.savepoint():
                inbox.create({
                    "instance_id": instance.id,
                    "event_type": event_type,
                    "payload_store_id": str(payload.get("storeId") or "").strip(),
                    "resource_url": payload["resource_url"],
                    "fingerprint": fingerprint,
                })
        except IntegrityError:
            # Concurrent retry raced with the first insert; DB unique index wins.
            return _json_response({"status": "duplicate"}, status=200)
        # Acknowledge quickly; the scheduled job fetches the trusted V1 API later.
        return _json_response({"status": "accepted"}, status=202)
