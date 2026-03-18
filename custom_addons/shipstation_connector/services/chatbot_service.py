from datetime import datetime, timedelta


class ShipStationSimpleChatbotService:
    FALLBACK_REPLY = (
        "I'm here to help with ShipStation connector information such as shipments, "
        "orders, tracking gaps, inventory, and sync status."
    )

    def __init__(self, env):
        self.env = env

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
        text = (message or "").strip().lower()
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
        Shipment = self.env["shipstation.shipment.sync"].sudo()
        today = datetime.utcnow()
        start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        domain = [("ship_date", ">=", start.strftime("%Y-%m-%d %H:%M:%S")), ("ship_date", "<", end.strftime("%Y-%m-%d %H:%M:%S"))]
        count = Shipment.search_count(domain)
        rows = Shipment.search(domain, order="ship_date desc, id desc", limit=5)
        if not count:
            return "No ShipStation shipments were synced for today."
        refs = ", ".join(row.shipstation_order_number or row.shipstation_order_id or "-" for row in rows)
        return "There are %s shipments for today. Recent shipment orders: %s." % (count, refs)

    def _recent_shipments_reply(self):
        Shipment = self.env["shipstation.shipment.sync"].sudo()
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
        Shipment = self.env["shipstation.shipment.sync"].sudo()
        domain = ["|", ("tracking_number", "=", False), ("status", "=", "shipped_pending_tracking")]
        count = Shipment.search_count(domain)
        rows = Shipment.search(domain, order="ship_date desc, id desc", limit=5)
        if not count:
            return "There are no shipments waiting for tracking details."
        refs = ", ".join(row.shipstation_order_number or row.shipstation_order_id or "-" for row in rows)
        return "There are %s shipments missing tracking details. Recent ones: %s." % (count, refs)

    def _recent_orders_reply(self):
        Order = self.env["shipstation.order.sync"].sudo()
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
        Inventory = self.env["shipstation.inventory"].sudo()
        rows = Inventory.search([("stock_level", "<=", 5)], order="stock_level asc, modify_date desc, id desc", limit=5)
        count = Inventory.search_count([("stock_level", "<=", 5)])
        if not count:
            return "There are no low-stock ShipStation inventory items right now."
        summary = ", ".join("%s (%s)" % ((row.name or row.sku or "item"), row.stock_level or 0) for row in rows)
        return "There are %s low-stock inventory items. Most urgent: %s." % (count, summary)

    def _sync_status_reply(self):
        Dashboard = self.env["shipstation.dashboard"].sudo()
        data = Dashboard.get_dashboard_data()
        summary = data.get("summary", {})
        return (
            "ShipStation sync status: %s successful syncs and %s failed syncs in the last 24 hours. "
            "Overall success rate is %s%%."
            % (
                int(summary.get("success_sync_24h", 0)),
                int(summary.get("failed_sync_24h", 0)),
                summary.get("success_rate_24h", 100.0),
            )
        )

    def _help_reply(self):
        return (
            "You can ask about today's shipments, recent shipments, pending tracking, "
            "recent orders, low stock inventory, or sync status."
        )
