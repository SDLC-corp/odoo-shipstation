"""Rule-based (keyword) assistant for the ShipStation menu.

Security model: every answer is computed with the *calling user's* environment. There is no
``sudo()`` anywhere, so the same ACLs and multi-company record rules that protect the list
views protect the assistant: it can never tell a user something they could not open in Odoo.
"""
from datetime import timedelta

from odoo import _, fields
from odoo.exceptions import AccessError

GROUP_READER = "shipstation_connector.group_shipstation_user_readonly"
GROUP_MANAGER = "shipstation_connector.group_shipstation_manager"
MAX_MESSAGE_LENGTH = 500


class ShipStationSimpleChatbotService:
    FALLBACK_REPLY = (
        "I'm here to help with ShipStation connector information such as shipments, "
        "orders, tracking gaps, inventory, and sync status."
    )

    def __init__(self, env):
        self.env = env

    def _require_access(self):
        if not self.env.su and not self.env.user.has_group(GROUP_READER):
            raise AccessError(_("ShipStation access is required to use the assistant."))

    def _tokenize(self, text):
        sanitized = text
        for char in "?.,!:/-_":
            sanitized = sanitized.replace(char, " ")
        return set(part for part in sanitized.split() if part)

    def _contains_any(self, text, phrases):
        return any(phrase in text for phrase in phrases)

    def _contains_any_token(self, tokens, candidates):
        return any(candidate in tokens for candidate in candidates)

    def detect_intent(self, message):
        text = (message if isinstance(message, str) else "").strip().lower()[:MAX_MESSAGE_LENGTH]
        if not text:
            return {"intent": "fallback"}
        tokens = self._tokenize(text)
        scores = {
            "today_shipments": 0,
            "recent_shipments": 0,
            "pending_tracking": 0,
            "recent_orders": 0,
            "low_stock": 0,
            "sync_status": 0,
            "help": 0,
            "fallback": 0,
        }
        if self._contains_any(text, ["help", "what can you do", "available options", "commands"]):
            scores["help"] += 10
        if self._contains_any_token(tokens, ["shipment", "shipments", "tracking", "carrier"]):
            scores["recent_shipments"] += 4
            scores["pending_tracking"] += 3
            scores["today_shipments"] += 2
        if self._contains_any_token(tokens, ["today", "todays"]):
            scores["today_shipments"] += 5
        if self._contains_any(text, ["pending tracking", "missing tracking", "tracking gap", "no tracking"]):
            scores["pending_tracking"] += 7
        if self._contains_any_token(tokens, ["order", "orders"]):
            scores["recent_orders"] += 5
        if self._contains_any(text, ["recent", "latest", "last", "newest"]):
            scores["recent_shipments"] += 4
            scores["recent_orders"] += 4
        if self._contains_any_token(tokens, ["stock", "inventory"]):
            scores["low_stock"] += 6
        if self._contains_any_token(tokens, ["sync", "status", "health", "failure", "failed"]):
            scores["sync_status"] += 6
        best = max(scores, key=scores.get)
        return {"intent": best if scores[best] > 0 else "fallback"}

    def get_reply(self, message):
        self._require_access()
        intent = self.detect_intent(message)["intent"]
        handlers = {
            "today_shipments": self._today_shipments_reply,
            "recent_shipments": self._recent_shipments_reply,
            "pending_tracking": self._pending_tracking_reply,
            "recent_orders": self._recent_orders_reply,
            "low_stock": self._low_stock_reply,
            "sync_status": self._sync_status_reply,
            "help": self._help_reply,
            "fallback": lambda: self.FALLBACK_REPLY,
        }
        return {"intent": intent, "reply": handlers[intent]()}

    def _today_shipments_reply(self):
        Shipment = self.env["shipstation.shipment.sync"]
        start = fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        domain = [("ship_date", ">=", start), ("ship_date", "<", start + timedelta(days=1)),
                  ("voided", "=", False)]
        count = Shipment.search_count(domain)
        rows = Shipment.search(domain, order="ship_date desc, id desc", limit=5)
        if not count:
            return "No ShipStation shipments were synced for today."
        refs = ", ".join(row.shipstation_order_number or row.shipstation_order_id or "-" for row in rows)
        return "There are %s shipments for today. Recent shipment orders: %s." % (count, refs)

    def _recent_shipments_reply(self):
        Shipment = self.env["shipstation.shipment.sync"]
        rows = Shipment.search([], order="ship_date desc, synced_on desc, id desc", limit=5)
        if not rows:
            return "No ShipStation shipments were found."
        summary = []
        for row in rows:
            summary.append(
                "%s (%s%s)"
                % (
                    row.shipstation_order_number or row.shipstation_order_id or "unknown order",
                    row.status or "no status",
                    ", tracking %s" % row.tracking_number if row.tracking_number else "",
                )
            )
        return "Recent ShipStation shipments: %s." % "; ".join(summary)

    def _pending_tracking_reply(self):
        Shipment = self.env["shipstation.shipment.sync"]
        domain = [("voided", "=", False), "|", ("tracking_number", "=", False),
                  ("status", "=", "shipped_pending_tracking")]
        count = Shipment.search_count(domain)
        rows = Shipment.search(domain, order="ship_date desc, id desc", limit=5)
        if not count:
            return "There are no shipments waiting for tracking details."
        refs = ", ".join(row.shipstation_order_number or row.shipstation_order_id or "-" for row in rows)
        return "There are %s shipments missing tracking details. Recent ones: %s." % (count, refs)

    def _recent_orders_reply(self):
        Order = self.env["shipstation.order.sync"]
        rows = Order.search([], order="order_date desc, synced_on desc, id desc", limit=5)
        if not rows:
            return "No ShipStation orders were found."
        summary = []
        for row in rows:
            summary.append(
                "%s for %s (%s)"
                % (
                    row.shipstation_order_number or row.shipstation_order_id or row.id,
                    row.customer_name or row.customer_email or "Guest",
                    row.total_amount or 0.0,
                )
            )
        return "Recent ShipStation orders: %s." % "; ".join(summary)

    def _low_stock_reply(self):
        Inventory = self.env["shipstation.inventory"]
        domain = [("stock_level", "<=", 5), ("remote_stock_verified", "=", True)]
        rows = Inventory.search(domain, order="stock_level asc, id desc", limit=5)
        count = Inventory.search_count(domain)
        if not count:
            return "There are no low-stock ShipStation inventory items right now."
        summary = ", ".join("%s (%s)" % ((row.name or row.sku or "item"), row.stock_level or 0) for row in rows)
        return "There are %s low-stock inventory items. Most urgent: %s." % (count, summary)

    def _sync_status_reply(self):
        # Sync logs hold raw request/response payloads, so they are Manager-only data.
        if not self.env.su and not self.env.user.has_group(GROUP_MANAGER):
            return "Sync status is available to ShipStation Managers."
        Log = self.env["shipstation.sync.log"]
        since = fields.Datetime.now() - timedelta(hours=24)
        failed = Log.search_count([("status", "=", "failed"), ("create_date", ">=", since)])
        succeeded = Log.search_count([("status", "=", "success"), ("create_date", ">=", since)])
        total = failed + succeeded
        rate = round(succeeded * 100.0 / total, 2) if total else 100.0
        return (
            "ShipStation sync status: %s successful syncs and %s failed syncs in the last 24 hours. "
            "Overall success rate is %s%%." % (succeeded, failed, rate)
        )

    def _help_reply(self):
        return (
            "You can ask about today's shipments, recent shipments, pending tracking, "
            "recent orders, low stock inventory, or sync status."
        )
