"""Authenticated, fast and bounded ingress for ShipStation V1 notifications.

The callback URL includes an instance-scoped random secret because ShipStation
V1 does not document a request-signature protocol. This route intentionally does
not accept V2 signed webhooks; those need a separate verifier and endpoint.
"""
import hmac
import json
import logging
import re

from psycopg2 import IntegrityError
from odoo import http
from odoo.http import request

from ..services.shipstation_webhook_security import (
    WebhookValidationError,
    validate_v1_notification,
)

_logger = logging.getLogger(__name__)

_TOKEN_IN_PATH = re.compile(r"(/shipstation/webhook/v1/[^/\s]*/)[^\s?#\"]+")


def redact_webhook_token(text):
    """Replace the secret path segment of a V1 callback URL with [redacted]."""
    return _TOKEN_IN_PATH.sub(r"\1[redacted]", text) if isinstance(text, str) else text


class WebhookTokenLogFilter(logging.Filter):
    """The callback URL's last segment is the webhook secret, and Odoo's access log (the
    'werkzeug' logger) writes every request path. Redact it so the secret never reaches log files."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(redact_webhook_token(arg) for arg in record.args)
        record.msg = redact_webhook_token(record.msg)
        return True


def _install_log_filter():
    access_logger = logging.getLogger("werkzeug")
    if not any(isinstance(f, WebhookTokenLogFilter) for f in access_logger.filters):
        access_logger.addFilter(WebhookTokenLogFilter())


_install_log_filter()


class ShipStationWebhookController(http.Controller):
    @http.route("/shipstation/webhook", type="http", auth="public", methods=["POST"], csrf=False,
                save_session=False)
    def deprecated_shipstation_webhook(self, **kwargs):
        # Old anonymous ingress is deliberately disabled, not redirected.
        return request.make_json_response({"error": "not_found"}, status=404)

    @http.route("/shipstation/webhook/v1/<int:instance_id>/<string:token>", type="http",
                auth="public", methods=["POST"], csrf=False, save_session=False)
    def shipstation_webhook_v1(self, instance_id, token, **kwargs):
        # Establish identity before parsing or writing the supplied body.
        instance = request.env["shipstation.instance"].sudo().browse(instance_id).exists()
        expected = instance.webhook_token if instance and instance.active else ""
        # Compare bytes: hmac.compare_digest raises TypeError on non-ASCII str, which an
        # attacker could use to turn a bad token into a server error.
        if not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return request.make_json_response({"error": "not_found"}, status=404)

        content_length = request.httprequest.content_length
        if content_length is not None and content_length > 65536:
            return request.make_json_response({"error": "invalid_body"}, status=400)
        raw = request.httprequest.data or b""
        if not raw or len(raw) > 65536 or request.httprequest.mimetype != "application/json":
            return request.make_json_response({"error": "invalid_body"}, status=400)
        try:
            payload = json.loads(raw.decode("utf-8"))
            event_type, _endpoint, _params, fingerprint = validate_v1_notification(
                payload, expected_store_id=instance.store_id,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, WebhookValidationError):
            return request.make_json_response({"error": "invalid_notification"}, status=422)

        if event_type in ("ORDER_NOTIFY", "ITEM_ORDER_NOTIFY"):
            enabled = instance.webhook_order_create or instance.webhook_order_update
        else:
            enabled = instance.webhook_shipment_update
        if not enabled:
            # Authenticated but deliberately switched off: acknowledge so ShipStation does not
            # keep retrying (and eventually deactivate) the subscription.
            return request.make_json_response({"status": "ignored"}, status=200)

        inbox = request.env["shipstation.webhook.inbox"].sudo()
        domain = [("instance_id", "=", instance.id), ("fingerprint", "=", fingerprint)]
        if inbox.search(domain, limit=1):
            return request.make_json_response({"status": "duplicate"}, status=200)
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
            return request.make_json_response({"status": "duplicate"}, status=200)
        # Acknowledge quickly; the scheduled job fetches the trusted V1 API later.
        return request.make_json_response({"status": "accepted"}, status=202)
