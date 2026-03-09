from odoo import api, models


class ShipStationDashboard(models.Model):
    _name = "shipstation.dashboard"
    _description = "ShipStation Dashboard"

    def _table_exists(self, table_name):
        self.env.cr.execute("SELECT to_regclass(%s)", (table_name,))
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def get_dashboard_data(self):
        cr = self.env.cr
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
            }

        table_orders = self._table_exists("shipstation_order_sync")
        table_shipments = self._table_exists("shipstation_shipment_sync")
        table_products = self._table_exists("shipstation_product_sync")
        table_customers = self._table_exists("shipstation_customer_sync")
        table_inventory = self._table_exists("shipstation_inventory")
        table_logs = self._table_exists("shipstation_sync_log")

        # Base instance list
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
            ORDER BY si.name
            """
        )
        instance_map = {}
        for row in cr.fetchall():
            inst_id, name, company_name, active, sync_dt = row
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
                GROUP BY instance_id
                """
            )
            for inst_id, count in cr.fetchall():
                if inst_id in instance_map:
                    instance_map[inst_id][target_key] = int(count or 0)

        if table_orders:
            cr.execute(
                """
                SELECT instance_id, COUNT(*), COALESCE(SUM(total_amount), 0.0)
                FROM shipstation_order_sync
                GROUP BY instance_id
                """
            )
            for inst_id, count, revenue in cr.fetchall():
                if inst_id in instance_map:
                    instance_map[inst_id]["total_orders"] = int(count or 0)
                    instance_map[inst_id]["total_revenue"] = float(revenue or 0.0)

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
                FROM shipstation_sync_log
                WHERE status = 'failed'
                """
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
                GROUP BY instance_id
                """
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
                WHERE l.status = 'failed'
                ORDER BY create_date DESC
                LIMIT 8
                """
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
                ORDER BY l.create_date DESC
                LIMIT 12
                """
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
                WHERE COALESCE(inv.stock_level, 0.0) <= 0.0
                ORDER BY inv.modify_date DESC NULLS LAST, inv.id DESC
                LIMIT 8
                """
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

        for inst in instance_map.values():
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
            summary["revenue"] += inst["total_revenue"]
            summary["instances"] += 1
            if inst["active"]:
                summary["active_instances"] += 1
            if inst["last_sync"] and (not summary["last_sync"] or inst["last_sync"] > summary["last_sync"]):
                summary["last_sync"] = inst["last_sync"]

        failed_24h = sum(x.get("failed_24h", 0) for x in instances)
        success_24h = sum(x.get("success_24h", 0) for x in instances)
        total_24h = failed_24h + success_24h
        summary["failed_sync_24h"] = failed_24h
        summary["success_sync_24h"] = success_24h
        summary["success_rate_24h"] = round((success_24h * 100.0 / total_24h), 2) if total_24h else 100.0

        instances.sort(key=lambda x: (x.get("failed_24h", 0), x.get("total_orders", 0)), reverse=True)

        return {
            "summary": summary,
            "instances": instances,
            "recent_failures": recent_failures,
            "recent_activity": recent_activity,
            "low_stock_items": low_stock_items,
        }
