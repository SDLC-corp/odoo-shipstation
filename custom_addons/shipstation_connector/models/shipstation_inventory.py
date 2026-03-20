import json
import logging

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


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

    def _extract_stock_level(self, payload):
        value, _source = self._extract_stock_value_and_source(payload)
        return value

    def _payload_get(self, payload, path):
        current = payload
        for part in str(path).split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        return current

    def _extract_stock_candidates(self, payload):
        if not isinstance(payload, dict):
            return {}
        candidates = {}
        for key in (
            "inStock",
            "stockLevel",
            "available",
            "quantity",
            "on_hand",
            "stock_level",
            "inventory.inStock",
            "inventory.available",
            "inventory.quantity",
            "inventory.on_hand",
            "inventoryDetails.inStock",
            "inventoryDetails.available",
            "inventoryDetails.quantity",
            "inventoryDetails.on_hand",
        ):
            value = self._payload_get(payload, key)
            if value not in (None, ""):
                candidates[key] = value
        return candidates

    def _extract_stock_value_and_source(self, payload):
        candidates = self._extract_stock_candidates(payload)
        for key in (
            "inStock",
            "stockLevel",
            "available",
            "quantity",
            "on_hand",
            "stock_level",
            "inventory.inStock",
            "inventory.available",
            "inventory.quantity",
            "inventory.on_hand",
            "inventoryDetails.inStock",
            "inventoryDetails.available",
            "inventoryDetails.quantity",
            "inventoryDetails.on_hand",
        ):
            if key in candidates:
                return max(self._to_float(candidates[key], 0.0), 0.0), key
        return 0.0, False

    def _inventory_api_base_url(self, instance):
        base_url = (instance.base_url or "").strip().rstrip("/")
        if "api.shipstation.com" in base_url:
            return "https://api.shipstation.com"
        return "https://api.shipstation.com"

    def _fetch_v2_product_payload(self, instance, sku):
        sku = str(sku or "").strip()
        api_key = str(instance.api_key or "").strip()
        if not sku or not api_key:
            return {}, "missing_credentials"
        url = "%s/v2/products" % self._inventory_api_base_url(instance)
        params = {"sku": sku, "page_size": 1}
        try:
            response = requests.get(
                url,
                headers={"api-key": api_key},
                params=params,
                timeout=20,
            )
            _logger.info(
                "ShipStation v2 product request instance=%s sku=%s endpoint=%s params=%s status=%s",
                instance.name,
                sku,
                url,
                params,
                response.status_code,
            )
            if response.status_code >= 400:
                _logger.info(
                    "ShipStation v2 product lookup skipped for SKU %s: status %s response=%s",
                    sku,
                    response.status_code,
                    (response.text or "")[:500],
                )
                if response.status_code in (401, 403):
                    return {}, "invalid_credentials"
                return {}, "unavailable"
            data = response.json() if response.text else {}
            rows = (data or {}).get("products") or []
            payload = rows[0] if rows else {}
            if payload:
                _logger.info(
                    "ShipStation v2 product payload sku=%s payload=%s",
                    sku,
                    json.dumps(payload, ensure_ascii=False, default=str)[:1500],
                )
            return payload, False
        except Exception as exc:
            _logger.info("ShipStation v2 product lookup failed for SKU %s: %s", sku, exc)
            return {}, "unavailable"

    def _fetch_v2_inventory_payload(self, instance, sku, product_id=None):
        sku = str(sku or "").strip()
        product_id = str(product_id or "").strip()
        api_key = str(instance.api_key or "").strip()
        if not api_key:
            _logger.info(
                "ShipStation inventory auth missing for instance=%s sku=%s product_id=%s",
                instance.name,
                sku,
                product_id,
            )
            return {}, "missing_credentials"
        params = {"page_size": 1}
        if sku:
            params["sku"] = sku
        elif product_id:
            params["product_id"] = product_id
        url = "%s/v2/inventory" % self._inventory_api_base_url(instance)
        try:
            response = requests.get(
                url,
                headers={"API-Key": api_key},
                params=params,
                timeout=20,
            )
            _logger.info(
                "ShipStation inventory request instance=%s sku=%s product_id=%s endpoint=%s params=%s status=%s",
                instance.name,
                sku,
                product_id,
                url,
                params,
                response.status_code,
            )
            if response.status_code >= 400:
                _logger.info(
                    "ShipStation v2 inventory lookup skipped for SKU %s: status %s response=%s",
                    sku,
                    response.status_code,
                    (response.text or "")[:500],
                )
                if response.status_code in (401, 403):
                    return {}, "invalid_credentials"
                return {}, "unavailable"
            data = response.json() if response.text else {}
            inventory_rows = (data or {}).get("inventory") or []
            payload = inventory_rows[0] if inventory_rows else {}
            if not payload and sku and product_id:
                fallback_params = {"product_id": product_id, "page_size": 1}
                fallback_response = requests.get(
                    url,
                    headers={"API-Key": api_key},
                    params=fallback_params,
                    timeout=20,
                )
                _logger.info(
                    "ShipStation inventory fallback request instance=%s sku=%s product_id=%s endpoint=%s params=%s status=%s",
                    instance.name,
                    sku,
                    product_id,
                    url,
                    fallback_params,
                    fallback_response.status_code,
                )
                if fallback_response.status_code < 400:
                    fallback_data = fallback_response.json() if fallback_response.text else {}
                    fallback_rows = (fallback_data or {}).get("inventory") or []
                    payload = fallback_rows[0] if fallback_rows else {}
            if payload:
                _logger.info(
                    "ShipStation inventory payload sku=%s product_id=%s payload=%s",
                    sku,
                    product_id,
                    json.dumps(payload, ensure_ascii=False, default=str)[:1500],
                )
            return payload, False
        except Exception as exc:
            _logger.info("ShipStation v2 inventory lookup failed for SKU %s: %s", sku, exc)
            return {}, "unavailable"

    def resolve_stock_level(self, instance, product_data):
        product_id = str(product_data.get("productId") or product_data.get("product_id") or "").strip()
        sku = str(product_data.get("sku") or "").strip()

        direct_stock, direct_source = self._extract_stock_value_and_source(product_data)
        if direct_source:
            _logger.info(
                "ShipStation stock resolved from product payload for SKU %s using field %s value=%s candidates=%s",
                sku,
                direct_source,
                direct_stock,
                self._extract_stock_candidates(product_data),
            )
            return direct_stock, {}, False

        v2_product_payload, product_issue = self._fetch_v2_product_payload(instance, sku)
        if v2_product_payload:
            v2_product_stock, v2_product_source = self._extract_stock_value_and_source(v2_product_payload)
            if v2_product_source:
                _logger.info(
                    "ShipStation stock resolved from v2 product payload for SKU %s using field %s value=%s",
                    sku,
                    v2_product_source,
                    v2_product_stock,
                )
                return v2_product_stock, {"product_v2": v2_product_payload}, False

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
        if existing and float(existing.stock_level or 0.0) > 0.0:
            return max(float(existing.stock_level or 0.0), 0.0), {}, False

        inventory_payload, issue = self._fetch_v2_inventory_payload(instance, sku, product_id=product_id)
        if inventory_payload:
            inventory_stock, inventory_source = self._extract_stock_value_and_source(inventory_payload)
            if inventory_source:
                _logger.info(
                    "ShipStation stock resolved from inventory payload for SKU %s using field %s value=%s",
                    sku,
                    inventory_source,
                    inventory_stock,
                )
                return inventory_stock, inventory_payload, False
            _logger.info(
                "ShipStation inventory payload had no recognized stock field for SKU %s product_id=%s payload=%s",
                sku,
                product_id,
                json.dumps(inventory_payload, ensure_ascii=False, default=str)[:1500],
            )
        _logger.info(
            "ShipStation stock unresolved for SKU %s. product_candidates=%s v2_product_candidates=%s inventory_candidates=%s issue=%s product_issue=%s",
            sku,
            self._extract_stock_candidates(product_data),
            self._extract_stock_candidates(v2_product_payload),
            self._extract_stock_candidates(inventory_payload),
            issue or "none",
            product_issue or "none",
        )
        final_issue = issue or product_issue
        if existing and final_issue in ("missing_credentials", "invalid_credentials"):
            return float(existing.stock_level or 0.0), {}, final_issue
        return None, {}, final_issue

    def _upsert_from_payload(self, instance, product_data):
        product_id = str(product_data.get("productId") or product_data.get("product_id") or "").strip()
        sku = str(product_data.get("sku") or "").strip()
        stock_level, inventory_payload, issue = self.resolve_stock_level(instance, product_data)
        payload_to_store = product_data
        if inventory_payload:
            payload_to_store = {
                "product": product_data,
                "inventory": inventory_payload,
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
        _logger.info(
            "ShipStation inventory upsert start sku=%s product_id=%s raw_product_candidates=%s raw_inventory_candidates=%s mapped_stock=%s issue=%s existing_id=%s",
            sku,
            product_id,
            self._extract_stock_candidates(product_data),
            self._extract_stock_candidates(inventory_payload),
            stock_level,
            issue or "none",
            existing.id if existing else False,
        )
        if stock_level is None and existing:
            stock_level = float(existing.stock_level or 0.0)
        if stock_level is None:
            stock_level = 0.0
        vals = {
            "instance_id": instance.id,
            "shipstation_product_id": product_id or False,
            "sku": sku or False,
            "name": str(product_data.get("name") or "").strip() or False,
            "stock_level": stock_level,
            "modify_date": instance._parse_ss_datetime(product_data.get("modifyDate")),
            "synced_on": fields.Datetime.now(),
            "payload": json.dumps(payload_to_store, ensure_ascii=False, default=str),
        }
        if issue in ("invalid_credentials", "unavailable"):
            vals["payload"] = json.dumps(
                {
                    "product": product_data,
                    "inventory_warning": "ShipStation inventory endpoint did not return stock with the configured API credentials.",
                    "stock_preserved": bool(existing),
                },
                ensure_ascii=False,
                default=str,
            )
        if existing:
            existing.write(vals)
            _logger.info(
                "ShipStation inventory upsert write sku=%s record_id=%s stored_stock=%s stock_status=%s",
                sku,
                existing.id,
                existing.stock_level,
                existing.stock_status,
            )
            return existing
        created = self.create(vals)
        _logger.info(
            "ShipStation inventory upsert create sku=%s record_id=%s stored_stock=%s stock_status=%s",
            sku,
            created.id,
            created.stock_level,
            created.stock_status,
        )
        return created

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

        data = self.instance_id._fetch_products_with_fallback(
            page=1,
            page_size=1,
            sku=sku if not product_id else None,
            product_id=product_id or None,
            legacy_params=params,
        )
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
