import ast
import hashlib
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


_logger = logging.getLogger(__name__)


class ShipStationInstance(models.Model):
    _name = "shipstation.instance"
    _description = "ShipStation Instance"
    _inherit = "shipstation.sync.base"

    name = fields.Char(required=True)
    base_url = fields.Char(default="https://api.shipstation.com")
    api_key = fields.Char(string="V1 API Key", required=True, groups="shipstation_connector.group_shipstation_admin")
    api_secret = fields.Char(string="V1 API Secret", required=True, groups="shipstation_connector.group_shipstation_admin")
    v2_api_key = fields.Char(
        string="V2 API Key",
        copy=False,
        groups="shipstation_connector.group_shipstation_admin",
        help="Optional independent key for ShipStation V2. Do not reuse the legacy V1 key.",
    )
    store_id = fields.Char()
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    # No callable default on purpose: Odoo would stamp one value onto every existing row at upgrade.
    order_currency_id = fields.Many2one(
        "res.currency",
        string="Order Currency",
        help="Currency of the amounts in this ShipStation store. ShipStation V1 orders do not state a currency, "
             "so this is what order totals are reported in. Leave empty to use the company currency.",
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

    webhook_token = fields.Char(
        string="V1 Webhook Secret", copy=False,
        default=lambda self: secrets.token_urlsafe(32),
        groups="shipstation_connector.group_shipstation_admin",
        help="Treat as a credential. Rotating it invalidates the old callback URL.",
    )
    webhook_target_url = fields.Char(
        string="V1 Webhook Target URL", compute="_compute_webhook_target_url",
        groups="shipstation_connector.group_shipstation_admin",
        help="Copy this HTTPS URL into the ShipStation V1 webhook subscription settings.",
    )

    @api.constrains("base_url")
    def _check_base_url(self):
        """The V2 API key is sent to ``base_url``; refuse anything that is not ShipStation."""
        for rec in self:
            url = rec.base_url or ""
            if not self._is_shipstation_url(url) and not self._custom_base_url_allowed():
                raise ValidationError(_(
                    "The base URL must be an https://*.shipstation.com address "
                    "(the V2 API key is sent to it). Current value: %s", url))

    @api.depends("webhook_token")
    def _compute_webhook_target_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for rec in self:
            rec.webhook_target_url = (
                f"{base}/shipstation/webhook/v1/{rec.id}/{rec.webhook_token}"
                if base.startswith("https://") and rec.id and rec.webhook_token else False
            )

    def action_rotate_v1_webhook_token(self):
        self.ensure_one()
        self._require_shipstation_admin()
        self.check_access("write")
        self.write({"webhook_token": secrets.token_urlsafe(32)})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {
                "title": _("V1 Webhook Secret Rotated"),
                "message": _("Update the subscribed URL in ShipStation. The previous URL no longer works."),
                "type": "warning", "sticky": True,
            },
        }

    webhook_order_create = fields.Boolean()
    webhook_order_update = fields.Boolean()
    webhook_shipment_update = fields.Boolean()

    cron_sync_orders = fields.Boolean(default=True)
    cron_sync_shipments = fields.Boolean(default=True)
    cron_sync_products = fields.Boolean(default=True)
    cron_sync_customers = fields.Boolean(default=False)
    cron_sync_inventory = fields.Boolean(default=False)
    inventory_update_odoo_stock = fields.Boolean(
        default=False,
        help="Legacy setting retained for database compatibility. Automatic Odoo stock adjustments are disabled; ShipStation quantities are read-only snapshots.",
    )
    inventory_warehouse_id = fields.Many2one("stock.warehouse", string="Inventory Warehouse")
    allow_label_void = fields.Boolean(
        string="Allow Void Label Requests", default=False, copy=False,
        groups="shipstation_connector.group_shipstation_admin",
        help="Off by default. Enables the administrator-only 'Void Label (Request Refund)' button on orders. "
             "Voiding asks ShipStation to refund a label and cannot be undone from Odoo.",
    )

    total_orders = fields.Integer(compute="_compute_totals")
    total_shipments = fields.Integer(compute="_compute_totals")
    total_revenue = fields.Float(compute="_compute_totals")
    store_mapping_ids = fields.One2many("shipstation.store.mapping", "instance_id", string="Store Mappings")
    carrier_mapping_ids = fields.One2many("shipstation.carrier.mapping", "instance_id", string="Carrier Mappings")
    automation_rule_ids = fields.One2many("shipstation.automation.rule", "instance_id", string="Automation Rules")

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

    @api.depends_context("uid")
    def _compute_totals(self):
        """Aggregate in SQL (one grouped query per model) instead of loading every mirror row."""
        orders = {
            instance.id: (count, total or 0.0)
            for instance, count, total in self.env["shipstation.order.sync"]._read_group(
                [("instance_id", "in", self.ids)], ["instance_id"],
                ["__count", "total_amount:sum"])
        }
        shipments = {
            instance.id: count
            for instance, count in self.env["shipstation.shipment.sync"]._read_group(
                [("instance_id", "in", self.ids), ("voided", "=", False)], ["instance_id"], ["__count"])
        }
        for rec in self:
            rec.total_orders, rec.total_revenue = orders.get(rec.id, (0, 0.0))
            rec.total_shipments = shipments.get(rec.id, 0)

    def _shipstation_connection_notice(self, version, endpoint):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("ShipStation %s Connected") % version,
                "message": _("ShipStation %s %s endpoint verified for this instance.") % (version, endpoint),
                "type": "success",
                "sticky": False,
            },
        }

    def _require_shipstation_admin(self):
        """Fail before any privileged ShipStation API side effect.

        Manual operations run as the integration (``self.sudo()``), exactly like the scheduled jobs,
        *after* this check: the connector administrator must not also need Sales, Inventory and
        Contacts rights just to import an order. The role and company are enforced here.
        """
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can run instance operations."))
        self.check_access("read")

    @staticmethod
    def _friendly_connection_issue(version, exc):
        """Provide actionable status guidance without printing remote payloads."""
        text = str(exc)
        if "V2 API key is missing" in text:
            return _("V2 API key is missing. Configure it before running the V2 connection test.")
        if "API credentials are missing" in text:
            return _("V1 API credentials are missing. Configure the API key and secret first.")
        for status, guidance in (
            ("401", _("HTTP 401: credentials were rejected; verify the key and secret or V2 key.")),
            ("403", _("the API user lacks permission to access this endpoint.")),
            ("404", _("this API route is unavailable; confirm the API version.")),
            ("429", _("the account is rate-limited; retry after the provider limit resets.")),
            ("500", _("the ShipStation service reported an error; retry later.")),
            ("502", _("the ShipStation service is temporarily unavailable; retry later.")),
            ("503", _("the ShipStation service is temporarily unavailable; retry later.")),
            ("504", _("the ShipStation service timed out; retry later.")),
        ):
            if "API error " + status in text:
                return _("ShipStation %(version)s connection check failed: %(guidance)s") % {
                    "version": version, "guidance": guidance,
                }
        return _("ShipStation %s connection check failed. Verify credentials, network access and API permissions; review the restricted sync logs for details.") % version

    def action_test_v1_connection(self):
        """Read-only legacy diagnostic; requires separate V1 key and secret."""
        self.ensure_one()
        self._require_shipstation_admin()
        try:
            self._ss_request("GET", "/stores")
        except UserError as exc:
            raise UserError(self._friendly_connection_issue("V1", exc)) from None
        return self._shipstation_connection_notice("V1", "/stores")

    def action_test_v2_connection(self):
        """Read-only V2 diagnostic: ``GET /v2/carriers`` is a documented, lightweight authenticated read."""
        self.ensure_one()
        self._require_shipstation_admin()
        try:
            self._ss_v2_request(
                "GET", "/carriers",
                params={"page": 1, "page_size": 1, "include_extended_details": "false"},
            )
        except UserError as exc:
            raise UserError(self._friendly_connection_issue("V2", exc)) from None
        return self._shipstation_connection_notice("V2", "/v2/carriers")

    def action_test_connection(self):
        """Backward-compatible legacy action, now explicitly tests V1."""
        return self.action_test_v1_connection()

    def action_sync_orders(self):
        self.ensure_one()
        self._require_shipstation_admin()
        result = self.sudo()._sync_orders(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Order Sync Completed") if not result["failed"] else _("Order Sync Needs Attention"),
                "message": _("%(processed)s processed, %(failed)s failed. Review sync reports for details.") % result,
                "type": "warning" if result["failed"] else "success",
                "sticky": bool(result["failed"]),
            },
        }

    def action_sync_shipments(self):
        self.ensure_one()
        self._require_shipstation_admin()
        result = self.sudo()._sync_shipments(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Shipment Sync Completed") if not result["failed"] else _("Shipment Sync Needs Attention"),
                "message": _("%(linked)s linked, %(pending)s pending, %(review)s need review, "
                             "%(unmatched)s without an Odoo order, %(voided)s voided, %(failed)s failed. "
                             "Review shipment records and reports.") % {"unmatched": 0, "voided": 0, **result},
                "type": "warning" if (result["failed"] or result["review"] or result["pending"]) else "success",
                "sticky": bool(result["failed"] or result["review"] or result["pending"]),
            },
        }

    def action_sync_products(self):
        self.ensure_one()
        self._require_shipstation_admin()
        self.sudo()._sync_products(mode="manual")
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
        self._require_shipstation_admin()
        self.sudo()._sync_customers(mode="manual")
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
        self._require_shipstation_admin()
        self.sudo()._sync_categories(mode="manual")
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
        self._require_shipstation_admin()
        self.sudo()._sync_attributes(mode="manual")
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
        self._require_shipstation_admin()
        result = self.sudo()._push_orders(mode="manual")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Orders Pushed") if not result["failed"] else _("Order Push Needs Attention"),
                "message": _("%(pushed)s pushed, %(failed)s failed, %(remaining)s still waiting. "
                             "Only confirmed orders with an open delivery are sent.") % result,
                "type": "warning" if result["failed"] else "success",
                "sticky": bool(result["failed"]),
            },
        }

    def action_push_products(self):
        self.ensure_one()
        self._require_shipstation_admin()
        self.sudo()._push_products(mode="manual")
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
        self._require_shipstation_admin()
        self.sudo()._push_customers(mode="manual")
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
        self._require_shipstation_admin()
        self.sudo()._sync_inventory(mode="manual")
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
            "phone": partner.phone or "",
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
            if mapping.warehouse_id.company_id.id != self.company_id.id:
                raise UserError(_("Store mapping warehouse belongs to a different company."))
            order_vals["warehouse_id"] = mapping.warehouse_id.id
        if mapping.sales_team_id and "team_id" in self.env["sale.order"]._fields:
            if mapping.sales_team_id.company_id and mapping.sales_team_id.company_id.id != self.company_id.id:
                raise UserError(_("Store mapping sales team belongs to a different company."))
            order_vals["team_id"] = mapping.sales_team_id.id
        return order_vals

    def _apply_automation_rules_to_order_vals(self, order_data, order_vals):
        weight = 0.0
        for item in order_data.get("items", []):
            qty = float(item.get("quantity") or 0.0)
            wt = item.get("weight") or 0.0
            if isinstance(wt, dict):
                # ShipStation sends item weights as {"value": 6, "units": "ounces"}.
                wt = wt.get("value") or 0.0
            weight += qty * float(wt)
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
                if rule.set_warehouse_id.company_id.id != self.company_id.id:
                    raise UserError(_("Automation warehouse belongs to another company."))
                order_vals["warehouse_id"] = rule.set_warehouse_id.id
            if rule.set_sales_team_id and "team_id" in self.env["sale.order"]._fields:
                if rule.set_sales_team_id.company_id and rule.set_sales_team_id.company_id.id != self.company_id.id:
                    raise UserError(_("Automation sales team belongs to another company."))
                order_vals["team_id"] = rule.set_sales_team_id.id
            if rule.set_shipping_method:
                order_vals["note"] = ((order_vals.get("note") or "") + f"\nShip method: {rule.set_shipping_method}").strip()
            break
        return order_vals

    def _build_order_payload(self, order):
        payload = {
            "orderNumber": order.name,
            # ``orderKey`` makes /orders/createorder an upsert: re-sending the same order
            # (for example after a timeout) updates it instead of creating a duplicate.
            "orderKey": self._order_key(order),
            "orderDate": self._format_ss_datetime(order.date_order),
            "orderStatus": "awaiting_shipment",
            "customerEmail": order.partner_id.email or "",
            "customerName": order.partner_id.name or "",
            "orderTotal": float(order.amount_total or 0.0),
            # ``amount_delivery`` belongs to website_sale, which this module does not require.
            "shippingAmount": float(sum(order.order_line.filtered("is_delivery").mapped("price_subtotal"))),
            "billTo": self._build_address_payload(order.partner_invoice_id),
            "shipTo": self._build_address_payload(order.partner_shipping_id),
            "items": [],
        }
        if order.shipstation_order_id and str(order.shipstation_order_id).isdigit():
            # Per the ShipStation docs, including orderId when updating prevents duplicates.
            payload["orderId"] = int(order.shipstation_order_id)
        elif self.store_id and str(self.store_id).isdigit():
            # The store is assigned through advancedOptions.storeId, and only when the order is created
            # (a top-level storeId is ignored and the order lands in the account's first manual store).
            payload["advancedOptions"] = {"storeId": int(self.store_id)}

        # Delivery charges are sent as shippingAmount, never also as an item.
        # Verified against the real API: quantity must be a whole number and weight a Weight object.
        for line in order.order_line.filtered(lambda l: not l.display_type and not l.is_delivery):
            sku = line.product_id.default_code or ""
            item = {
                "sku": sku,
                "name": line.name or line.product_id.display_name,
                "quantity": self._ss_item_quantity(line.product_uom_qty, line.product_id.display_name),
                "unitPrice": float(line.price_unit or 0.0),
            }
            weight = self._ss_item_weight(line.product_id)
            if weight:
                item["weight"] = weight
            payload["items"].append(item)

        return self._apply_field_mappings(payload, order, "order")

    def _order_key(self, order):
        """Stable, instance-scoped idempotency key for an Odoo sale order."""
        self.ensure_one()
        return "odoo-%s-%s" % (self.id, order.name)

    def _build_customer_payload(self, partner):
        payload = {
            "name": partner.name,
            "email": partner.email or "",
            "phone": partner.phone or "",
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

    PUSH_BATCH_LIMIT = 200  # ShipStation V1 allows 40 calls/min; keep one manual run bounded

    def _push_orders(self, mode="manual"):
        self.ensure_one()
        SaleOrder = self.env["sale.order"].with_company(self.company_id)
        # Only confirmed orders that still have goods to ship. Pushing finished history as
        # "awaiting_shipment" could make the warehouse ship orders a second time. The open-delivery
        # test is part of the query so a database with years of history is never loaded in Python.
        domain = [
            ("company_id", "=", self.company_id.id),
            ("state", "=", "sale"),
            ("shipstation_order_id", "=", False),
            "|", ("shipstation_instance_id", "=", False), ("shipstation_instance_id", "=", self.id),
            ("picking_ids", "any", [("picking_type_code", "=", "outgoing"),
                                    ("state", "not in", ("done", "cancel"))]),
        ]
        limit = self.PUSH_BATCH_LIMIT
        total = SaleOrder.search_count(domain)
        # Orders never attempted go first, so a few permanently failing orders cannot starve the rest.
        orders = list(SaleOrder.search(
            domain + [("shipstation_push_state", "!=", "failed")], order="id", limit=limit))
        if len(orders) < limit:
            orders += list(SaleOrder.search(
                domain + [("shipstation_push_state", "=", "failed")], order="id", limit=limit - len(orders)))
        remaining = max(int(total) - len(orders), 0)
        pushed = failed = 0
        for order in orders:
            # Defence in depth on top of the query: an order that came from ShipStation, or belongs
            # to another instance, must never be exported (again) from here.
            if order.shipstation_order_id:
                continue
            if order.shipstation_instance_id and order.shipstation_instance_id.id != self.id:
                continue
            try:
                with self.env.cr.savepoint():
                    payload = self._build_order_payload(order)
                    response = self._ss_request("POST", "/orders/createorder", data=payload)
                    order_id = response.get("orderId") or response.get("order_id")
                    order_number = response.get("orderNumber") or order.name
                    order.write({
                        "shipstation_instance_id": self.id,
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
                failed += 1
                order.write({
                    "shipstation_push_state": "failed",
                    "shipstation_push_error": str(exc)[:1000],
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
            status="failed" if failed else "success",
            message=(f"{pushed} orders pushed, {failed} failed"
                     + (f", {remaining} more waiting - run again." if remaining else ".")),
            mode=mode,
        )
        return {"pushed": pushed, "failed": failed, "remaining": remaining}

    def _push_products(self, mode="manual"):
        """ShipStation V1 cannot create products: ``/products`` is list/get, and update needs an existing id.

        Products appear in ShipStation from imported orders and connected stores. Refuse explicitly instead
        of sending a request the API does not offer.
        """
        self.ensure_one()
        raise UserError(_("ShipStation's API cannot create products. Products appear in ShipStation from "
                          "imported orders and connected stores; use Sync Products to read them into Odoo."))

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

    def _import_order_record(self, order_data):
        """One order = one atomic Odoo transaction savepoint managed by the caller.

        Lock the remote identity before querying local records, so two cron/webhook
        workers cannot create the same sale order at the same time.
        """
        self.ensure_one()
        if not isinstance(order_data, dict):
            raise UserError(_("Invalid ShipStation order payload."))
        order_id = str(order_data.get("orderId") or "").strip()
        order_number = str(order_data.get("orderNumber") or "").strip()
        if not order_id or not order_number:
            raise UserError(_("ShipStation order ID and number are required; refusing ambiguous import."))
        if not self._remote_store_matches(order_data):
            raise UserError(_("Order store does not match this ShipStation instance."))
        store_id = self._attributed_store_id(order_data)

        # PostgreSQL transaction-level advisory lock; released on transaction end.
        identity = f"shipstation:order:{self.id}:{order_id}".encode("utf-8")
        key = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        order = self._find_order(order_id, order_number, store_id)
        incoming_modified = self._parse_ss_datetime(order_data.get("modifyDate"))
        if (order and incoming_modified and order.shipstation_last_modified_date
                and incoming_modified < order.shipstation_last_modified_date):
            return "stale"

        metadata = {
            "shipstation_instance_id": self.id,
            "shipstation_order_id": order_id,
            "shipstation_order_number": order_number,
            "shipstation_store_id": store_id or False,
            "shipstation_last_modified_date": incoming_modified,
            "shipstation_status": order_data.get("orderStatus") or False,
        }
        if order and order.state not in ("draft", "sent"):
            # Confirmed/cancelled orders are controlled by Odoo fulfillment. Never
            # replace validated lines or automatically cancel completed deliveries.
            order.write(metadata)
        else:
            partner, shipping_partner = self._find_partner(order_data)
            lines = self._prepare_lines(order_data)
            values = {
                **metadata,
                "partner_id": partner.id,
                "partner_invoice_id": partner.id,
                "partner_shipping_id": shipping_partner.id,
                "date_order": self._parse_ss_datetime(order_data.get("orderDate")) or fields.Datetime.now(),
                "client_order_ref": order_number,
                "company_id": self.company_id.id,
            }
            values = self._apply_store_mapping_to_order_vals(values, store_id)
            values = self._apply_automation_rules_to_order_vals(order_data, values)
            if order:
                order.write(values)
                if str(order_data.get("orderStatus") or "").lower() == "cancelled":
                    order.action_cancel()
                else:
                    order.order_line.unlink()
                    order.write({"order_line": lines})
            else:
                values["order_line"] = lines
                self.env["sale.order"].with_company(self.company_id).create(values)

        self.env["shipstation.order.sync"]._upsert_from_payload(self, order_data)
        return "processed"

    def _fetch_order_pages(self, start_dt=None, end_dt=None, *, dated=True, max_pages=100):
        """Bounded pagination; fail closed if the API changes its response shape."""
        page_size = 100
        for page in range(1, max_pages + 1):
            params = {"page": page, "pageSize": page_size, "sortBy": "ModifyDate", "sortDir": "ASC"}
            if dated:
                params.update({
                    "modifyDateStart": self._format_ss_datetime(start_dt, query=True),
                    "modifyDateEnd": self._format_ss_datetime(end_dt, query=True),
                })
            if self.store_id and str(self.store_id).isdigit():
                params["storeId"] = self.store_id
            if self.order_status and self.order_status != "all":
                params["orderStatus"] = self.order_status
            data = self._ss_request("GET", "/orders", params=params)
            if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
                raise UserError(_("ShipStation returned an unexpected orders response."))
            orders = data["orders"]
            if not orders:
                return
            yield orders
            if len(orders) < page_size:
                return
        raise UserError(_("Order import hit the 100-page safety limit; checkpoint was not advanced."))

    def _sync_orders(self, mode="manual"):
        """Unified incremental import with atomic records and honest checkpoints."""
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(
            self.last_order_sync_at, days_back=self.order_sync_days_back or 7
        )
        if self.last_order_sync_at:
            start_dt -= timedelta(minutes=5)  # overlap covers timestamp boundaries
        processed = 0
        stale = 0
        failed = 0
        fetch_error = None
        used_undated_fallback = False

        def consume(pages):
            nonlocal processed, stale, failed
            got_any = False
            for orders in pages:
                got_any = True
                for payload in orders:
                    try:
                        with self.env.cr.savepoint():
                            outcome = self._import_order_record(payload)
                        if outcome == "stale":
                            stale += 1
                        else:
                            processed += 1
                    except Exception as exc:
                        failed += 1
                        _logger.exception("ShipStation order import record failed")
                        ref = payload.get("orderNumber") if isinstance(payload, dict) else None
                        self._create_report(
                            operation="Order Sync", status="failed", mode=mode,
                            message=str(exc)[:1000], reference=ref,
                        )
            return got_any

        try:
            found = consume(self._fetch_order_pages(start_dt, end_dt, dated=True))
            if mode == "manual" and not found and self.last_order_sync_at:
                fallback_start, fallback_end = self._get_sync_window(
                    None, days_back=self.order_sync_days_back or 7
                )
                found = consume(self._fetch_order_pages(fallback_start, fallback_end, dated=True))
            if mode == "manual" and not found:
                used_undated_fallback = True
                consume(self._fetch_order_pages(dated=False))
        except Exception as exc:
            failed += 1
            fetch_error = str(exc)[:1000]
            _logger.exception("ShipStation order import page fetch failed")
            self._create_report(
                operation="Order Sync", status="failed", mode=mode,
                message=f"Page retrieval failed: {fetch_error}",
            )

        # A failed page or record must never cause future syncs to skip it.
        if not failed and not used_undated_fallback:
            self.last_order_sync_at = end_dt
            self.last_sync = fields.Datetime.now()
        status = "failed" if failed else "success"
        self._create_report(
            operation="Order Sync", status=status, mode=mode,
            message=(f"{processed} processed, {stale} stale skipped, {failed} failed. "
                     + ("Checkpoint held for recovery." if failed or used_undated_fallback
                        else "Checkpoint advanced.")),
        )
        return {"processed": processed, "stale": stale, "failed": failed,
                "checkpoint_advanced": not failed and not used_undated_fallback}

    def _reconcile_single_shipment(self, shipment):
        """Link one package conservatively; never select a random delivery.

        Multiple packages are preserved in the shipment sync records. The
        single tracking fields on stock.picking are set only if unoccupied or
        already belong to this exact remote shipment.
        """
        self.ensure_one()
        if shipment.instance_id.id != self.id or shipment.company_id.id != self.company_id.id:
            raise UserError(_("Shipment belongs to another instance or company."))
        if getattr(shipment, "voided", False) is True:
            return self._reconcile_voided_shipment(shipment)
        order = self._find_order(
            shipment.shipstation_order_id, shipment.shipstation_order_number,
            shipment.shipstation_store_id,
        )
        if not order:
            # Shipments created outside Odoo (other channels, or before this connector existed)
            # are not an error: record them, keep them visible, and never block the checkpoint.
            shipment.write({
                "picking_id": False, "link_state": "unmatched",
                "link_message": _("No Odoo sales order matches ShipStation order %s. Import the order, "
                                  "or ignore this shipment if it was not created from Odoo.")
                % (shipment.shipstation_order_number or shipment.shipstation_order_id or ""),
            })
            return "unmatched"
        if order.company_id.id != self.company_id.id or not order.shipstation_instance_id or order.shipstation_instance_id.id != self.id:
            raise UserError(_("Sales order is not owned by this ShipStation instance/company."))
        eligible = order.picking_ids.filtered(
            lambda p: p.company_id.id == self.company_id.id and
            p.state != "cancel" and p.picking_type_code == "outgoing"
        )
        if not eligible:
            shipment.write({"picking_id": False, "link_state": "pending",
                            "link_message": _("No outgoing delivery exists yet. Confirm and fulfill the sale order first.")})
            return "pending"
        shipment_id = str(shipment.shipstation_shipment_id or "").strip()
        exact = eligible.filtered(
            lambda p: shipment_id and str(p.shipstation_shipment_id or "").strip() == shipment_id
        )
        # Deliveries of this order that no other package has claimed yet. After a partial
        # shipment the backorder is typically the only one left; if two or more are still
        # unclaimed the match is ambiguous and a person must decide.
        unclaimed = eligible.filtered(lambda p: not str(p.shipstation_shipment_id or "").strip())
        if len(exact) == 1:
            picking = exact
        elif len(eligible) == 1:
            picking = eligible
        elif len(unclaimed) == 1:
            picking = unclaimed
        else:
            shipment.write({"picking_id": False, "link_state": "review",
                            "link_message": _("Multiple outgoing deliveries exist. Assign the package to the correct picking manually.")})
            return "review"

        old_id = str(picking.shipstation_shipment_id or "").strip()
        old_tracking = str(picking.shipstation_tracking_number or picking.carrier_tracking_ref or "").strip()
        tracking = str(shipment.tracking_number or "").strip()
        if (old_id and old_id != shipment_id) or (old_tracking and tracking and old_tracking != tracking):
            shipment.write({"picking_id": False, "link_state": "review",
                            "link_message": _("Delivery already has a different package or tracking number. No tracking was overwritten.")})
            return "review"
        if not tracking:
            shipment.write({"picking_id": False, "link_state": "pending",
                            "link_message": _("Shipment does not have a tracking number yet.")})
            return "pending"
        vals = {
            "shipstation_shipment_id": shipment_id or False,
            "shipstation_tracking_number": tracking,
            "shipstation_carrier_code": shipment.carrier_code or False,
            "shipstation_service_code": shipment.service_code or False,
            "shipstation_ship_date": shipment.ship_date or False,
            "carrier_tracking_ref": tracking,
        }
        if shipment.carrier_code:
            mapping = self.env["shipstation.carrier.mapping"].search([
                ("instance_id", "=", self.id),
                ("shipstation_carrier_code", "=", shipment.carrier_code),
                ("active", "=", True),
            ], limit=2)
            if len(mapping) > 1:
                raise UserError(_("Ambiguous ShipStation carrier mapping; resolve it first."))
            if mapping and mapping.odoo_carrier_id and mapping.odoo_carrier_id.company_id.id in (False, self.company_id.id):
                vals["carrier_id"] = mapping.odoo_carrier_id.id
        picking.write(vals)
        shipment.write({"picking_id": picking.id, "link_state": "linked", "link_message": False})
        return "linked"

    def _reconcile_voided_shipment(self, shipment):
        """A voided label is not a valid shipment: take back only the tracking we wrote.

        If the voided label had been linked to a delivery, the tracking this connector set is
        removed and the shipment is escalated for an operator ("review"), because the
        delivery may now need a replacement label. An unlinked voided label needs no action.
        """
        if not self.env.context.get("skip_order_label_bookkeeping"):
            # A label voided in ShipStation (by us or by anyone else) frees the order for a replacement.
            self.env["shipstation.order.sync"]._apply_void_for_shipment(self, shipment.shipstation_shipment_id)
        if shipment.link_state == "review":
            return "review"  # already escalated; waiting for an operator to acknowledge
        picking = shipment.picking_id
        shipment_id = str(shipment.shipstation_shipment_id or "").strip()
        if picking and shipment_id and str(picking.shipstation_shipment_id or "").strip() == shipment_id:
            vals = {
                "shipstation_shipment_id": False, "shipstation_tracking_number": False,
                "shipstation_carrier_code": False, "shipstation_service_code": False,
                "shipstation_ship_date": False,
            }
            if shipment.tracking_number and picking.carrier_tracking_ref == shipment.tracking_number:
                vals["carrier_tracking_ref"] = False
            picking.write(vals)
            shipment.write({
                "picking_id": False, "link_state": "review",
                "link_message": _("Label voided in ShipStation. Tracking was removed from delivery %s; "
                                  "a replacement label may be needed.") % picking.display_name,
            })
            self._relink_siblings_after_void(shipment)
            return "review"
        shipment.write({
            "picking_id": False, "link_state": "voided",
            "link_message": _("Label voided in ShipStation; it is not linked to any delivery."),
        })
        return "voided"

    def _relink_siblings_after_void(self, voided):
        """The delivery has just been freed: re-evaluate the other open shipments of the same order.

        A replacement label is often bought before the void is processed, so it was parked in
        review because the delivery still carried the old package. Local work only; no ShipStation call.
        """
        if not voided.shipstation_order_id:
            return
        siblings = self.env["shipstation.shipment.sync"].search([
            ("instance_id", "=", self.id), ("id", "!=", voided.id), ("voided", "=", False),
            ("shipstation_order_id", "=", voided.shipstation_order_id),
            ("link_state", "in", ("review", "pending")),
        ])
        for sibling in siblings:
            try:
                with self.env.cr.savepoint():
                    self._reconcile_single_shipment(sibling)
            except Exception:  # noqa: BLE001 - recovery is best effort; the operator can still recheck
                _logger.warning("Could not re-link shipment %s after a void", sibling.id, exc_info=True)

    def _process_shipment_record(self, shipment_data):
        """Atomic remote shipment import; caller must provide a DB savepoint."""
        self.ensure_one()
        if not isinstance(shipment_data, dict):
            raise UserError(_("Invalid ShipStation shipment payload."))
        shipment_id = str(shipment_data.get("shipmentId") or "").strip()
        order_id = str(shipment_data.get("orderId") or "").strip()
        if not order_id or not (shipment_id or str(shipment_data.get("trackingNumber") or "").strip()):
            raise UserError(_("Shipment order ID and shipment/tracking identity are required."))
        if not self._remote_store_matches(shipment_data):
            raise UserError(_("Shipment store does not match this instance."))
        identity = ("shipstation:shipment:%s:%s" % (self.id, shipment_id or
                    (order_id + ":" + str(shipment_data.get("trackingNumber"))))).encode("utf-8")
        lock_key = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        record = self.env["shipstation.shipment.sync"]._upsert_from_payload(self, shipment_data)
        return self._reconcile_single_shipment(record)

    def _sync_shipments(self, mode="manual"):
        """Incremental shipment import using only parameters documented for ``GET /shipments``.

        That endpoint filters on ``createDate*``, ``shipDate*`` and ``voidDate*`` (it has no
        ``modifyDate*`` filter or ``ModifyDate`` sort), so two windowed passes are made: labels
        created in the window, and labels voided in the window. A shipment returned by both is
        processed once. The checkpoint only advances when nothing failed.
        """
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(self.last_shipment_sync_at)
        if self.last_shipment_sync_at:
            start_dt -= timedelta(minutes=5)  # overlap covers timestamp boundaries
        start = self._format_ss_datetime(start_dt, query=True)
        end = self._format_ss_datetime(end_dt, query=True)
        passes = (
            ("created", {"createDateStart": start, "createDateEnd": end}),
            ("voided", {"voidDateStart": start, "voidDateEnd": end}),
        )
        page_size, max_pages = 100, 100
        linked = pending = review = failed = voided = unmatched = 0
        seen = set()
        aborted = False
        for pass_name, date_params in passes:
            if aborted:
                break
            page = 1
            while page <= max_pages:
                params = {
                    "page": page, "pageSize": page_size,
                    "sortBy": "CreateDate", "sortDir": "ASC", **date_params,
                }
                if self.store_id and str(self.store_id).isdigit():
                    params["storeId"] = self.store_id
                try:
                    response = self._ss_request("GET", "/shipments", params=params)
                    if not isinstance(response, dict) or not isinstance(response.get("shipments"), list):
                        raise UserError(_("Invalid ShipStation shipment listing response."))
                    shipments = response["shipments"]
                    if len(shipments) > page_size:
                        raise UserError(_("ShipStation returned too many shipments in one page."))
                except Exception as exc:
                    failed += 1
                    aborted = True
                    _logger.exception("ShipStation shipment page fetch failed")
                    self._create_report(operation="Shipment Sync", status="failed", mode=mode,
                                        message=str(exc), reference="%s page:%s" % (pass_name, page))
                    break
                if not shipments:
                    break
                for shipment_data in shipments:
                    if isinstance(shipment_data, dict):
                        key = str(shipment_data.get("shipmentId") or shipment_data.get("trackingNumber") or "")
                        if key and key in seen:
                            continue
                        seen.add(key)
                    try:
                        with self.env.cr.savepoint():
                            result = self._process_shipment_record(shipment_data)
                        if result == "linked":
                            linked += 1
                        elif result == "review":
                            review += 1
                        elif result == "voided":
                            voided += 1
                        elif result == "unmatched":
                            unmatched += 1
                        else:
                            pending += 1
                    except Exception as exc:
                        failed += 1
                        _logger.exception("ShipStation shipment record failed")
                        self._create_report(operation="Shipment Sync", status="failed", mode=mode,
                                            message=str(exc), reference=str(shipment_data.get("shipmentId") if isinstance(shipment_data, dict) else "invalid"))
                if len(shipments) < page_size:
                    break
                page += 1
            else:
                failed += 1
                aborted = True
                self._create_report(operation="Shipment Sync", status="failed", mode=mode,
                                    message=_("Shipment page limit reached; checkpoint held."))

        if not failed:
            self.last_shipment_sync_at = end_dt
            self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Shipment Sync", status="failed" if failed else "success", mode=mode,
            message=("%s linked, %s pending, %s need review, %s without an Odoo order, %s voided, "
                     "%s failed; %s." % (linked, pending, review, unmatched, voided, failed,
                                         "checkpoint held" if failed else "checkpoint advanced")),
        )
        return {"linked": linked, "pending": pending, "review": review, "voided": voided,
                "unmatched": unmatched, "failed": failed, "checkpoint_advanced": not failed}

    def _paged_records(self, fetch, key, *, page_size=100, max_pages=200):
        """Yield records page by page from ``fetch(page)``; stop on a short or empty page."""
        for page in range(1, max_pages + 1):
            data = fetch(page)
            records = data.get(key) if isinstance(data, dict) else None
            if not isinstance(records, list):
                raise UserError(_("ShipStation returned an unexpected %s response.") % key)
            if not records:
                return
            yield from records
            if len(records) < page_size:
                return
        raise UserError(_("%s listing hit the %s-page safety limit.") % (key, max_pages))

    def _import_records(self, records, handler, operation, mode, reference_of):
        """Run ``handler(record)`` per record in its own savepoint; return (synced, failed)."""
        synced = failed = 0
        for record in records:
            try:
                with self.env.cr.savepoint():
                    handler(record)
                synced += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                _logger.warning("ShipStation %s failed for one record: %s", operation, exc)
                self._create_report(
                    operation=operation, status="failed", message=str(exc)[:1000], mode=mode,
                    reference=reference_of(record) if isinstance(record, dict) else None,
                )
        return synced, failed

    def _sync_products(self, mode="manual"):
        """Import products. The checkpoint advances only when every record was imported."""
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(self.last_product_sync_at)
        page_size = 100
        synced = failed = 0

        def handler(product_data):
            self.env["shipstation.product.sync"]._upsert_from_payload(self, product_data)
            self.env["shipstation.inventory"]._upsert_from_payload(self, product_data)

        def run(dated):
            nonlocal synced, failed
            def fetch(page):
                params = {"page": page, "pageSize": page_size, "sortBy": "ModifyDate", "sortDir": "ASC"}
                if dated:
                    params["modifyDateStart"] = self._format_ss_datetime(start_dt, query=True)
                    params["modifyDateEnd"] = self._format_ss_datetime(end_dt, query=True)
                if self.store_id and str(self.store_id).isdigit():
                    params["storeId"] = self.store_id
                return self._fetch_products_with_fallback(page=page, page_size=page_size, legacy_params=params)
            count = 0
            batch = []
            for product_data in self._paged_records(fetch, "products", page_size=page_size):
                batch.append(product_data)
                count += 1
            ok, bad = self._import_records(batch, handler, "Product Sync", mode,
                                           lambda rec: rec.get("sku"))
            synced += ok
            failed += bad
            return count

        try:
            found = run(dated=True)
            if mode == "manual" and not found:
                run(dated=False)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _logger.exception("ShipStation product page fetch failed")
            self._create_report(operation="Product Sync", status="failed", mode=mode,
                                message=_("Page retrieval failed: %s") % str(exc)[:1000])

        if not failed:
            self.last_product_sync_at = end_dt
            self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Product Sync", status="failed" if failed else "success", mode=mode,
            message=f"{synced} products synced, {failed} failed. "
                    + ("Checkpoint held for recovery." if failed else "Checkpoint advanced."),
        )
        return {"synced": synced, "failed": failed, "checkpoint_advanced": not failed}

    def _sync_customers(self, mode="manual"):
        """Import customers. The checkpoint advances only when every record was imported."""
        self.ensure_one()
        start_dt, end_dt = self._get_sync_window(self.last_customer_sync_at)
        page_size = 100
        synced = failed = 0

        def handler(customer_data):
            self.env["shipstation.customer.sync"]._upsert_from_payload(self, customer_data)

        def run(dated):
            nonlocal synced, failed
            def fetch(page):
                params = {"page": page, "pageSize": page_size, "sortBy": "ModifyDate", "sortDir": "ASC"}
                if dated:
                    params["modifyDateStart"] = self._format_ss_datetime(start_dt, query=True)
                    params["modifyDateEnd"] = self._format_ss_datetime(end_dt, query=True)
                return self._ss_request("GET", "/customers", params=params)
            batch = list(self._paged_records(fetch, "customers", page_size=page_size))
            ok, bad = self._import_records(batch, handler, "Customer Sync", mode,
                                           lambda rec: rec.get("email") or rec.get("name"))
            synced += ok
            failed += bad
            return len(batch)

        try:
            found = run(dated=True)
            if mode == "manual" and not found:
                run(dated=False)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _logger.exception("ShipStation customer page fetch failed")
            self._create_report(operation="Customer Sync", status="failed", mode=mode,
                                message=_("Page retrieval failed: %s") % str(exc)[:1000])

        if not failed:
            self.last_customer_sync_at = end_dt
            self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Customer Sync", status="failed" if failed else "success", mode=mode,
            message=f"{synced} customers synced, {failed} failed. "
                    + ("Checkpoint held for recovery." if failed else "Checkpoint advanced."),
        )
        return {"synced": synced, "failed": failed, "checkpoint_advanced": not failed}

    def _sync_inventory(self, mode="manual"):
        self.ensure_one()
        page = 1
        page_size = 100
        synced = 0
        failed = 0
        # Never write to stock.quant here. Remote stock is an observation,
        # not an authorized Odoo inventory adjustment.
        if self.inventory_warehouse_id and self.inventory_warehouse_id.company_id != self.company_id:
            raise UserError(_("Inventory warehouse must belong to the ShipStation instance company."))
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
                    inventory_record._snapshot_odoo_inventory(self)
                    synced += 1
                except Exception as exc:
                    failed += 1
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
        if not failed:
            self.last_sync = fields.Datetime.now()
        self._create_report(
            operation="Inventory Sync",
            status="failed" if failed else "success",
            message=f"{synced} inventory records synced; {failed} failed. No Odoo stock quantities were changed.",
            mode=mode,
        )
        if failed:
            _logger.warning("ShipStation inventory sync instance=%s partial failure: %s records", self.id, failed)

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

    def _run_cron_for_instances(self, flag, method_name):
        """Run one sync for every enabled instance, isolating failures per instance.

        Each instance runs in a savepoint so a database error in one connection cannot
        poison the transaction used for the next one.
        """
        for instance in self.search([("active", "=", True), (flag, "=", True)]):
            try:
                with self.env.cr.savepoint():
                    getattr(instance, method_name)(mode="cron")
            except Exception as exc:  # noqa: BLE001
                _logger.exception("ShipStation cron %s failed for instance %s: %s",
                                  method_name, instance.id, exc)

    @api.model
    def run_cron_sync_orders(self):
        self._run_cron_for_instances("cron_sync_orders", "_sync_orders")

    @api.model
    def run_cron_sync_shipments(self):
        self._run_cron_for_instances("cron_sync_shipments", "_sync_shipments")

    @api.model
    def run_cron_sync_products(self):
        self._run_cron_for_instances("cron_sync_products", "_sync_products")

    @api.model
    def run_cron_sync_customers(self):
        self._run_cron_for_instances("cron_sync_customers", "_sync_customers")

    @api.model
    def run_cron_sync_inventory(self):
        self._run_cron_for_instances("cron_sync_inventory", "_sync_inventory")
