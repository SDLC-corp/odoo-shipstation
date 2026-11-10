"""Durable, bounded and idempotent processing of authenticated V1 webhooks."""
import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from ..services.shipstation_webhook_security import (
    WebhookValidationError,
    validate_v1_notification,
)

_logger = logging.getLogger(__name__)


class ShipStationWebhookInbox(models.Model):
    _name = "shipstation.webhook.inbox"
    _description = "ShipStation Webhook Inbox"
    _order = "create_date desc, id desc"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    fingerprint = fields.Char(required=True, index=True, copy=False)
    event_type = fields.Char(required=True)
    payload_store_id = fields.Char()
    resource_url = fields.Text(required=True)
    state = fields.Selection(
        [("pending", "Pending"), ("done", "Done"), ("failed", "Retry Pending"),
         ("exhausted", "Needs Attention")], default="pending", required=True, index=True)
    attempts = fields.Integer(default=0)
    next_retry_at = fields.Datetime()
    last_error = fields.Char()
    processed_at = fields.Datetime()

    # Odoo 19 table constraints: _sql_constraints is no longer reliable.
    _unique_webhook = models.Constraint(
        "UNIQUE(instance_id, fingerprint)",
        "This ShipStation event has already been received for this instance.",
    )

    def action_retry_now(self):
        """Re-queue failed or exhausted notifications so the next cron run processes them."""
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can retry webhook notifications."))
        self.check_access("write")
        targets = self.filtered(lambda rec: rec.state in ("failed", "exhausted"))
        if not targets:
            raise UserError(_("Only notifications that failed or need attention can be retried."))
        targets.write({"state": "pending", "attempts": 0, "next_retry_at": False, "last_error": False})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Webhook Inbox"),
                       "message": _("%s notification(s) queued for processing.") % len(targets),
                       "type": "success", "sticky": False},
        }

    @api.autovacuum
    def _gc_processed_notifications(self):
        """Processed notifications are only needed for replay protection; keep 30 days."""
        cutoff = fields.Datetime.now() - timedelta(days=30)
        self.search([("state", "=", "done"), ("processed_at", "<", cutoff)], limit=5000).unlink()

    def _process_v1_notification(self):
        self.ensure_one()
        instance = self.instance_id
        if not instance.active or not instance.webhook_token:
            raise WebhookValidationError("Webhook connection is disabled")
        event_type, endpoint, params, _fingerprint = validate_v1_notification(
            {"resource_type": self.event_type, "resource_url": self.resource_url,
             "storeId": self.payload_store_id},
            expected_store_id=instance.store_id,
        )
        if event_type in ("ORDER_NOTIFY", "ITEM_ORDER_NOTIFY"):
            if not (instance.webhook_order_create or instance.webhook_order_update):
                raise WebhookValidationError("Order webhooks are disabled for this instance")
            collection = "orders"
            model = "shipstation.order.sync"
        else:
            if not instance.webhook_shipment_update:
                raise WebhookValidationError("Shipment webhooks are disabled for this instance")
            collection = "shipments"
            model = "shipstation.shipment.sync"
        # Bound pages to avoid one notification consuming an Odoo worker for too long.
        params = {k: v for k, v in params.items() if k.lower() not in ("page", "pagesize", "storeid")}
        if instance.store_id:
            params["storeId"] = str(instance.store_id)
        for page in range(1, 4):
            result = instance._ss_request(
                "GET", endpoint, params={**params, "page": page, "pageSize": 100}
            )
            if not isinstance(result, dict) or not isinstance(result.get(collection), list):
                raise ValueError("ShipStation returned an unexpected collection response")
            items = result[collection]
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Malformed ShipStation collection item")
                # Look-ups are filtered by storeId; an item that names ANOTHER store is skipped, but items
                # that name none (shipments never do) are legitimate and must not be dropped.
                if not instance._remote_store_matches(item):
                    continue
                self.env[model]._upsert_from_payload(instance, item)
            total_pages = result.get("pages")
            if total_pages is not None:
                try:
                    pages = int(total_pages)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid ShipStation pages field") from exc
                if pages <= page:
                    return
                if pages > 3:
                    raise ValueError("Webhook result exceeds 3-page safe limit; run a scoped manual sync")
            elif len(items) < 100:
                return
        raise ValueError("Webhook result exceeded safe page limit")

    def _cron_process_webhooks(self, limit=5):
        """Short cron batch, row-locked to prevent two workers processing one event."""
        limit = max(1, min(int(limit), 20))
        self.env.cr.execute(
            """
            SELECT id FROM shipstation_webhook_inbox
            WHERE state IN ('pending', 'failed')
              AND (next_retry_at IS NULL OR next_retry_at <= NOW())
              AND attempts < 5
            ORDER BY create_date, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """, (limit,)
        )
        ids = [row[0] for row in self.env.cr.fetchall()]
        for record in self.sudo().browse(ids):
            attempt = record.attempts + 1
            try:
                with self.env.cr.savepoint():
                    record._process_v1_notification()
            except Exception as exc:
                # Upserts within the savepoint are rolled back, leaving a
                # retriable inbox item and a sanitized operator-facing error.
                delay = min(2 ** (attempt - 1), 60)
                next_state = "exhausted" if attempt >= 5 else "failed"
                record.write({
                    "state": next_state,
                    "attempts": attempt,
                    "last_error": str(exc)[:300],
                    "next_retry_at": fields.Datetime.now() + timedelta(minutes=delay)
                    if next_state == "failed" else False,
                })
                _logger.warning("ShipStation webhook inbox %s failed (attempt %s)", record.id, attempt)
            else:
                record.write({
                    "state": "done", "attempts": attempt,
                    "last_error": False,
                    "processed_at": fields.Datetime.now(),
                    "next_retry_at": False,
                })
        return len(ids)
