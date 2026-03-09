from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ShipStationInventory(models.Model):
    _name = "shipstation.inventory"
    _description = "ShipStation Inventory"
    _order = "modify_date desc, id desc"
    _rec_name = "name"

    instance_id = fields.Many2one("shipstation.instance", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)
    shipstation_product_id = fields.Char(index=True)
    sku = fields.Char(index=True)
    name = fields.Char()
    stock_level = fields.Float()
    stock_status = fields.Selection(
        [("in_stock", "In stock"), ("out_of_stock", "Out of stock")],
        compute="_compute_stock_status",
        store=True,
    )
    modify_date = fields.Datetime()
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()

    @api.depends("stock_level")
    def _compute_stock_status(self):
        for rec in self:
            rec.stock_status = "in_stock" if (rec.stock_level or 0.0) > 0 else "out_of_stock"

    def _to_float(self, value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _upsert_from_payload(self, instance, product_data):
        product_id = str(product_data.get("productId") or product_data.get("product_id") or "").strip()
        sku = str(product_data.get("sku") or "").strip()
        vals = {
            "instance_id": instance.id,
            "shipstation_product_id": product_id or False,
            "sku": sku or False,
            "name": str(product_data.get("name") or "").strip() or False,
            "stock_level": max(self._to_float(product_data.get("stockLevel"), 0.0), 0.0),
            "modify_date": instance._parse_ss_datetime(product_data.get("modifyDate")),
            "synced_on": fields.Datetime.now(),
            "payload": str(product_data),
        }
        existing = False
        if product_id:
            existing = self.search(
                [("instance_id", "=", instance.id), ("shipstation_product_id", "=", product_id)],
                limit=1,
            )
        if not existing and sku:
            existing = self.search(
                [("instance_id", "=", instance.id), ("sku", "=", sku)],
                limit=1,
            )
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)

    def action_refresh_inventory(self):
        instances = self.mapped("instance_id")
        if not instances:
            instances = self.env["shipstation.instance"].search([("active", "=", True)])
        if not instances:
            raise UserError(_("No active ShipStation instance found."))
        for instance in instances:
            instance.with_context(suppress_toast=True)._sync_inventory(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ShipStation"),
                "message": _("Inventory refreshed for %s instances.") % len(instances),
                "type": "success",
                "sticky": False,
            },
        }

    def action_update_inventory(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        params = {}
        product_id = str(self.shipstation_product_id or "").strip()
        sku = str(self.sku or "").strip()
        if product_id:
            params["productId"] = int(product_id) if product_id.isdigit() else product_id
        elif sku:
            params["sku"] = sku
        else:
            raise UserError(_("Set ShipStation Product ID or SKU before updating inventory."))
        if self.instance_id.store_id and str(self.instance_id.store_id).isdigit():
            params["storeId"] = int(self.instance_id.store_id)

        data = self.instance_id._ss_request("GET", "/products", params=params)
        products = (data or {}).get("products") or []
        if not products:
            raise UserError(_("No product found in ShipStation for the given Product ID/SKU."))

        updated = self._upsert_from_payload(self.instance_id, products[0])
        if updated and updated.id != self.id:
            self.write(
                {
                    "shipstation_product_id": updated.shipstation_product_id,
                    "sku": updated.sku,
                    "name": updated.name,
                    "stock_level": updated.stock_level,
                    "modify_date": updated.modify_date,
                    "synced_on": fields.Datetime.now(),
                    "payload": updated.payload,
                }
            )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Inventory Updated"),
                "message": _("Inventory updated from ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }
