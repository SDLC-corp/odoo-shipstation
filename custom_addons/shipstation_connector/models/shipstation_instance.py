import ast
import logging

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class ShipStationInstance(models.Model):
    _name = "shipstation.instance"
    _description = "ShipStation Instance"
    _inherit = "shipstation.sync.base"

    name = fields.Char(required=True)
    base_url = fields.Char(default="https://api.shipstation.com")
    api_key = fields.Char(required=True)
    api_secret = fields.Char(required=True)
    store_id = fields.Char()
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    active = fields.Boolean(default=True)

    last_sync = fields.Datetime()
    last_order_sync_at = fields.Datetime()
    last_shipment_sync_at = fields.Datetime()
    last_product_sync_at = fields.Datetime()
    last_customer_sync_at = fields.Datetime()
    order_sync_days_back = fields.Integer(default=7)
    order_status = fields.Selection(
        [
            ("all", "All"),
            ("awaiting_payment", "Awaiting Payment"),
            ("awaiting_shipment", "Awaiting Shipment"),
            ("shipped", "Shipped"),
            ("on_hold", "On Hold"),
            ("cancelled", "Cancelled"),
        ],
        default="awaiting_shipment",
    )

    webhook_order_create = fields.Boolean()
    webhook_order_update = fields.Boolean()
    webhook_shipment_update = fields.Boolean()

    cron_sync_orders = fields.Boolean(default=True)
    cron_sync_shipments = fields.Boolean(default=True)
    cron_sync_products = fields.Boolean(default=True)
    cron_sync_customers = fields.Boolean(default=False)
    cron_sync_inventory = fields.Boolean(default=False)
    inventory_update_odoo_stock = fields.Boolean(default=False)
    inventory_warehouse_id = fields.Many2one("stock.warehouse", string="Inventory Warehouse")

    total_orders = fields.Integer(compute="_compute_totals")
    total_shipments = fields.Integer(compute="_compute_totals")
    total_revenue = fields.Float(compute="_compute_totals")
    store_mapping_ids = fields.One2many("shipstation.store.mapping", "instance_id", string="Store Mappings")
    carrier_mapping_ids = fields.One2many("shipstation.carrier.mapping", "instance_id", string="Carrier Mappings")
    automation_rule_ids = fields.One2many("shipstation.automation.rule", "instance_id", string="Automation Rules")

    def init(self):
        """Schema safety net for environments running updated code before module upgrade."""
        self.env.cr.execute(
            """
            ALTER TABLE shipstation_instance
            ADD COLUMN IF NOT EXISTS cron_sync_inventory boolean
            """
        )
        self.env.cr.execute(
            """
            ALTER TABLE shipstation_instance
            ADD COLUMN IF NOT EXISTS inventory_update_odoo_stock boolean
            """
        )
        self.env.cr.execute(
            """
            ALTER TABLE shipstation_instance
            ADD COLUMN IF NOT EXISTS inventory_warehouse_id integer
            """
        )

    def _extract_store_id_from_response(self, stores_response):
        """Return first valid store ID from /stores response."""
        stores = []
        if isinstance(stores_response, dict):
            stores = stores_response.get("stores") or []
        elif isinstance(stores_response, list):
            stores = stores_response

        for store in stores:
            if not isinstance(store, dict):
                continue
            store_id = store.get("storeId") or store.get("store_id") or store.get("id")
            if store_id is None:
                continue
            return str(store_id).strip()
        return False

    def _auto_set_store_id(self, force=False):
        """Keep any manually configured store_id; do not depend on /stores."""
        for rec in self:
            if not rec.api_key or not rec.api_secret:
                continue
            if rec.store_id and not force:
                continue
            _logger.info(
                "ShipStation auto store fetch skipped for instance %s; keep store_id=%s",
                rec.name,
                rec.store_id or "",
            )

    @api.model_create_multi
    def create(self, vals_list):
        normalized_vals_list = []
        for vals in vals_list:
            vals = dict(vals)
            vals["base_url"] = self._normalize_shipstation_base_url(vals.get("base_url"))
            normalized_vals_list.append(vals)
        records = super().create(normalized_vals_list)
        if not self.env.context.get("skip_store_autofill"):
            records._auto_set_store_id(force=False)
        ShipStationField = self.env["shipstation.field"]
        for rec in records:
            ShipStationField.ensure_default_fields_for_instance(rec)
        return records

    def write(self, vals):
        if "base_url" in vals:
            vals["base_url"] = self._normalize_shipstation_base_url(vals.get("base_url"))
        res = super().write(vals)
        if self.env.context.get("skip_store_autofill"):
            return res

        credentials_changed = any(key in vals for key in ("api_key", "api_secret", "base_url"))
        store_cleared = "store_id" in vals and not vals.get("store_id")
        if credentials_changed or store_cleared:
            self._auto_set_store_id(force=credentials_changed)
        return res

    def _compute_totals(self):
        for rec in self:
            orders = self.env["shipstation.order.sync"].search_count(
                [("instance_id", "=", rec.id)]
            )
            shipments = self.env["shipstation.shipment.sync"].search_count(
                [("instance_id", "=", rec.id)]
            )
            revenue = sum(
                self.env["shipstation.order.sync"]
                .search([("instance_id", "=", rec.id)])
                .mapped("total_amount")
            )
            rec.total_orders = orders
            rec.total_shipments = shipments
            rec.total_revenue = revenue

    def action_test_connection(self):
        updated_store_ids = []
        for rec in self:
            response = requests.get(
                "https://api.shipstation.com/v2/products",
                headers={"api-key": str(rec.api_key or "").strip()},
                params={"page": 1, "page_size": 1},
                timeout=30,
            )
            if response.status_code >= 400:
                raise UserError(
                    _("ShipStation API error %s on /v2/products: %s")
                    % (response.status_code, (response.text or "")[:1000])
                )
            self.env["shipstation.field"].ensure_default_fields_for_instance(rec)
            if rec.store_id:
                updated_store_ids.append(rec.store_id)
        message = _("ShipStation connection successful.")
        if updated_store_ids:
            message = _("ShipStation connection successful. Store ID: %s") % ", ".join(updated_store_ids)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connected"),
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_orders(self):
        self.ensure_one()
        self._sync_orders(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Orders Synced"),
                "message": _("Orders synced successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_shipments(self):
        self.ensure_one()
        self._sync_shipments(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Shipments Synced"),
                "message": _("Shipments synced successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_products(self):
        self.ensure_one()
        self._sync_products(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Products Synced"),
                "message": _("Products synced successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_customers(self):
        self.ensure_one()
        self._sync_customers(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Customers Synced"),
                "message": _("Customers synced successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_categories(self):
        self.ensure_one()
        self._sync_categories(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Categories Synced"),
                "message": _("Categories synced successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_attributes(self):
        self.ensure_one()
        self._sync_attributes(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Attributes Synced"),
                "message": _("Attributes synced successfully."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_push_orders(self):
        self.ensure_one()
        self._push_orders(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Orders Pushed"),
                "message": _("Orders pushed to ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_push_products(self):
        self.ensure_one()
        self._push_products(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Products Pushed"),
                "message": _("Products pushed to ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_push_customers(self):
        self.ensure_one()
        self._push_customers(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Customers Pushed"),
                "message": _("Customers pushed to ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_sync_inventory(self):
        self.ensure_one()
        self._sync_inventory(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Inventory Synced"),
                "message": _("Inventory pulled from ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def _build_address_payload(self, partner):
        if not partner:
            return {}
        return {
            "name": partner.name,
            "company": partner.commercial_company_name or "",
            "street1": partner.street or "",
            "street2": partner.street2 or "",
            "city": partner.city or "",
            "state": partner.state_id.code or "",
            "postalCode": partner.zip or "",
            "country": partner.country_id.code or "",
            "phone": partner.phone or partner.mobile or "",
            "email": partner.email or "",
        }

    def _payload_get(self, payload, key, default=None):
        if not isinstance(payload, dict) or not key:
            return default
        current = payload
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current.get(part)
        return current

    def _normalize_category_value(self, value):
        """Return (name, shipstation_category_id) from category-like values."""
        if value in (None, ""):
            return "", ""
        if isinstance(value, dict):
            name = str(value.get("name") or value.get("category") or "").strip()
            category_id = str(
                value.get("categoryId")
                or value.get("id")
                or value.get("productCategoryId")
                or ""
            ).strip()
            return name, category_id

        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return "", ""
            # handle values like "{'categoryId': 36495, 'name': 'wood'}"
            if cleaned.startswith("{") and cleaned.endswith("}"):
                try:
                    parsed = ast.literal_eval(cleaned)
                    if isinstance(parsed, dict):
                        return self._normalize_category_value(parsed)
                except Exception:
                    pass
            return cleaned, ""

        return str(value).strip(), ""

    def _extract_categories_from_direct_response(self, response):
        categories = []
        rows = []
        if isinstance(response, list):
            rows = response
        elif isinstance(response, dict):
            for key in ("categories", "results", "data"):
                if isinstance(response.get(key), list):
                    rows = response.get(key)
                    break
            if not rows:
                rows = [response]

        for row in rows:
            name, category_id = self._normalize_category_value(row)
            if name:
                categories.append((name, category_id))
        return categories

    def _get_store_mapping(self, store_id):
        if not store_id:
            return self.env["shipstation.store.mapping"]
        return self.env["shipstation.store.mapping"].search(
            [
                ("instance_id", "=", self.id),
                ("shipstation_store_id", "=", str(store_id)),
                ("active", "=", True),
            ],
            limit=1,
        )

    def _apply_store_mapping_to_order_vals(self, order_vals, store_id):
        mapping = self._get_store_mapping(store_id)
        if not mapping:
            return order_vals
        if mapping.warehouse_id and "warehouse_id" in self.env["sale.order"]._fields:
            order_vals["warehouse_id"] = mapping.warehouse_id.id
        if mapping.sales_team_id and "team_id" in self.env["sale.order"]._fields:
            order_vals["team_id"] = mapping.sales_team_id.id
        return order_vals

    def _apply_automation_rules_to_order_vals(self, order_data, order_vals):
        weight = 0.0
        for item in order_data.get("items", []):
            qty = float(item.get("quantity") or 0.0)
            wt = float(item.get("weight") or 0.0)
            weight += qty * wt
        country_code = (
            (order_data.get("shipTo") or {}).get("country")
            or (order_data.get("billTo") or {}).get("country")
            or ""
        ).strip().upper()

        for rule in self.automation_rule_ids.filtered("active").sorted(key=lambda r: r.sequence):
            if rule.country_code and rule.country_code.strip().upper() != country_code:
                continue
            if rule.min_weight and weight < rule.min_weight:
                continue
            if rule.max_weight and weight > rule.max_weight:
                continue
            if rule.set_warehouse_id and "warehouse_id" in self.env["sale.order"]._fields:
                order_vals["warehouse_id"] = rule.set_warehouse_id.id
            if rule.set_sales_team_id and "team_id" in self.env["sale.order"]._fields:
                order_vals["team_id"] = rule.set_sales_team_id.id
            if rule.set_shipping_method:
                order_vals["note"] = ((order_vals.get("note") or "") + f"\nShip method: {rule.set_shipping_method}").strip()
            break
        return order_vals

    def _build_order_payload(self, order):
        payload = {
            "orderNumber": order.name,
            "orderDate": self._format_ss_datetime(order.date_order),
            "orderStatus": "awaiting_shipment",
            "customerEmail": order.partner_id.email or "",
            "customerName": order.partner_id.name or "",
            "orderTotal": float(order.amount_total or 0.0),
            "shippingAmount": float(order.amount_delivery or 0.0),
            "billTo": self._build_address_payload(order.partner_invoice_id),
            "shipTo": self._build_address_payload(order.partner_shipping_id),
            "items": [],
        }
        if self.store_id and str(self.store_id).isdigit():
            payload["storeId"] = int(self.store_id)

        for line in order.order_line.filtered(lambda l: not l.display_type):
            sku = line.product_id.default_code or ""
            payload["items"].append({
                "sku": sku,
                "name": line.name or line.product_id.display_name,
                "quantity": float(line.product_uom_qty or 0.0),
                "unitPrice": float(line.price_unit or 0.0),
                "weight": float(line.product_id.weight or 0.0),
            })

        return self._apply_field_mappings(payload, order, "order")

    def _build_product_payload(self, product):
        stock_level = self._get_template_stock_qty(product)
        sku = product.default_code or product.product_variant_id.default_code or ""
        payload = {
            "sku": sku,
            "name": product.name,
            "price": float(product.list_price or 0.0),
            "weight": float(product.weight or 0.0),
            "stockLevel": float(stock_level or 0.0),
        }
        self._apply_field_mappings(payload, product, "product")
        if product.categ_id:
            self._apply_field_mappings(payload, product.categ_id, "category")
        if product.attribute_line_ids:
            mappings = self._get_field_mappings("attribute")
            attributes = product.attribute_line_ids.mapped("attribute_id")
            for mapping in mappings:
                values = []
                for attribute in attributes:
                    value = self._get_record_field_value(attribute, mapping.odoo_field_id.name)
                    if value:
                        values.append(str(value))
                if values:
                    self._set_payload_value(
                        payload,
                        mapping.shipstation_field_key.name,
                        ", ".join(values),
                    )
        return payload

    def _get_template_stock_qty(self, product_template):
        """Compute accurate template stock, optionally scoped to configured warehouse."""
        if not product_template:
            return 0.0
        variants = product_template.product_variant_ids
        if not variants:
            return 0.0
        location = self.inventory_warehouse_id.lot_stock_id if self.inventory_warehouse_id else False
        if not location:
            return float(sum(variants.mapped("qty_available")) or 0.0)
        Quant = self.env["stock.quant"].sudo()
        qty = 0.0
        for variant in variants:
            qty += float(Quant._get_available_quantity(variant, location) or 0.0)
        return qty

    def _build_customer_payload(self, partner):
        payload = {
            "name": partner.name,
            "email": partner.email or "",
            "phone": partner.phone or partner.mobile or "",
        }
        payload.update(self._build_address_payload(partner))
        return self._apply_field_mappings(payload, partner, "customer")

    def _is_customer_endpoint_not_found(self, message):
        text = str(message or "").lower()
        return (
            "error 404" in text
            or "no http resource was found" in text
            or "no action was found on the controller" in text
        )

    def _push_customer_with_fallback(self, payload, customer_id=None):
        calls = []
        clean_id = str(customer_id or "").strip()
        if clean_id and clean_id.isdigit():
            calls.append(("PUT", f"/customers/{int(clean_id)}"))
            calls.append(("POST", "/customers/updatecustomer"))
        calls.append(("POST", "/customers"))
        calls.append(("POST", "/customers/createcustomer"))

        last_error = None
        for method, endpoint in calls:
            try:
                return self._ss_request(method, endpoint, data=payload)
            except Exception as exc:
                last_error = exc
                if self._is_customer_endpoint_not_found(exc):
                    continue
                raise
        if last_error and self._is_customer_endpoint_not_found(last_error):
            return None
        if last_error:
            raise last_error
        raise UserError(_("Unable to push customer to ShipStation."))

    def _push_orders(self, mode="manual"):
        self.ensure_one()
        SaleOrder = self.env["sale.order"].with_company(self.company_id)
        orders = SaleOrder.search([
            ("company_id", "=", self.company_id.id),
            ("state", "in", ("sale", "done")),
        ])
        pushed = 0
        for order in orders:
            if order.shipstation_pushed and order.shipstation_order_id:
                continue
            try:
                payload = self._build_order_payload(order)
                response = self._ss_request("POST", "/orders/createorder", data=payload)
                order_id = response.get("orderId") or response.get("order_id")
                order_number = response.get("orderNumber") or order.name
                order.write({
                    "shipstation_order_id": order_id or order.shipstation_order_id,
                    "shipstation_order_number": order_number,
                    "shipstation_store_id": self.store_id,
                    "shipstation_pushed": True,
                    "shipstation_push_date": fields.Datetime.now(),
                    "shipstation_push_state": "success",
                    "shipstation_push_error": False,
                })
                pushed += 1
            except Exception as exc:
                order.write({
                    "shipstation_push_state": "failed",
                    "shipstation_push_error": str(exc),
                })
                self._create_report(
                    operation="Order Push",
                    status="failed",
                    message=str(exc),
                    mode=mode,
                    reference=order.name,
                )

        self._create_report(
            operation="Order Push",
            status="success",
            message=f"{pushed} orders pushed successfully",
            mode=mode,
        )

    def _push_products(self, mode="manual"):
        self.ensure_one()
        Product = self.env["product.template"].with_company(self.company_id)
        products = Product.search([("company_id", "=", self.company_id.id)])
        pushed = 0
        for product in products:
            if product.shipstation_pushed and product.shipstation_product_id:
                continue
            try:
                payload = self._build_product_payload(product)
                response = self._ss_request("POST", "/products", data=payload)
                product_id = response.get("productId") or response.get("product_id")
                product.write({
                    "shipstation_product_id": product_id or product.shipstation_product_id,
                    "shipstation_pushed": True,
                    "shipstation_push_date": fields.Datetime.now(),
                    "shipstation_push_state": "success",
                    "shipstation_push_error": False,
                })
                pushed += 1
            except Exception as exc:
                product.write({
                    "shipstation_push_state": "failed",
                    "shipstation_push_error": str(exc),
                })
                self._create_report(
                    operation="Product Push",
                    status="failed",
                    message=str(exc),
                    mode=mode,
                    reference=product.display_name,
                )

        self._create_report(
            operation="Product Push",
            status="success",
            message=f"{pushed} products pushed successfully",
            mode=mode,
        )

    def _push_customers(self, mode="manual"):
        self.ensure_one()
        Partner = self.env["res.partner"].with_company(self.company_id)
        partners = Partner.search([
            ("company_id", "=", self.company_id.id),
            ("customer_rank", ">", 0),
        ])
        pushed = 0
        skipped = 0
        for partner in partners:
            if partner.shipstation_pushed and partner.shipstation_customer_id:
                continue
            try:
                payload = self._build_customer_payload(partner)
                response = self._push_customer_with_fallback(
                    payload,
                    customer_id=partner.shipstation_customer_id,
                )
                if not response:
                    skipped += 1
                    self._create_report(
                        operation="Customer Push",
                        status="failed",
                        message="Customer push skipped: endpoint not supported on this API cluster.",
                        mode=mode,
                        reference=partner.display_name,
                    )
                    continue
                customer_id = response.get("customerId") or response.get("customer_id")
                partner.write({
                    "shipstation_customer_id": customer_id or partner.shipstation_customer_id,
                    "shipstation_pushed": True,
                    "shipstation_push_date": fields.Datetime.now(),
                    "shipstation_push_state": "success",
                    "shipstation_push_error": False,
                })
                pushed += 1
            except Exception as exc:
                partner.write({
                    "shipstation_push_state": "failed",
                    "shipstation_push_error": str(exc),
                })
                self._create_report(
                    operation="Customer Push",
                    status="failed",
                    message=str(exc),
                    mode=mode,
                    reference=partner.display_name,
                )

        self._create_report(
            operation="Customer Push",
            status="success",
            message=f"{pushed} customers pushed successfully, {skipped} skipped",
            mode=mode,
        )

    def _sync_orders(self, mode="manual"):
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(
            self.last_order_sync_at, days_back=self.order_sync_days_back or 7
        )
        page = 1
        page_size = 100
        synced = 0
        fetched_any = False

        while True:
            params = {
                "page": page,
                "pageSize": page_size,
                "sortBy": "ModifyDate",
                "sortDir": "ASC",
                "modifyDateStart": self._format_ss_datetime(start_dt),
                "modifyDateEnd": self._format_ss_datetime(end_dt),
            }
            if self.store_id and str(self.store_id).isdigit():
                params["storeId"] = self.store_id
            if self.order_status and self.order_status != "all":
                params["orderStatus"] = self.order_status
            _logger.info("ShipStation order sync params: %s", params)
            data = self._ss_request("GET", "/orders", params=params)
            orders = data.get("orders", [])
            if not orders:
                break
            fetched_any = True

            for order_data in orders:
                try:
                    order_id = str(order_data.get("orderId") or "")
                    order_number = order_data.get("orderNumber") or ""
                    store_id = str(order_data.get("storeId") or "")
                    partner, shipping_partner = self._find_partner(order_data)
                    order_lines = self._prepare_lines(order_data)
                    order_vals = {
                        "partner_id": partner.id,
                        "partner_invoice_id": partner.id,
                        "partner_shipping_id": shipping_partner.id,
                        "date_order": self._parse_ss_datetime(order_data.get("orderDate")) or fields.Datetime.now(),
                        "client_order_ref": order_number,
                        "shipstation_order_id": order_id or False,
                        "shipstation_order_number": order_number or False,
                        "shipstation_store_id": store_id or False,
                        "shipstation_last_modified_date": self._parse_ss_datetime(order_data.get("modifyDate")),
                        "shipstation_status": order_data.get("orderStatus"),
                        "company_id": self.company_id.id,
                    }
                    order_vals = self._apply_store_mapping_to_order_vals(order_vals, store_id)
                    order_vals = self._apply_automation_rules_to_order_vals(order_data, order_vals)
                    order = self._find_order(order_id, order_number, store_id)
                    if order:
                        order.write(order_vals)
                        if (
                            str(order_data.get("orderStatus") or "").lower() == "cancelled"
                            and order.state not in ("cancel", "done")
                        ):
                            order.action_cancel()
                        if order.state in ("draft", "sent"):
                            order.order_line.unlink()
                            order.write({"order_line": order_lines})
                    else:
                        order_vals["order_line"] = order_lines
                        self.env["sale.order"].with_company(self.company_id).create(order_vals)

                    self.env["shipstation.order.sync"]._upsert_from_payload(
                        self, order_data
                    )
                    synced += 1
                except Exception as exc:
                    _logger.exception("ShipStation order sync failed: %s", exc)
                    self._create_report(
                        operation="Order Sync",
                        status="failed",
                        message=str(exc),
                        mode=mode,
                        reference=order_data.get("orderNumber"),
                    )

            if len(orders) < page_size:
                break
            page += 1

        if mode == "manual" and not fetched_any and self.last_order_sync_at:
            start_dt, end_dt = self._get_sync_window(
                None, days_back=self.order_sync_days_back or 7
            )
            page = 1
            while True:
                params = {
                    "page": page,
                    "pageSize": page_size,
                    "sortBy": "ModifyDate",
                    "sortDir": "ASC",
                    "modifyDateStart": self._format_ss_datetime(start_dt),
                    "modifyDateEnd": self._format_ss_datetime(end_dt),
                }
                if self.store_id and str(self.store_id).isdigit():
                    params["storeId"] = self.store_id
                if self.order_status and self.order_status != "all":
                    params["orderStatus"] = self.order_status
                _logger.info("ShipStation order sync fallback params: %s", params)
                data = self._ss_request("GET", "/orders", params=params)
                orders = data.get("orders", [])
                if not orders:
                    break
                for order_data in orders:
                    try:
                        order_id = str(order_data.get("orderId") or "")
                        order_number = order_data.get("orderNumber") or ""
                        store_id = str(order_data.get("storeId") or "")
                        partner, shipping_partner = self._find_partner(order_data)
                        order_lines = self._prepare_lines(order_data)
                        order_vals = {
                            "partner_id": partner.id,
                            "partner_invoice_id": partner.id,
                            "partner_shipping_id": shipping_partner.id,
                        "date_order": self._parse_ss_datetime(order_data.get("orderDate")) or fields.Datetime.now(),
                            "client_order_ref": order_number,
                            "shipstation_order_id": order_id or False,
                            "shipstation_order_number": order_number or False,
                            "shipstation_store_id": store_id or False,
                        "shipstation_last_modified_date": self._parse_ss_datetime(order_data.get("modifyDate")),
                            "shipstation_status": order_data.get("orderStatus"),
                            "company_id": self.company_id.id,
                        }
                        order_vals = self._apply_store_mapping_to_order_vals(order_vals, store_id)
                        order_vals = self._apply_automation_rules_to_order_vals(order_data, order_vals)
                        order = self._find_order(order_id, order_number, store_id)
                        if order:
                            order.write(order_vals)
                            if (
                                str(order_data.get("orderStatus") or "").lower() == "cancelled"
                                and order.state not in ("cancel", "done")
                            ):
                                order.action_cancel()
                            if order.state in ("draft", "sent"):
                                order.order_line.unlink()
                                order.write({"order_line": order_lines})
                        else:
                            order_vals["order_line"] = order_lines
                            self.env["sale.order"].with_company(self.company_id).create(order_vals)

                        self.env["shipstation.order.sync"]._upsert_from_payload(
                            self, order_data
                        )
                        synced += 1
                    except Exception as exc:
                        _logger.exception("ShipStation order sync failed: %s", exc)
                        self._create_report(
                            operation="Order Sync",
                            status="failed",
                            message=str(exc),
                            mode=mode,
                            reference=order_data.get("orderNumber"),
                        )
                if len(orders) < page_size:
                    break
                page += 1

        if mode == "manual" and not fetched_any:
            page = 1
            while True:
                params = {
                    "page": page,
                    "pageSize": page_size,
                    "sortBy": "ModifyDate",
                    "sortDir": "ASC",
                }
                if self.store_id and str(self.store_id).isdigit():
                    params["storeId"] = self.store_id
                if self.order_status and self.order_status != "all":
                    params["orderStatus"] = self.order_status
                _logger.info("ShipStation order sync no-date params: %s", params)
                data = self._ss_request("GET", "/orders", params=params)
                orders = data.get("orders", [])
                if not orders:
                    break
                for order_data in orders:
                    try:
                        order_id = str(order_data.get("orderId") or "")
                        order_number = order_data.get("orderNumber") or ""
                        store_id = str(order_data.get("storeId") or "")
                        partner, shipping_partner = self._find_partner(order_data)
                        order_lines = self._prepare_lines(order_data)
                        order_vals = {
                            "partner_id": partner.id,
                            "partner_invoice_id": partner.id,
                            "partner_shipping_id": shipping_partner.id,
                            "date_order": self._parse_ss_datetime(order_data.get("orderDate")) or fields.Datetime.now(),
                            "client_order_ref": order_number,
                            "shipstation_order_id": order_id or False,
                            "shipstation_order_number": order_number or False,
                            "shipstation_store_id": store_id or False,
                            "shipstation_last_modified_date": self._parse_ss_datetime(order_data.get("modifyDate")),
                            "shipstation_status": order_data.get("orderStatus"),
                            "company_id": self.company_id.id,
                        }
                        order_vals = self._apply_store_mapping_to_order_vals(order_vals, store_id)
                        order_vals = self._apply_automation_rules_to_order_vals(order_data, order_vals)
                        order = self._find_order(order_id, order_number, store_id)
                        if order:
                            order.write(order_vals)
                            if (
                                str(order_data.get("orderStatus") or "").lower() == "cancelled"
                                and order.state not in ("cancel", "done")
                            ):
                                order.action_cancel()
                            if order.state in ("draft", "sent"):
                                order.order_line.unlink()
                                order.write({"order_line": order_lines})
                        else:
                            order_vals["order_line"] = order_lines
                            self.env["sale.order"].with_company(self.company_id).create(order_vals)

                        self.env["shipstation.order.sync"]._upsert_from_payload(
                            self, order_data
                        )
                        synced += 1
                    except Exception as exc:
                        _logger.exception("ShipStation order sync failed: %s", exc)
                        self._create_report(
                            operation="Order Sync",
                            status="failed",
                            message=str(exc),
                            mode=mode,
                            reference=order_data.get("orderNumber"),
                        )
                if len(orders) < page_size:
                    break
                page += 1

        self.last_order_sync_at = end_dt
        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Order Sync",
            status="success",
            message=f"{synced} orders synced successfully",
            mode=mode,
        )

    def _sync_shipments(self, mode="manual"):
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(self.last_shipment_sync_at)
        page = 1
        page_size = 100
        synced = 0

        while True:
            params = {
                "page": page,
                "pageSize": page_size,
                "sortBy": "ModifyDate",
                "sortDir": "ASC",
                "modifyDateStart": self._format_ss_datetime(start_dt),
                "modifyDateEnd": self._format_ss_datetime(end_dt),
            }
            if self.store_id and str(self.store_id).isdigit():
                params["storeId"] = self.store_id
            _logger.info("ShipStation shipment sync params: %s", params)
            data = self._ss_request("GET", "/shipments", params=params)
            shipments = data.get("shipments", [])
            if not shipments:
                break

            for shipment_data in shipments:
                try:
                    order_id = str(shipment_data.get("orderId") or "")
                    order_number = shipment_data.get("orderNumber") or ""
                    store_id = str(shipment_data.get("storeId") or "")
                    order = self._find_order(order_id, order_number, store_id)
                    if not order:
                        raise UserError(_("Order not found for shipment %s.") % order_number)

                    picking = order.picking_ids.filtered(
                        lambda p: p.state != "cancel" and p.picking_type_code == "outgoing"
                    )[:1]
                    if not picking:
                        raise UserError(_("No outgoing picking found for order %s.") % order.name)

                    carrier_code = shipment_data.get("carrierCode") or shipment_data.get("carrierName")
                    tracking_number = shipment_data.get("trackingNumber")
                    service_code = shipment_data.get("serviceCode")
                    ship_date = fields.Datetime.to_datetime(shipment_data.get("shipDate"))

                    carrier = False
                    if carrier_code:
                        mapping = self.env["shipstation.carrier.mapping"].search(
                            [
                                ("instance_id", "=", self.id),
                                ("shipstation_carrier_code", "=", carrier_code),
                                ("active", "=", True),
                            ],
                            limit=1,
                        )
                        carrier = mapping.odoo_carrier_id if mapping else False
                        if not carrier:
                            carrier = self.env["delivery.carrier"].search(
                                [("name", "ilike", carrier_code)], limit=1
                            )

                    vals = {
                        "shipstation_shipment_id": str(shipment_data.get("shipmentId") or ""),
                        "shipstation_tracking_number": tracking_number,
                        "shipstation_carrier_code": carrier_code,
                        "shipstation_service_code": service_code,
                        "shipstation_ship_date": ship_date,
                        "carrier_tracking_ref": tracking_number,
                    }
                    if carrier:
                        vals["carrier_id"] = carrier.id
                    picking.write(vals)

                    self.env["shipstation.shipment.sync"]._upsert_from_payload(
                        self, shipment_data
                    )
                    synced += 1
                except Exception as exc:
                    _logger.exception("ShipStation shipment sync failed: %s", exc)
                    self._create_report(
                        operation="Shipment Sync",
                        status="failed",
                        message=str(exc),
                        mode=mode,
                        reference=shipment_data.get("orderNumber"),
                    )

            if len(shipments) < page_size:
                break
            page += 1

        self.last_shipment_sync_at = end_dt
        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Shipment Sync",
            status="success",
            message=f"{synced} shipments synced successfully",
            mode=mode,
        )

    def _sync_products(self, mode="manual"):
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(self.last_product_sync_at)
        page = 1
        page_size = 100
        synced = 0
        fetched_any = False

        while True:
            legacy_params = {
                "page": page,
                "pageSize": page_size,
                "sortBy": "ModifyDate",
                "sortDir": "ASC",
                "modifyDateStart": self._format_ss_datetime(start_dt),
                "modifyDateEnd": self._format_ss_datetime(end_dt),
            }
            if self.store_id and str(self.store_id).isdigit():
                legacy_params["storeId"] = self.store_id
            _logger.info("ShipStation product sync params: %s", legacy_params)
            data = self._fetch_products_with_fallback(page=page, page_size=page_size, legacy_params=legacy_params)
            products = data.get("products", [])
            if not products:
                break
            fetched_any = True

            for product_data in products:
                try:
                    _logger.info(
                        "ShipStation product sync raw payload sku=%s payload=%s",
                        product_data.get("sku"),
                        str(product_data)[:1500],
                    )
                    self.env["shipstation.product.sync"]._upsert_from_payload(
                        self, product_data
                    )
                    self.env["shipstation.inventory"]._upsert_from_payload(
                        self, product_data
                    )
                    synced += 1
                except Exception as exc:
                    _logger.exception("ShipStation product sync failed: %s", exc)
                    self._create_report(
                        operation="Product Sync",
                        status="failed",
                        message=str(exc),
                        mode=mode,
                        reference=product_data.get("sku"),
                    )

            if len(products) < page_size:
                break
            page += 1

        if mode == "manual" and not fetched_any:
            page = 1
            while True:
                legacy_params = {
                    "page": page,
                    "pageSize": page_size,
                    "sortBy": "ModifyDate",
                    "sortDir": "ASC",
                }
                if self.store_id and str(self.store_id).isdigit():
                    legacy_params["storeId"] = self.store_id
                _logger.info("ShipStation product sync no-date params: %s", legacy_params)
                data = self._fetch_products_with_fallback(page=page, page_size=page_size, legacy_params=legacy_params)
                products = data.get("products", [])
                if not products:
                    break
                for product_data in products:
                    try:
                        _logger.info(
                            "ShipStation product sync raw payload sku=%s payload=%s",
                            product_data.get("sku"),
                            str(product_data)[:1500],
                        )
                        self.env["shipstation.product.sync"]._upsert_from_payload(
                            self, product_data
                        )
                        self.env["shipstation.inventory"]._upsert_from_payload(
                            self, product_data
                        )
                        synced += 1
                    except Exception as exc:
                        _logger.exception("ShipStation product sync failed: %s", exc)
                        self._create_report(
                            operation="Product Sync",
                            status="failed",
                            message=str(exc),
                            mode=mode,
                            reference=product_data.get("sku"),
                        )
                if len(products) < page_size:
                    break
                page += 1

        self.last_product_sync_at = end_dt
        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Product Sync",
            status="success",
            message=f"{synced} products synced successfully",
            mode=mode,
        )

    def _sync_customers(self, mode="manual"):
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(self.last_customer_sync_at)
        page = 1
        page_size = 100
        synced = 0
        fetched_any = False

        while True:
            params = {
                "page": page,
                "pageSize": page_size,
                "sortBy": "ModifyDate",
                "sortDir": "ASC",
                "modifyDateStart": self._format_ss_datetime(start_dt),
                "modifyDateEnd": self._format_ss_datetime(end_dt),
            }
            _logger.info("ShipStation customer sync params: %s", params)
            data = self._ss_request("GET", "/customers", params=params)
            customers = data.get("customers", [])
            if not customers:
                break
            fetched_any = True

            for customer_data in customers:
                try:
                    self.env["shipstation.customer.sync"]._upsert_from_payload(
                        self, customer_data
                    )
                    synced += 1
                except Exception as exc:
                    _logger.exception("ShipStation customer sync failed: %s", exc)
                    self._create_report(
                        operation="Customer Sync",
                        status="failed",
                        message=str(exc),
                        mode=mode,
                        reference=customer_data.get("email") or customer_data.get("name"),
                    )

            if len(customers) < page_size:
                break
            page += 1

        if mode == "manual" and not fetched_any:
            page = 1
            while True:
                params = {
                    "page": page,
                    "pageSize": page_size,
                    "sortBy": "ModifyDate",
                    "sortDir": "ASC",
                }
                _logger.info("ShipStation customer sync no-date params: %s", params)
                data = self._ss_request("GET", "/customers", params=params)
                customers = data.get("customers", [])
                if not customers:
                    break
                for customer_data in customers:
                    try:
                        self.env["shipstation.customer.sync"]._upsert_from_payload(
                            self, customer_data
                        )
                        synced += 1
                    except Exception as exc:
                        _logger.exception("ShipStation customer sync failed: %s", exc)
                        self._create_report(
                            operation="Customer Sync",
                            status="failed",
                            message=str(exc),
                            mode=mode,
                            reference=customer_data.get("email") or customer_data.get("name"),
                        )
                if len(customers) < page_size:
                    break
                page += 1

        self.last_customer_sync_at = end_dt
        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Customer Sync",
            status="success",
            message=f"{synced} customers synced successfully",
            mode=mode,
        )

    def _sync_inventory(self, mode="manual"):
        self.ensure_one()
        page = 1
        page_size = 100
        synced = 0
        Quant = self.env["stock.quant"].sudo()
        location = self.inventory_warehouse_id.lot_stock_id if self.inventory_warehouse_id else False
        ProductProduct = self.env["product.product"].with_company(self.company_id)
        while True:
            legacy_params = {"page": page, "pageSize": page_size}
            if self.store_id and str(self.store_id).isdigit():
                legacy_params["storeId"] = int(self.store_id)
            data = self._fetch_products_with_fallback(page=page, page_size=page_size, legacy_params=legacy_params)
            products = (data or {}).get("products") or []
            if not products:
                break
            for product_data in products:
                try:
                    _logger.info(
                        "ShipStation inventory sync raw payload sku=%s payload=%s",
                        product_data.get("sku"),
                        str(product_data)[:1500],
                    )
                    self.env["shipstation.product.sync"]._upsert_from_payload(self, product_data)
                    inventory_record = self.env["shipstation.inventory"]._upsert_from_payload(self, product_data)
                    if self.inventory_update_odoo_stock and location:
                        sku = str(product_data.get("sku") or "").strip()
                        if sku:
                            product = ProductProduct.search([("default_code", "=", sku)], limit=1)
                            if product:
                                target_qty = max(float(inventory_record.stock_level or 0.0), 0.0)
                                current_qty = Quant._get_available_quantity(product, location)
                                diff = target_qty - current_qty
                                if abs(diff) > 0.0001:
                                    Quant._update_available_quantity(product, location, diff)
                    synced += 1
                except Exception as exc:
                    self._create_report(
                        operation="Inventory Sync",
                        status="failed",
                        message=str(exc),
                        mode=mode,
                        reference=product_data.get("sku"),
                    )
            if len(products) < page_size:
                break
            page += 1
        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Inventory Sync",
            status="success",
            message=f"{synced} inventory records synced successfully",
            mode=mode,
        )

    def _sync_categories(self, mode="manual"):
        self.ensure_one()
        categories = {}

        # 1) Prefer direct category endpoint (maps to Reporting Categories UI).
        try:
            direct = self._ss_request("GET", "/products/categories")
            for name, category_id in self._extract_categories_from_direct_response(direct):
                categories[name.lower()] = {"name": name, "category_id": category_id}
        except Exception as exc:
            _logger.info("ShipStation direct category endpoint unavailable: %s", exc)

        # 2) Fallback: infer from product payload values.
        page = 1
        page_size = 100
        mapping_keys = self._get_field_mappings("category").filtered(
            lambda m: m.odoo_field_id.name == "name"
        ).mapped("shipstation_field_key.name")
        key_candidates = [key for key in (mapping_keys + ["category", "productCategory", "category.name"]) if key]

        while True:
            data = self._ss_request("GET", "/products", params={"page": page, "pageSize": page_size})
            products = (data or {}).get("products") or []
            if not products:
                break

            for product_data in products:
                for key in key_candidates:
                    value = self._payload_get(product_data, key)
                    if value in (None, ""):
                        continue
                    name, category_id = self._normalize_category_value(value)
                    if name:
                        categories[name.lower()] = {
                            "name": name,
                            "category_id": category_id or categories.get(name.lower(), {}).get("category_id", ""),
                        }

            if len(products) < page_size:
                break
            page += 1

        synced = 0
        CategorySync = self.env["shipstation.category.sync"]
        # Cleanup older malformed rows where name was saved as dict string.
        for rec in CategorySync.search([("instance_id", "=", self.id)]):
            if rec.name and rec.name.strip().startswith("{") and rec.name.strip().endswith("}"):
                fixed_name, fixed_id = self._normalize_category_value(rec.name)
                if fixed_name:
                    rec.write(
                        {
                            "name": fixed_name,
                            "shipstation_category_id": fixed_id or rec.shipstation_category_id,
                        }
                    )

        for item in categories.values():
            name = item.get("name")
            category_id = item.get("category_id") or ""
            if not name:
                continue
            domain = [("instance_id", "=", self.id), ("name", "=", name)]
            if category_id:
                by_id = CategorySync.search(
                    [("instance_id", "=", self.id), ("shipstation_category_id", "=", category_id)],
                    limit=1,
                )
                existing = by_id or CategorySync.search(domain, limit=1)
            else:
                existing = CategorySync.search(domain, limit=1)
            vals = {
                "instance_id": self.id,
                "name": name,
                "shipstation_category_id": category_id or False,
                "synced_on": fields.Datetime.now(),
            }
            category = self.env["product.category"].search([("name", "=", name)], limit=1)
            if category:
                vals["odoo_category_id"] = category.id
            if existing:
                existing.write(vals)
            else:
                CategorySync.create(vals)
            synced += 1

        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Category Sync",
            status="success",
            message=f"{synced} categories synced successfully",
            mode=mode,
        )

    def _sync_attributes(self, mode="manual"):
        self.ensure_one()
        page = 1
        page_size = 100
        names = set()

        mapping_keys = self._get_field_mappings("attribute").filtered(
            lambda m: m.odoo_field_id.name == "name"
        ).mapped("shipstation_field_key.name")
        key_candidates = [key for key in (mapping_keys + ["attributes", "attribute", "attribute.name"]) if key]

        while True:
            data = self._ss_request("GET", "/products", params={"page": page, "pageSize": page_size})
            products = (data or {}).get("products") or []
            if not products:
                break

            for product_data in products:
                for key in key_candidates:
                    value = self._payload_get(product_data, key)
                    if value in (None, ""):
                        continue
                    if isinstance(value, list):
                        for item in value:
                            name = item.get("name") if isinstance(item, dict) else item
                            if name:
                                names.add(str(name).strip())
                    else:
                        for item in str(value).split(","):
                            name = item.strip()
                            if name:
                                names.add(name)

            if len(products) < page_size:
                break
            page += 1

        synced = 0
        AttributeSync = self.env["shipstation.attribute.sync"]
        for name in names:
            existing = AttributeSync.search(
                [("instance_id", "=", self.id), ("name", "=", name)],
                limit=1,
            )
            vals = {
                "instance_id": self.id,
                "name": name,
                "synced_on": fields.Datetime.now(),
            }
            attribute = self.env["product.attribute"].search([("name", "=", name)], limit=1)
            if attribute:
                vals["odoo_attribute_id"] = attribute.id
            if existing:
                existing.write(vals)
            else:
                AttributeSync.create(vals)
            synced += 1

        self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Attribute Sync",
            status="success",
            message=f"{synced} attributes synced successfully",
            mode=mode,
        )

    @api.model
    def run_cron_sync_orders(self):
        for instance in self.search([("active", "=", True), ("cron_sync_orders", "=", True)]):
            try:
                instance._sync_orders(mode="cron")
            except Exception as exc:
                _logger.exception("ShipStation cron order sync failed: %s", exc)

    @api.model
    def run_cron_sync_shipments(self):
        for instance in self.search([("active", "=", True), ("cron_sync_shipments", "=", True)]):
            try:
                instance._sync_shipments(mode="cron")
            except Exception as exc:
                _logger.exception("ShipStation cron shipment sync failed: %s", exc)

    @api.model
    def run_cron_sync_products(self):
        for instance in self.search([("active", "=", True), ("cron_sync_products", "=", True)]):
            try:
                instance._sync_products(mode="cron")
            except Exception as exc:
                _logger.exception("ShipStation cron product sync failed: %s", exc)

    @api.model
    def run_cron_sync_customers(self):
        for instance in self.search([("active", "=", True), ("cron_sync_customers", "=", True)]):
            try:
                instance._sync_customers(mode="cron")
            except Exception as exc:
                _logger.exception("ShipStation cron customer sync failed: %s", exc)

    @api.model
    def run_cron_sync_inventory(self):
        for instance in self.search([("active", "=", True), ("cron_sync_inventory", "=", True)]):
            try:
                instance._sync_inventory(mode="cron")
            except Exception as exc:
                _logger.exception("ShipStation cron inventory sync failed: %s", exc)
