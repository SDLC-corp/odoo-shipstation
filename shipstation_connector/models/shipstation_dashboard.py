import json
from collections import Counter
from datetime import datetime, timedelta

from odoo import api, models, _
from odoo.exceptions import AccessError

from ..services.shipstation_ai_service import ShipStationAIService


class ShipStationDashboard(models.Model):
    _name = "shipstation.dashboard"
    _description = "ShipStation Dashboard"

    def _require_shipstation_dashboard_admin(self):
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can access the current dashboard."))

    def _table_exists(self, table_name):
        self.env.cr.execute("SELECT to_regclass(%s)", (table_name,))
        return bool(self.env.cr.fetchone()[0])

    def _latest_ai_insight(self, range_days=30):
        insight = self.env["shipstation.ai.insight"].sudo().search(
            [
                ("scope", "=", "all"),
                ("instance_id", "=", False),
                ("range_days", "=", int(range_days or 30)),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        if not insight:
            return {
                "summary_text": "",
                "status": "draft",
                "generated_at": False,
                "error_message": "",
                "pending_tracking_shipments": [],
                "low_stock_inventory": [],
                "failing_instances": [],
                "top_carriers": [],
                "shipment_summary": {},
                "actionable_recommendations": [],
            }
        return insight.get_payload()

    def _shipment_window_summary(self, days):
        Shipment = self.env["shipstation.shipment.sync"].sudo()
        cutoff = datetime.utcnow() - timedelta(days=days)
        company_id = self.env.company.id
        shipments = Shipment.search([
            ("company_id", "=", company_id),
            ("ship_date", ">=", cutoff.strftime("%Y-%m-%d %H:%M:%S")),
        ])
        order_refs = list(
            {
                shipment.shipstation_order_id
                for shipment in shipments
                if shipment.shipstation_order_id
            }
        )
        revenue = 0.0
        if order_refs:
            orders = self.env["shipstation.order.sync"].sudo().search(
                [("shipstation_order_id", "in", order_refs), ("company_id", "=", company_id)]
            )
            revenue = sum(orders.mapped("total_amount"))
        currencies = {str(o.currency or "").strip().upper() for o in orders} if order_refs else set()
        return {
            "count": len(shipments),
            "revenue": revenue if len(currencies) == 1 and "" not in currencies else None,
            "currency": next(iter(currencies)) if len(currencies) == 1 else "",
        }

    def _build_ai_metrics(self):
        company_domain = [("company_id", "=", self.env.company.id)]
        Shipment = self.env["shipstation.shipment.sync"].sudo()
        Inventory = self.env["shipstation.inventory"].sudo()

        pending_tracking = Shipment.search(
            [
                "|",
                ("tracking_number", "=", False),
                ("status", "=", "shipped_pending_tracking"),
                *company_domain,
            ],
            order="ship_date desc, id desc",
            limit=10,
        )
        low_stock = Inventory.search(
            [("stock_level", "<=", 5), ("remote_stock_verified", "=", True), *company_domain],
            order="stock_level asc, modify_date desc, id desc",
            limit=10,
        )
        instance_rows = self.get_dashboard_data().get("instances", [])
        failing_instances = [
            {
                "id": inst["id"],
                "name": inst["name"],
                "failed_24h": inst["failed_24h"],
                "success_24h": inst["success_24h"],
                "health": inst["health"],
            }
            for inst in instance_rows
            if inst.get("failed_24h", 0) > 0
        ][:5]

        carrier_counts = Counter()
        for shipment in Shipment.search(
            [("ship_date", ">=", (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")), *company_domain],
            limit=500,
        ):
            carrier = shipment.carrier_code or "Unknown"
            carrier_counts[carrier] += 1

        return {
            "shipments_last_7_days": self._shipment_window_summary(7),
            "shipments_last_30_days": self._shipment_window_summary(30),
            "pending_tracking_count": Shipment.search_count(
                [
                    "|",
                    ("tracking_number", "=", False),
                    ("status", "=", "shipped_pending_tracking"),
                    *company_domain,
                ]
            ),
            "pending_tracking_shipments": [
                {
                    "order_id": row.shipstation_order_id,
                    "order_number": row.shipstation_order_number,
                    "status": row.status,
                    "ship_date": row.ship_date,
                    "tracking_number": row.tracking_number,
                }
                for row in pending_tracking
            ],
            "low_stock_inventory": [
                {
                    "sku": row.sku,
                    "name": row.name,
                    "stock_level": row.stock_level,
                    "instance_name": row.instance_id.name,
                }
                for row in low_stock
            ],
            "failing_instances": failing_instances,
            "top_carriers": [
                {"carrier_code": carrier, "shipments": count}
                for carrier, count in carrier_counts.most_common(5)
            ],
        }

    @api.model
    def generate_ai_insights(self, range=30):
        self._require_shipstation_dashboard_admin()
        range_days = int(range or 30)
        metrics = self._build_ai_metrics()
        context_meta = {
            "range_days": range_days,
            "summary": self.get_dashboard_data().get("summary", {}),
        }
        result = ShipStationAIService(self.env).generate_operational_insights(metrics, context_meta)
        record = self.env["shipstation.ai.insight"].sudo().upsert_latest(
            {
                "name": "AI Insight - ShipStation",
                "scope": "all",
                "instance_id": False,
                "range_days": range_days,
                "company_id": self.env.company.id,
                "summary_text": result["summary_text"],
                "insight_json": json.dumps(result["insight_payload"], default=str),
                "status": result["status"],
                "generated_at": result["generated_at"],
                "error_message": result.get("error_message") or False,
            }
        )
        return record.get_payload()

    @api.model
    def get_dashboard_data(self):
        self._require_shipstation_dashboard_admin()
        cr = self.env.cr
        company_id = self.env.company.id
        instances = []
        summary = {
            "instances": 0,
            "active_instances": 0,
            "orders": 0,
            "shipments": 0,
            "products": 0,
            "customers": 0,
            "inventory_items": 0,
            "revenue": 0.0,
            "revenue_available": True,
            "currency": self.env.company.currency_id.name,
            "failed_sync": 0,
            "failed_sync_24h": 0,
            "success_sync_24h": 0,
            "success_rate_24h": 100.0,
            "last_sync": "",
        }

        if not self._table_exists("shipstation_instance"):
            return {
                "summary": summary,
                "instances": [],
                "recent_failures": [],
                "recent_activity": [],
                "low_stock_items": [],
                "ai_insight": self._latest_ai_insight(30),
            }

        table_orders = self._table_exists("shipstation_order_sync")
        table_inventory = self._table_exists("shipstation_inventory")
        table_logs = self._table_exists("shipstation_sync_log")

        cr.execute(
            """
            SELECT
                si.id,
                si.name,
                rc.name AS company_name,
                si.active,
                si.last_sync
            FROM shipstation_instance si
            LEFT JOIN res_company rc ON rc.id = si.company_id
            WHERE si.company_id = %s
            ORDER BY si.name
            """, (company_id,)
        )
        instance_map = {}
        for inst_id, name, company_name, active, sync_dt in cr.fetchall():
            sync_text = sync_dt.strftime("%Y-%m-%d %H:%M:%S") if sync_dt else ""
            instance_map[inst_id] = {
                "id": inst_id,
                "name": name,
                "company_name": company_name or "",
                "active": bool(active),
                "total_orders": 0,
                "total_shipments": 0,
                "total_products": 0,
                "total_customers": 0,
                "inventory_items": 0,
                "total_revenue": 0.0,
                "revenue_available": True,
                "currency": "",
                "revenue_by_currency": {},
                "failed_24h": 0,
                "success_24h": 0,
                "health": "healthy",
                "last_sync": sync_text,
            }

        def _apply_count(table_name, target_key, count_expr="COUNT(*)"):
            if not self._table_exists(table_name):
                return
            cr.execute(
                f"""
                SELECT instance_id, {count_expr}
                FROM {table_name}
                WHERE instance_id = ANY(%s)
                GROUP BY instance_id
                """, (list(instance_map),)
            )
            for inst_id, count in cr.fetchall():
                if inst_id in instance_map:
                    instance_map[inst_id][target_key] = int(count or 0)

        if table_orders:
            cr.execute(
                """
                SELECT instance_id, COALESCE(NULLIF(UPPER(TRIM(currency)), ''), 'UNKNOWN') AS currency_code,
                       COUNT(*), COALESCE(SUM(total_amount), 0.0)
                FROM shipstation_order_sync
                WHERE instance_id = ANY(%s)
                GROUP BY instance_id, currency_code
                """, (list(instance_map),)
            )
            for inst_id, currency_code, count, revenue in cr.fetchall():
                if inst_id in instance_map:
                    row = instance_map[inst_id]
                    row["total_orders"] += int(count or 0)
                    row["revenue_by_currency"][currency_code] = float(revenue or 0.0)
            for row in instance_map.values():
                currencies = row["revenue_by_currency"]
                row["revenue_available"] = len(currencies) <= 1 and "UNKNOWN" not in currencies
                row["currency"] = next(iter(currencies), "") if row["revenue_available"] else ""
                row["total_revenue"] = sum(currencies.values()) if row["revenue_available"] else None

        _apply_count("shipstation_shipment_sync", "total_shipments")
        _apply_count("shipstation_product_sync", "total_products")
        _apply_count("shipstation_customer_sync", "total_customers")
        _apply_count("shipstation_inventory", "inventory_items")

        recent_failures = []
        recent_activity = []
        if table_logs:
            cr.execute(
                """
                SELECT COALESCE(COUNT(*), 0)
                FROM shipstation_sync_log l
                JOIN shipstation_instance si ON si.id = l.instance_id
                WHERE l.status = 'failed' AND si.company_id = %s
                """, (company_id,)
            )
            summary["failed_sync"] = int(cr.fetchone()[0] or 0)

            cr.execute(
                """
                SELECT
                    instance_id,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_24h,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_24h
                FROM shipstation_sync_log
                WHERE create_date >= (NOW() - INTERVAL '24 hours')
                  AND instance_id = ANY(%s)
                GROUP BY instance_id
                """, (list(instance_map),)
            )
            for inst_id, failed_24h, success_24h in cr.fetchall():
                if inst_id in instance_map:
                    instance_map[inst_id]["failed_24h"] = int(failed_24h or 0)
                    instance_map[inst_id]["success_24h"] = int(success_24h or 0)

            cr.execute(
                """
                SELECT
                    l.create_date,
                    COALESCE(si.name, ''),
                    COALESCE(l.endpoint, ''),
                    COALESCE(l.error_message, '')
                FROM shipstation_sync_log l
                LEFT JOIN shipstation_instance si ON si.id = l.instance_id
                WHERE l.status = 'failed' AND si.company_id = %s
                ORDER BY l.create_date DESC
                LIMIT 8
                """, (company_id,)
            )
            for create_date, instance_name, endpoint, error_message in cr.fetchall():
                recent_failures.append(
                    {
                        "at": create_date.strftime("%Y-%m-%d %H:%M:%S") if create_date else "",
                        "instance_name": instance_name,
                        "endpoint": endpoint or "",
                        "error": (error_message or "")[:200],
                    }
                )

            cr.execute(
                """
                SELECT
                    l.create_date,
                    COALESCE(si.name, ''),
                    COALESCE(l.status, ''),
                    COALESCE(l.method, ''),
                    COALESCE(l.endpoint, ''),
                    COALESCE(l.error_message, '')
                FROM shipstation_sync_log l
                LEFT JOIN shipstation_instance si ON si.id = l.instance_id
                WHERE si.company_id = %s
                ORDER BY l.create_date DESC
                LIMIT 12
                """, (company_id,)
            )
            for create_date, instance_name, status, method, endpoint, error_message in cr.fetchall():
                recent_activity.append(
                    {
                        "at": create_date.strftime("%Y-%m-%d %H:%M:%S") if create_date else "",
                        "instance_name": instance_name,
                        "status": status,
                        "method": method,
                        "endpoint": endpoint,
                        "error": (error_message or "")[:160],
                    }
                )

        low_stock_items = []
        if table_inventory:
            cr.execute(
                """
                SELECT
                    COALESCE(si.name, ''),
                    COALESCE(inv.sku, ''),
                    COALESCE(inv.name, ''),
                    COALESCE(inv.stock_level, 0.0)
                FROM shipstation_inventory inv
                LEFT JOIN shipstation_instance si ON si.id = inv.instance_id
                WHERE COALESCE(inv.stock_level, 0.0) <= 5.0
                  AND inv.remote_stock_verified IS TRUE
                  AND si.company_id = %s
                ORDER BY inv.stock_level ASC, inv.modify_date DESC NULLS LAST, inv.id DESC
                LIMIT 8
                """, (company_id,)
            )
            for instance_name, sku, name, stock_level in cr.fetchall():
                low_stock_items.append(
                    {
                        "instance_name": instance_name,
                        "sku": sku,
                        "name": name,
                        "stock_level": float(stock_level or 0.0),
                    }
                )

        revenue_currencies = {}
        for inst in instance_map.values():
            for code, amount in inst["revenue_by_currency"].items():
                revenue_currencies[code] = revenue_currencies.get(code, 0.0) + amount
            if inst["failed_24h"] >= 5:
                inst["health"] = "critical"
            elif inst["failed_24h"] > 0:
                inst["health"] = "warning"
            else:
                inst["health"] = "healthy"
            instances.append(inst)
            summary["orders"] += inst["total_orders"]
            summary["shipments"] += inst["total_shipments"]
            summary["products"] += inst["total_products"]
            summary["customers"] += inst["total_customers"]
            summary["inventory_items"] += inst["inventory_items"]
            # Never sum monetary amounts of different currencies.
            inst.pop("revenue_by_currency", None)
            summary["instances"] += 1
            if inst["active"]:
                summary["active_instances"] += 1
            if inst["last_sync"] and (not summary["last_sync"] or inst["last_sync"] > summary["last_sync"]):
                summary["last_sync"] = inst["last_sync"]

        if revenue_currencies:
            summary["revenue_available"] = len(revenue_currencies) == 1 and "UNKNOWN" not in revenue_currencies
            summary["currency"] = next(iter(revenue_currencies), "") if summary["revenue_available"] else ""
            summary["revenue"] = sum(revenue_currencies.values()) if summary["revenue_available"] else None
        failed_24h = sum(item.get("failed_24h", 0) for item in instances)
        success_24h = sum(item.get("success_24h", 0) for item in instances)
        total_24h = failed_24h + success_24h
        summary["failed_sync_24h"] = failed_24h
        summary["success_sync_24h"] = success_24h
        summary["success_rate_24h"] = round((success_24h * 100.0 / total_24h), 2) if total_24h else 100.0

        instances.sort(key=lambda x: (x.get("failed_24h", 0), x.get("total_shipments", 0)), reverse=True)

        return {
            "summary": summary,
            "instances": instances,
            "recent_failures": recent_failures,
            "recent_activity": recent_activity,
            "low_stock_items": low_stock_items,
            "ai_insight": self._latest_ai_insight(30),
        }
