# -*- coding: utf-8 -*-
import json
import logging

from odoo import fields, models, _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class ShipStationProductSync(models.Model):
    _name = "shipstation.product.sync"
    _description = "ShipStation Product Sync"
    _order = "modify_date desc, id desc"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade", required=True, index=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True, index=True)

    shipstation_product_id = fields.Char(index=True)
    sku = fields.Char(index=True)
    name = fields.Char()
    price = fields.Float()
    weight = fields.Float()
    stock_level = fields.Float()
    modify_date = fields.Datetime()
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _build_payload(self):
        """Build ShipStation Product payload."""
        self.ensure_one()
        source_product = self._get_mapping_source_product_template()
        stock_level = self._get_template_stock_qty(source_product) if source_product else float(self.stock_level or 0.0)
        payload = {
            # ShipStation expects productId on update; omit on create
            "productId": self.shipstation_product_id or None,
            "sku": self.sku or "",
            "name": self.name or "",
            "price": float(self.price or 0.0),
            "weight": float(self.weight or 0.0),
            "stockLevel": float(stock_level or 0.0),
        }
        if not payload["productId"]:
            payload.pop("productId", None)

        # Apply instance field mappings for "product" model.
        # Field mappings are configured against product.template, so prefer a
        # source template resolved by SKU.
        source_record = source_product or self
        payload = self.instance_id._apply_field_mappings(payload, source_record, "product")

        # Keep key types valid for ShipStation product endpoints.
        if "sku" in payload and payload["sku"] is not None:
            payload["sku"] = str(payload["sku"]).strip()
        if "name" in payload and payload["name"] is not None:
            payload["name"] = str(payload["name"]).strip()
        for number_key in ("price", "weight", "stockLevel"):
            if number_key in payload:
                try:
                    payload[number_key] = float(payload[number_key])
                except (TypeError, ValueError):
                    payload.pop(number_key, None)

        return payload

    def _get_template_stock_qty(self, product_template):
        if not product_template or not self.instance_id:
            return 0.0
        variants = product_template.product_variant_ids
        if not variants:
            return 0.0
        location = self.instance_id.inventory_warehouse_id.lot_stock_id if self.instance_id.inventory_warehouse_id else False
        if not location:
            return float(sum(variants.mapped("qty_available")) or 0.0)
        Quant = self.env["stock.quant"].sudo()
        qty = 0.0
        for variant in variants:
            qty += float(Quant._get_available_quantity(variant, location) or 0.0)
        return qty

    def _to_float(self, value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _payload_get(self, payload, key, default=None):
        if not isinstance(payload, dict) or not key:
            return default
        current = payload
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return current

    def _extract_listing_overrides_from_mapping(self, instance, payload):
        """Map configured ShipStation keys back to listing fields on pull/sync."""
        overrides = {}
        if not instance or not isinstance(payload, dict):
            return overrides

        # Mapping is configured on product.template fields.
        field_to_listing = {
            "default_code": "sku",
            "name": "name",
            "list_price": "price",
            "weight": "weight",
            "qty_available": "stock_level",
        }
        for mapping in instance._get_field_mappings("product"):
            odoo_field_name = mapping.odoo_field_id.name
            listing_field = field_to_listing.get(odoo_field_name)
            if not listing_field:
                continue
            key_name = mapping.shipstation_field_key.name
            value = self._payload_get(payload, key_name)
            if value in (None, ""):
                continue
            overrides[listing_field] = value
        return overrides

    def _select_display_value(self, payload_value, override_value, value_type="char"):
        if value_type == "char":
            if payload_value not in (None, ""):
                return str(payload_value).strip()
            if override_value not in (None, ""):
                return str(override_value).strip()
            return False
        if payload_value not in (None, ""):
            return payload_value
        if override_value not in (None, ""):
            return override_value
        return False

    def _resolve_stock_level(self, instance, product_data):
        inventory_model = self.env["shipstation.inventory"]
        stock_level, inventory_payload, issue = inventory_model.resolve_stock_level(instance, product_data)
        return stock_level, inventory_payload, issue

    def _get_mapping_source_product_template(self):
        self.ensure_one()
        if not self.instance_id:
            return self.env["product.template"]

        ProductProduct = self.env["product.product"].with_company(self.instance_id.company_id)
        ProductTemplate = self.env["product.template"].with_company(self.instance_id.company_id)

        sku = (self.sku or "").strip()
        if sku:
            variant = ProductProduct.search([("default_code", "=", sku)], limit=1)
            if variant:
                return variant.product_tmpl_id
            template = ProductTemplate.search([("default_code", "=", sku)], limit=1)
            if template:
                return template

        return ProductTemplate.browse()

    def _json_dump(self, obj):
        try:
            return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return str(obj)

    def _get_existing_shipstation_active(self):
        self.ensure_one()
        payload_data = {}
        try:
            payload_data = json.loads(self.payload or "{}")
        except Exception:
            payload_data = {}
        if isinstance(payload_data, dict):
            product_payload = payload_data.get("product") if isinstance(payload_data.get("product"), dict) else payload_data
            if isinstance(product_payload, dict) and "active" in product_payload:
                return bool(product_payload.get("active"))
        return True

    # -------------------------------------------------------------------------
    # Buttons (called from form view)
    # -------------------------------------------------------------------------
    def action_push_to_shipstation(self):
        """Push current record to ShipStation (create/update)."""
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        payload = self._build_payload()

        # If we already have productId, update; else create
        # ShipStation commonly uses:
        # - POST /products (create)
        # - PUT /products/{productId} (update)
        if self.shipstation_product_id:
            endpoint = f"/products/{self.shipstation_product_id}"
            method = "PUT"
            payload.setdefault("active", self._get_existing_shipstation_active())
        else:
            endpoint = "/products"
            method = "POST"

        _logger.info(
            "ShipStation product push sku=%s method=%s endpoint=%s payload=%s",
            self.sku,
            method,
            endpoint,
            payload,
        )

        response = self.instance_id._ss_request(method, endpoint, data=payload)

        # Capture product id from response (if returned)
        product_id = (
            (response or {}).get("productId")
            or (response or {}).get("product_id")
            or (response or {}).get("id")
        )
        if product_id and not self.shipstation_product_id:
            self.shipstation_product_id = str(product_id)

        self.synced_on = fields.Datetime.now()
        self.payload = self._json_dump({"request": payload, "response": response})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Product Pushed"),
                "message": _("Product sent to ShipStation successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_pull_from_shipstation(self):
        """Pull product details from ShipStation by productId or SKU."""
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        params = {}
        if self.shipstation_product_id:
            params["productId"] = self.shipstation_product_id
        elif self.sku:
            params["sku"] = self.sku

        if not params:
            raise UserError(_("Set a ShipStation product ID or SKU before pulling."))

        data = self.instance_id._fetch_products_with_fallback(
            page=1,
            page_size=1,
            sku=self.sku if not self.shipstation_product_id else None,
            product_id=self.shipstation_product_id or None,
            legacy_params=params,
        )
        products = (data or {}).get("products") or []

        if not products:
            raise UserError(_("No product found in ShipStation for the given ID/SKU."))

        # Update THIS record from payload
        self._write_from_payload(products[0])

        self.synced_on = fields.Datetime.now()
        self.payload = self._json_dump({"request": params, "response": products[0]})

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Product Pulled"),
                "message": _("Product refreshed from ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    # -------------------------------------------------------------------------
    # Upsert / Write from payload
    # -------------------------------------------------------------------------
    def _write_from_payload(self, product_data):
        """Write current record fields using ShipStation product payload."""
        self.ensure_one()
        instance = self.instance_id

        product_id = str(
            product_data.get("productId")
            or product_data.get("product_id")
            or ""
        ).strip()
        overrides = self._extract_listing_overrides_from_mapping(instance, product_data)
        stock_val, inventory_payload, issue = self._resolve_stock_level(instance, product_data)
        sku_val = self._select_display_value(product_data.get("sku"), overrides.get("sku"))
        name_val = self._select_display_value(product_data.get("name"), overrides.get("name"))
        price_val = self._select_display_value(product_data.get("price"), overrides.get("price"), value_type="float")
        weight_val = self._select_display_value(product_data.get("weight"), overrides.get("weight"), value_type="float")
        inventory_model = self.env["shipstation.inventory"]
        _logger.info(
            "ShipStation product write start sku=%s product_id=%s raw_product_candidates=%s raw_inventory_candidates=%s mapped_stock=%s issue=%s record_id=%s",
            product_data.get("sku"),
            product_id,
            inventory_model._extract_stock_candidates(product_data),
            inventory_model._extract_stock_candidates(inventory_payload),
            stock_val,
            issue or "none",
            self.id,
        )
        if stock_val is None:
            stock_val = self.stock_level

        vals = {
            "shipstation_product_id": product_id or self.shipstation_product_id,
            "sku": str(sku_val).strip() if sku_val not in (None, "") else self.sku,
            "name": str(name_val).strip() if name_val not in (None, "") else self.name,
            "price": self._to_float(price_val, 0.0),
            "weight": self._to_float(weight_val, 0.0),
            "stock_level": self._to_float(stock_val, 0.0),
            "modify_date": instance._parse_ss_datetime(product_data.get("modifyDate")),
            "payload": self._json_dump({"product": product_data, "inventory": inventory_payload} if inventory_payload else product_data),
        }
        if issue in ("invalid_credentials", "unavailable"):
            vals["payload"] = self._json_dump(
                {
                    "product": product_data,
                    "inventory_warning": "ShipStation inventory endpoint did not return stock with the configured API credentials.",
                    "stock_preserved": True,
                }
            )
        self.write(vals)
        _logger.info(
            "ShipStation product write done sku=%s record_id=%s stored_stock=%s",
            self.sku,
            self.id,
            self.stock_level,
        )

    def _upsert_from_payload(self, instance, product_data):
        """Create or update a shipstation.product.sync record from payload."""
        product_id = str(
            product_data.get("productId")
            or product_data.get("product_id")
            or ""
        ).strip()
        overrides = self._extract_listing_overrides_from_mapping(instance, product_data)
        stock_val, inventory_payload, issue = self._resolve_stock_level(instance, product_data)
        sku_val = self._select_display_value(product_data.get("sku"), overrides.get("sku"))
        name_val = self._select_display_value(product_data.get("name"), overrides.get("name"))
        price_val = self._select_display_value(product_data.get("price"), overrides.get("price"), value_type="float")
        weight_val = self._select_display_value(product_data.get("weight"), overrides.get("weight"), value_type="float")
        inventory_model = self.env["shipstation.inventory"]

        vals = {
            "instance_id": instance.id,
            "shipstation_product_id": product_id or False,
            "sku": str(sku_val).strip() if sku_val not in (None, "") else False,
            "name": str(name_val).strip() if name_val not in (None, "") else False,
            "price": self._to_float(price_val, 0.0),
            "weight": self._to_float(weight_val, 0.0),
            "stock_level": self._to_float(stock_val, 0.0) if stock_val is not None else 0.0,
            "modify_date": instance._parse_ss_datetime(product_data.get("modifyDate")),
            "synced_on": fields.Datetime.now(),
            "payload": self._json_dump({"product": product_data, "inventory": inventory_payload} if inventory_payload else product_data),
        }

        existing = False
        if product_id:
            existing = self.search(
                [("shipstation_product_id", "=", product_id), ("instance_id", "=", instance.id)],
                limit=1,
            )

        if not existing and product_data.get("sku"):
            existing = self.search(
                [("sku", "=", product_data.get("sku")), ("instance_id", "=", instance.id)],
                limit=1,
            )
        _logger.info(
            "ShipStation product upsert start sku=%s product_id=%s raw_product_candidates=%s raw_inventory_candidates=%s mapped_stock=%s issue=%s existing_id=%s",
            product_data.get("sku"),
            product_id,
            inventory_model._extract_stock_candidates(product_data),
            inventory_model._extract_stock_candidates(inventory_payload),
            stock_val,
            issue or "none",
            existing.id if existing else False,
        )
        if existing and stock_val is None:
            vals["stock_level"] = existing.stock_level
        if issue in ("invalid_credentials", "unavailable"):
            vals["payload"] = self._json_dump(
                {
                    "product": product_data,
                    "inventory_warning": "ShipStation inventory endpoint did not return stock with the configured API credentials.",
                    "stock_preserved": bool(existing),
                }
            )

        if existing:
            existing.write(vals)
            _logger.info(
                "ShipStation product upsert write sku=%s record_id=%s stored_stock=%s",
                existing.sku,
                existing.id,
                existing.stock_level,
            )
            return existing

        created = self.create(vals)
        _logger.info(
            "ShipStation product upsert create sku=%s record_id=%s stored_stock=%s",
            created.sku,
            created.id,
            created.stock_level,
        )
        return created
