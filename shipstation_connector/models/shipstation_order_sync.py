import ast
import base64
import hashlib
import json
import logging
import math
from datetime import timedelta
from urllib.parse import urlsplit

import psycopg2.errors

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


class ShipStationOrderSync(models.Model):
    _name = "shipstation.order.sync"
    _description = "ShipStation Order Sync"
    _order = "order_date desc"

    instance_id = fields.Many2one("shipstation.instance", ondelete="cascade", required=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    shipstation_order_id = fields.Char(index=True)
    shipstation_order_number = fields.Char(index=True)
    shipstation_store_id = fields.Char(index=True)
    status = fields.Char()
    customer_name = fields.Char()
    customer_email = fields.Char()
    total_amount = fields.Float()
    currency = fields.Char()
    order_date = fields.Datetime()
    last_modified_date = fields.Datetime()
    synced_on = fields.Datetime(default=fields.Datetime.now)
    payload = fields.Text()
    carrier_code = fields.Char()
    service_code = fields.Char()
    package_code = fields.Char()
    confirmation = fields.Char()
    ship_date = fields.Datetime()
    label_url = fields.Char()
    tracking_url = fields.Char()
    tracking_number = fields.Char()
    shipstation_shipment_id = fields.Char()
    rate_ids = fields.One2many("shipstation.rate", "order_id", string="Rates")
    # S06: quote preparation is local-only until a user explicitly requests rates.
    package_preset_id = fields.Many2one(
        "shipstation.package.preset", ondelete="set null", string="Package Preset",
        domain="[('instance_id', '=', instance_id), ('active', '=', True)]",
    )
    rate_from_postal = fields.Char(string="Origin Postal Code")
    rate_to_country = fields.Char(string="Destination Country (ISO 2)")
    rate_to_postal = fields.Char(string="Destination Postal Code")
    rate_to_state = fields.Char(string="Destination State / Province")
    rate_to_city = fields.Char(string="Destination City")
    rate_residential = fields.Boolean(string="Residential Delivery")
    rate_weight = fields.Float(string="Package Weight")
    rate_weight_units = fields.Selection(
        [("ounces", "Ounces"), ("pounds", "Pounds"), ("grams", "Grams")],
        default="ounces", required=True,
    )
    rate_length = fields.Float(string="Length")
    rate_width = fields.Float(string="Width")
    rate_height = fields.Float(string="Height")
    rate_dimension_units = fields.Selection(
        [("inches", "Inches"), ("centimeters", "Centimeters")],
        default="inches", required=True,
    )
    rate_checked_at = fields.Datetime(readonly=True, copy=False)
    rate_request_hash = fields.Char(readonly=True, copy=False)
    label_state = fields.Selection(
        [("none", "No Label Requested"), ("requested", "Request Sent - Outcome Unknown"),
         ("created", "Label Created"), ("failed", "Request Rejected"),
         ("void_requested", "Void Requested - Outcome Unknown"), ("voided", "Label Voided")],
        default="none", required=True, readonly=True, copy=False, index=True,
        help="Durable guard against buying the same label twice.",
    )
    label_requested_at = fields.Datetime(readonly=True, copy=False)
    label_requested_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    last_void_shipment_id = fields.Char(string="Voided Shipment", readonly=True, copy=False)
    last_void_tracking_number = fields.Char(string="Voided Tracking", readonly=True, copy=False)
    last_void_at = fields.Datetime(string="Voided At", readonly=True, copy=False)
    last_void_by_id = fields.Many2one("res.users", string="Voided By", readonly=True, copy=False)
    label_void_allowed = fields.Boolean(compute="_compute_label_void_allowed", compute_sudo=True)

    @api.depends("instance_id.allow_label_void")
    def _compute_label_void_allowed(self):
        for rec in self:
            rec.label_void_allowed = bool(rec.instance_id.allow_label_void)

    @api.depends("shipstation_order_number", "shipstation_order_id", "customer_name")
    def _compute_display_name(self):
        for rec in self:
            number = rec.shipstation_order_number or rec.shipstation_order_id or _("Order")
            rec.display_name = "%s · %s" % (number, rec.customer_name) if rec.customer_name else number

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
        overrides = {}
        if not instance or not isinstance(payload, dict):
            return overrides

        # Mapping based on selected Odoo field (highest priority).
        field_to_listing = {
            "name": "shipstation_order_number",
            "shipstation_order_number": "shipstation_order_number",
            "shipstation_order_id": "shipstation_order_id",
            "shipstation_store_id": "shipstation_store_id",
            "client_order_ref": "shipstation_order_number",
            "amount_total": "total_amount",
            "date_order": "order_date",
            "shipstation_status": "status",
            "partner_id": "customer_name",
            "partner_invoice_id": "customer_name",
            "partner_shipping_id": "customer_name",
        }
        # Key-wise mapping so listing updates by selected ShipStation key
        # even if Odoo field selection is different.
        key_to_listing = {
            "orderNumber": "shipstation_order_number",
            "orderStatus": "status",
            "orderTotal": "total_amount",
            "orderDate": "order_date",
            "customerName": "customer_name",
            "customerUsername": "customer_name",
            "customerEmail": "customer_email",
            "billTo.name": "customer_name",
            "billTo.email": "customer_email",
            "shipTo.name": "customer_name",
            "shipTo.email": "customer_email",
        }
        for mapping in instance._get_field_mappings("order"):
            shipstation_key = mapping.shipstation_field_key.name
            # User-selected Odoo field mapping should override generic key mapping.
            listing_field = field_to_listing.get(mapping.odoo_field_id.name) or key_to_listing.get(shipstation_key)
            if not listing_field:
                continue
            value = self._payload_get(payload, shipstation_key)
            if value in (None, ""):
                continue
            overrides[listing_field] = value
        return overrides

    def _to_float(self, value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _sanitize_address(self, address):
        if not isinstance(address, dict):
            return {}
        clean = {}
        allowed = {
            "name",
            "company",
            "street1",
            "street2",
            "city",
            "state",
            "postalCode",
            "country",
            "phone",
            "email",
            "residential",
        }
        for key in allowed:
            if key not in address:
                continue
            value = address.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
            clean[key] = value
        country = clean.get("country")
        if isinstance(country, str):
            country = country.strip().upper()
            if len(country) == 2:
                clean["country"] = country
            else:
                clean.pop("country", None)
        return clean

    def _sanitize_items(self, items):
        if not isinstance(items, list):
            return []
        clean_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            sku = str(item.get("sku") or "").strip()
            name = str(item.get("name") or "").strip()
            qty = self._to_float(item.get("quantity"), 0.0)
            price = self._to_float(item.get("unitPrice"), 0.0)
            if qty <= 0:
                qty = 1.0
            if price < 0:
                price = 0.0
            clean = {
                # the real API rejects 1.0: whole numbers only
                "quantity": self.instance_id._ss_item_quantity(qty, sku or name),
                "unitPrice": price,
            }
            if sku:
                clean["sku"] = sku
            if name:
                clean["name"] = name

            weight = item.get("weight")
            if isinstance(weight, dict):
                w_value = self._to_float(weight.get("value"), 0.0)
                w_units = str(weight.get("units") or "").strip().lower()
                if w_value > 0 and w_units in {"ounces", "pounds", "grams"}:
                    clean["weight"] = {"value": w_value, "units": w_units}
            clean_items.append(clean)
        return clean_items

    def _build_payload(self):
        self.ensure_one()
        base = {}
        if self.payload:
            try:
                base = ast.literal_eval(self.payload)
            except Exception:
                base = {}
        base.update({
            "orderId": self.shipstation_order_id or base.get("orderId"),
            "orderNumber": self.shipstation_order_number or base.get("orderNumber"),
            "storeId": self.shipstation_store_id or base.get("storeId"),
            "orderStatus": self.status or base.get("orderStatus"),
            "customerName": self.customer_name or base.get("customerName"),
            "customerEmail": self.customer_email or base.get("customerEmail"),
            "orderTotal": float(self.total_amount or 0.0),
            "orderDate": self.instance_id._format_ss_datetime(self.order_date) if self.order_date else base.get("orderDate"),
        })
        return base

    def _prepare_createorder_payload(self):
        """Normalize payload for ShipStation /orders/createorder."""
        self.ensure_one()
        base = dict(self._build_payload() or {})
        source = self._get_order_payload()

        payload = {}
        order_number = str(base.get("orderNumber") or source.get("orderNumber") or "").strip()
        if order_number:
            payload["orderNumber"] = order_number

        order_date = base.get("orderDate") or source.get("orderDate")
        if order_date:
            payload["orderDate"] = str(order_date).split(".")[0]

        status = str(base.get("orderStatus") or source.get("orderStatus") or "awaiting_shipment").strip().lower()
        if self.shipstation_order_id and status in ("shipped", "cancelled"):
            # The createorder API cannot modify shipped or cancelled orders.
            raise UserError(_("ShipStation does not allow changing %s orders.") % status)
        allowed_status = {"awaiting_payment", "awaiting_shipment", "on_hold", "cancelled"}
        if status not in allowed_status:
            status = "awaiting_shipment"
        payload["orderStatus"] = status

        # createorder is an upsert only when it can identify the order: orderKey (and orderId
        # when known). Without them every push would create a duplicate order in ShipStation.
        order_key = str(source.get("orderKey") or "").strip()
        if not order_key:
            order_key = "odoo-%s-mirror-%s" % (self.instance_id.id, self.id)
        payload["orderKey"] = order_key
        known_id = str(self.shipstation_order_id or source.get("orderId") or "").strip()
        if known_id.isdigit():
            payload["orderId"] = int(known_id)

        customer_email = str(base.get("customerEmail") or source.get("customerEmail") or "").strip()
        if customer_email:
            payload["customerEmail"] = customer_email

        customer_username = str(source.get("customerUsername") or "").strip()
        if customer_username:
            payload["customerUsername"] = customer_username

        if not known_id.isdigit():
            # ShipStation takes the store at advancedOptions.storeId, and only when the order is created.
            store_id = str(self.shipstation_store_id or self.instance_id._remote_store_id(source)
                           or self.instance_id.store_id or "").strip()
            if store_id.isdigit():
                payload["advancedOptions"] = {"storeId": int(store_id)}

        bill_to = self._sanitize_address(source.get("billTo"))
        ship_to = self._sanitize_address(source.get("shipTo"))
        if bill_to:
            payload["billTo"] = bill_to
        if ship_to:
            payload["shipTo"] = ship_to

        items = self._sanitize_items(source.get("items") or base.get("items"))
        if items:
            payload["items"] = items

        shipping_amount = self._to_float(source.get("shippingAmount"), 0.0)
        tax_amount = self._to_float(source.get("taxAmount"), 0.0)
        if shipping_amount >= 0:
            payload["shippingAmount"] = shipping_amount
        if tax_amount >= 0:
            payload["taxAmount"] = tax_amount

        missing = []
        if not payload.get("orderNumber"):
            missing.append("orderNumber")
        if not payload.get("orderDate"):
            missing.append("orderDate")
        for required_address in ("billTo", "shipTo"):  # both are required by the API
            if not payload.get(required_address):
                missing.append(required_address)
        items = payload.get("items")
        if not items or not isinstance(items, list):
            missing.append("items")
        if missing:
            raise UserError(
                _("Cannot push order to ShipStation. Missing required fields: %s")
                % ", ".join(missing)
            )

        return payload

    def _get_order_payload(self):
        if not self.payload:
            return {}
        try:
            payload = ast.literal_eval(self.payload)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _get_order_id_for_rates(self):
        payload = self._get_order_payload()
        order_id = self.shipstation_order_id or payload.get("orderId")
        if order_id and str(order_id).isdigit():
            return int(order_id)
        return order_id

    def _check_rate_operator(self):
        self.ensure_one()
        if not self.env.su and not (
            self.env.user.has_group("shipstation_connector.group_shipstation_manager")
            or self.env.user.has_group("shipstation_connector.group_shipstation_admin")
        ):
            raise AccessError(_("ShipStation Manager access is required to prepare shipping."))
        self.check_access("write")
        if self.instance_id.company_id not in self.env.companies:
            raise AccessError(_("This ShipStation instance is outside your active companies."))

    @staticmethod
    def _rate_notice(title, message, level="success"):
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": title, "message": message, "type": level, "sticky": False,
        }}

    def action_apply_package_preset(self):
        """Fill dimensions and weight from an explicitly selected, same-instance preset."""
        self._check_rate_operator()
        preset = self.package_preset_id
        if not preset or not preset.active or preset.instance_id != self.instance_id:
            raise UserError(_("Choose an active package preset for this ShipStation instance."))
        self.write({
            "package_code": preset.package_code,
            "rate_weight": preset.weight_value,
            "rate_weight_units": preset.weight_units,
            "rate_length": preset.length,
            "rate_width": preset.width,
            "rate_height": preset.height,
            "rate_dimension_units": preset.dimension_units,
            "rate_checked_at": False,
            "rate_request_hash": False,
        })
        return self._rate_notice(_("Package prepared"), _("Package values loaded. Review address and carrier, then Get Rates."))

    def _build_v1_rate_request(self):
        """Validate ShipStation V1 required fields; never infer a weight or origin."""
        self.ensure_one()
        carrier = (self.carrier_code or "").strip()
        origin = (self.rate_from_postal or "").strip()
        country = (self.rate_to_country or "").strip().upper()
        postal = (self.rate_to_postal or "").strip()
        if not carrier:
            raise UserError(_("Enter a ShipStation carrier code before comparing rates."))
        if not origin or not postal or len(country) != 2 or not country.isalpha():
            raise UserError(_("Enter origin postal code, destination postal code and two-letter country code."))
        weight = float(self.rate_weight or 0.0)
        if not math.isfinite(weight) or weight <= 0:
            raise UserError(_("Package weight must be a positive finite number."))
        if self.rate_weight_units not in ("ounces", "pounds", "grams"):
            raise UserError(_("Choose a supported ShipStation weight unit."))
        vals = (self.rate_length, self.rate_width, self.rate_height)
        if any(not math.isfinite(float(v or 0.0)) or float(v or 0.0) < 0 for v in vals):
            raise UserError(_("Package dimensions must be finite and nonnegative."))
        if any(vals) and not all(float(v or 0.0) > 0 for v in vals):
            raise UserError(_("Either enter all three positive dimensions or leave them empty."))
        request = {
            "carrierCode": carrier,
            "fromPostalCode": origin,
            "toCountry": country,
            "toPostalCode": postal,
            "weight": {"value": weight, "units": self.rate_weight_units},
            "residential": bool(self.rate_residential),
        }
        if self.rate_to_state:
            request["toState"] = self.rate_to_state.strip()
        if self.rate_to_city:
            request["toCity"] = self.rate_to_city.strip()
        if self.service_code:
            request["serviceCode"] = self.service_code.strip()
        if self.package_code:
            request["packageCode"] = self.package_code.strip()
        if self.confirmation:
            request["confirmation"] = self.confirmation.strip()
        if all(vals):
            request["dimensions"] = {
                "length": float(self.rate_length), "width": float(self.rate_width),
                "height": float(self.rate_height), "units": self.rate_dimension_units,
            }
        return request

    @staticmethod
    def _rate_fingerprint(request):
        return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _ensure_fresh_rate_quote(self):
        self.ensure_one()
        if not self.rate_ids or not self.rate_checked_at or not self.rate_request_hash:
            raise UserError(_("Retrieve new shipping rates before selecting a service."))
        checked_at = fields.Datetime.to_datetime(self.rate_checked_at)
        if fields.Datetime.now() - checked_at > timedelta(minutes=30):
            raise UserError(_("These rates are over 30 minutes old. Retrieve fresh rates."))
        if self._rate_fingerprint(self._build_v1_rate_request()) != self.rate_request_hash:
            raise UserError(_("Shipment details changed. Retrieve fresh rates before selecting."))

    def _rate_amount(self, rate, key):
        """A non-negative, finite money amount from a ShipStation rate row (absent means 0)."""
        try:
            amount = float(rate.get(key) or 0.0)
        except (ValueError, TypeError):
            raise UserError(_("ShipStation returned a nonnumeric shipping cost."))
        if not math.isfinite(amount) or amount < 0:
            raise UserError(_("ShipStation returned an invalid shipping cost."))
        return amount

    def _rate_days(self, value):
        try:
            return max(int(value or 0), 0) or False
        except (ValueError, TypeError):
            return False

    def action_get_rates(self):
        """Explicitly request V1 *quotes*, never shipping labels or stock updates."""
        self._check_rate_operator()
        request = self._build_v1_rate_request()
        response = self.instance_id._ss_request("POST", "/shipments/getrates", data=request)
        if isinstance(response, list):
            rates = response
        elif isinstance(response, dict) and isinstance(response.get("rates"), list):
            rates = response["rates"]
        else:
            raise UserError(_("ShipStation returned an invalid shipping-rate response. Existing quotes were preserved."))
        if len(rates) > 150:
            raise UserError(_("Too many rates returned; narrow the carrier/service selection."))
        prepared = []
        for rate in rates:
            if not isinstance(rate, dict) or not rate.get("serviceCode"):
                raise UserError(_("ShipStation returned an incomplete rate; previous quotes were preserved."))
            prepared.append({
                "order_id": self.id,
                "carrier_code": rate.get("carrierCode") or request["carrierCode"],
                "service_code": rate["serviceCode"],
                "package_code": rate.get("packageCode") or request.get("packageCode") or False,
                "confirmation": rate.get("confirmation") or request.get("confirmation") or False,
                "carrier_friendly_name": rate.get("carrierFriendlyName"),
                "service_friendly_name": rate.get("serviceName") or rate.get("serviceFriendlyName"),
                "package_friendly_name": rate.get("packageFriendlyName"),
                "shipment_cost": self._rate_amount(rate, "shipmentCost"),
                "other_cost": self._rate_amount(rate, "otherCost"),
                "tax_amount": self._rate_amount(rate, "taxAmount"),
                "delivery_days": self._rate_days(rate.get("deliveryDays")),
                "estimated_delivery_date": self.instance_id._parse_ss_datetime(rate.get("estimatedDeliveryDate")),
                "payload": str(rate),
            })
        # All validation is complete before replacing existing rate rows. Quotes are disposable
        # cache entries and the operator role was verified above, so Managers (who cannot delete
        # arbitrary rates through the ACL) may still refresh them.
        self.rate_ids.sudo().unlink()
        if prepared:
            self.env["shipstation.rate"].create(prepared)
        self.write({"rate_checked_at": fields.Datetime.now(),
                    "rate_request_hash": self._rate_fingerprint(request)})
        return self._rate_notice(
            _("Rates refreshed"),
            _("Received %s shipping quotes. No label has been purchased.") % len(prepared),
            "success" if prepared else "warning",
        )

    def action_select_best_rate(self):
        self._check_rate_operator()
        self._ensure_fresh_rate_quote()
        best = min(self.rate_ids, key=lambda r: (
            float(r.shipment_cost or 0.0) + float(r.other_cost or 0.0) + float(r.tax_amount or 0.0),
            r.id,
        ))
        return best.action_apply_rate()

    def _locked_label_transition(self, check, vals):
        """Under a NOWAIT row lock on a side cursor: run ``check(record)``, then commit ``vals``.

        The commit happens *before* any money moves, so a timeout or crash later leaves a durable
        marker. A concurrent click fails immediately instead of acting twice.
        """
        from odoo import SUPERUSER_ID, api  # local import keeps the offline test loaders light
        self.ensure_one()
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '3s'")
                cr.execute("SELECT 1 FROM shipstation_order_sync WHERE id = %s FOR UPDATE NOWAIT", (self.id,))
                side_env = api.Environment(cr, SUPERUSER_ID, {})
                record = side_env["shipstation.order.sync"].browse(self.id)
                record.invalidate_recordset()
                check(record)
                record.write(vals)
                # commit() only flushes environments that are still referenced (Transaction.envs is a WeakSet): flush explicitly so the
                # durable marker can never be dropped with a collected env (seen on Odoo 18).
                side_env.flush_all()
        except psycopg2.errors.LockNotAvailable:
            raise UserError(_("Another label request for this order is in progress. Wait for it to finish."))
        self.invalidate_recordset()

    def _claim_label_request(self):
        """Atomically claim the right to buy ONE label and make that claim durable.

        If anything later goes wrong (a timeout, a failed commit), the order stays in "requested"
        and cannot be bought again until an administrator has checked ShipStation.
        """
        def check(record):
            if record.shipstation_shipment_id or record.tracking_number or record.label_url \
                    or record.label_state in ("created", "void_requested"):
                raise UserError(self.env._("A label or shipment is already recorded. Verify ShipStation and void "
                                           "it explicitly before buying another label."))
            if record.label_state == "requested":
                raise UserError(self.env._(
                    "A label request was already sent on %s and its outcome is unknown. Check this order "
                    "in ShipStation first; an administrator can then use 'Reset Label Request'.",
                    fields.Datetime.to_string(record.label_requested_at)))

        self._locked_label_transition(check, {
            "label_state": "requested", "label_requested_at": fields.Datetime.now(),
            "label_requested_by_id": self.env.user.id,
        })

    def _claim_void_request(self):
        """Durably mark that a void request is about to be sent (only an active label can be voided)."""
        def check(record):
            if record.label_state != "created":
                raise UserError(self.env._("This label is no longer active (state: %s). Reload the order and "
                                           "check ShipStation before trying again.", record.label_state))
        self._locked_label_transition(check, {"label_state": "void_requested"})

    def _void_bookkeeping_vals(self, by_user=True):
        """Values that free the order for a replacement label while keeping an audit trail."""
        return {
            "label_state": "voided",
            "last_void_shipment_id": self.shipstation_shipment_id,
            "last_void_tracking_number": self.tracking_number,
            "last_void_at": fields.Datetime.now(),
            "last_void_by_id": self.env.uid if by_user else False,
            "shipstation_shipment_id": False, "tracking_number": False,
            "label_url": False, "tracking_url": False,
        }

    @api.model
    def _apply_void_for_shipment(self, instance, shipment_id):
        """ShipStation reports a label as voided (by this connector or by someone in ShipStation)."""
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            return self.browse()
        orders = self.search([
            ("instance_id", "=", instance.id), ("shipstation_shipment_id", "=", shipment_id),
            ("label_state", "in", ("created", "void_requested")),
        ])
        for order in orders:
            order.write(order._void_bookkeeping_vals(by_user=False))
        return orders

    def _persist_label_state(self, vals):
        """Commit label bookkeeping on its own cursor so it survives any later failure or rollback.

        After the claim has been committed on a separate connection, the request's own transaction
        (REPEATABLE READ, snapshot taken earlier) must never UPDATE this row again: PostgreSQL would
        raise a serialization failure and Odoo would retry the whole request - after the label was
        already bought. So *every* write to the row goes through here.
        """
        from odoo import SUPERUSER_ID, api
        with self.env.registry.cursor() as cr:
            side_env = api.Environment(cr, SUPERUSER_ID, {})
            side_env["shipstation.order.sync"].browse(self.id).write(vals)
            # commit() only flushes environments that are still referenced (Transaction.envs is a WeakSet): an inline Environment can be
            # garbage-collected and its write silently dropped (seen on Odoo 18). Flush explicitly.
            side_env.flush_all()
        self.invalidate_recordset()

    def _release_label_request(self, state):
        """Durably record the final state of a claimed label request."""
        self._persist_label_state({"label_state": state})

    def _label_request_body(self, order_id):
        """Body for ``POST /orders/createlabelfororder`` (confirmation, shipDate, testLabel are required)."""
        self.ensure_one()
        now = fields.Datetime.now()
        # ShipStation rejects ship dates in the past.
        ship_day = max(self.ship_date or now, now)
        body = {
            "orderId": order_id,
            "carrierCode": self.carrier_code,
            "serviceCode": self.service_code,
            "packageCode": self.package_code,
            "confirmation": (self.confirmation or "none").strip() or "none",
            "shipDate": self.instance_id._format_ss_date(ship_day),
            "testLabel": False,
        }
        weight = float(self.rate_weight or 0.0)
        if weight > 0 and self.rate_weight_units in ("ounces", "pounds", "grams"):
            body["weight"] = {"value": weight, "units": self.rate_weight_units}
        dims = (self.rate_length, self.rate_width, self.rate_height)
        if all(float(v or 0.0) > 0 for v in dims):
            body["dimensions"] = {
                "length": float(self.rate_length), "width": float(self.rate_width),
                "height": float(self.rate_height), "units": self.rate_dimension_units,
            }
        return body

    def _attach_label_pdf(self, label_data, tracking=False):
        """Store the base64 PDF returned by ShipStation on this record and on the delivery."""
        try:
            raw = base64.b64decode(label_data, validate=True)
        except (ValueError, TypeError):
            _logger.warning("ShipStation returned unreadable label data for order sync %s", self.id)
            return self.env["ir.attachment"]
        if not raw.startswith(b"%PDF"):
            _logger.warning("ShipStation label for order sync %s is not a PDF", self.id)
            return self.env["ir.attachment"]
        name = "ShipStation_Label_%s_%s.pdf" % (self.shipstation_order_number or self.id, tracking or "label")
        # The operator is authorised to buy this label; storing the PDF must not depend on them
        # also holding Sales/Inventory rights, so the lookup and attachments use sudo.
        record = self.sudo()
        Attachment = self.env["ir.attachment"].sudo()
        attachment = Attachment.create({
            "name": name, "datas": label_data, "mimetype": "application/pdf",
            "res_model": self._name, "res_id": self.id,
        })
        order = record.instance_id._find_order(
            self.shipstation_order_id, self.shipstation_order_number, self.shipstation_store_id)
        pickings = order.picking_ids.filtered(
            lambda p: p.state != "cancel" and p.picking_type_code == "outgoing") if order else []
        if len(pickings) == 1:
            Attachment.create({
                "name": name, "datas": label_data, "mimetype": "application/pdf",
                "res_model": "stock.picking", "res_id": pickings.id,
            })
        return attachment

    def action_create_label(self):
        """Buy ONE label for ONE order. Always an explicit, confirmed, single-record click.

        Safeguards: Manager role, company check, a durable "request sent" claim taken before
        the API call (no double purchase on double-click, timeout or crash), no automatic retry
        of the POST, and ``testLabel`` is never set implicitly. Bulk actions and cron jobs never
        call this method.
        """
        self.ensure_one()
        # A hidden button is not a security boundary: block direct RPC calls.
        self._check_rate_operator()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        if self.shipstation_shipment_id or self.tracking_number or self.label_url:
            raise UserError(_("A label or shipment is already recorded. Verify ShipStation and void it explicitly before buying another label."))
        order_id = self._get_order_id_for_rates()
        if not order_id or not str(order_id).isdigit():
            raise UserError(_("A numeric ShipStation order ID is required to create a label."))
        if not (self.carrier_code and self.service_code and self.package_code):
            raise UserError(_("Set carrier, service, and package before creating a label."))
        if (self.status or "").lower() != "awaiting_shipment":
            raise UserError(_("Only orders awaiting shipment can receive a label (current status: %s).", self.status or "unknown"))

        body = self._label_request_body(int(order_id))
        self._claim_label_request()
        try:
            response = self.instance_id._ss_request("POST", "/orders/createlabelfororder", data=body)
        except (UserError, AccessError) as exc:
            # ShipStationAPIError.ambiguous: the answer was lost or unclear (timeout, 5xx, bad body),
            # so ShipStation may have acted. Everything else is a definitive refusal.
            if getattr(exc, "ambiguous", False):
                raise UserError(_(
                    "ShipStation did not give a clear answer, so a label may have been bought. Do NOT retry. "
                    "Check this order in ShipStation; if no label exists an administrator can use "
                    "'Reset Label Request'. (%s)", exc)) from exc
            self._release_label_request("failed")  # nothing was bought
            raise
        shipment_id = str((response or {}).get("shipmentId") or "").strip() if isinstance(response, dict) else ""
        tracking = (response or {}).get("trackingNumber") if isinstance(response, dict) else False
        if not shipment_id and not tracking:
            raise UserError(_(
                "ShipStation accepted the request but returned no shipment. A label may exist: check this "
                "order in ShipStation before trying again."))
        # 1. The purchase itself is recorded durably FIRST, on its own cursor, in ONE write. Nothing
        #    below (attachments, mirror rows, a failing commit) can lose the fact that money was spent,
        #    and nothing below may write this row again (see _persist_label_state).
        stored = {k: v for k, v in response.items() if k not in ("labelData", "formData")}
        self._persist_label_state({
            "shipstation_shipment_id": shipment_id or False,
            "tracking_number": tracking or False,
            "label_state": "created",
            "payload": str(stored),
            "ship_date": fields.Datetime.now(),
        })
        # 2. Conveniences touching OTHER rows. Each is isolated: a failure is reported, never allowed
        #    to undo step 1.
        problems = []
        label_data = response.get("labelData")
        for description, step in (
            (_("attaching the label PDF"), lambda: label_data and self._attach_label_pdf(label_data, tracking)),
            (_("recording the shipment"), lambda: shipment_id and
                self.env["shipstation.shipment.sync"]._upsert_from_payload(self.instance_id, {
                    "shipmentId": shipment_id,
                    "orderId": self.shipstation_order_id,
                    "orderNumber": self.shipstation_order_number,
                    "storeId": self.shipstation_store_id,
                    "carrierCode": self.carrier_code,
                    "trackingNumber": tracking,
                    "serviceCode": self.service_code,
                    "shipDate": self.instance_id._format_ss_datetime(fields.Datetime.now()),
                    "shipmentStatus": "label_created",
                })),
        ):
            try:
                with self.env.cr.savepoint():
                    step()
            except Exception:  # noqa: BLE001
                _logger.exception("ShipStation label bought for order sync %s but %s failed", self.id, description)
                problems.append(description)

        if problems:
            return self._rate_notice(
                _("Label bought - follow-up needed"),
                _("The label was created (tracking %(tracking)s) but Odoo could not finish: %(problems)s. "
                  "The purchase is recorded; run a shipment sync to complete the rest.",
                  tracking=tracking or "pending", problems=", ".join(problems)),
                "warning")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Label Created"),
                "message": _("Shipment label created in ShipStation (tracking %s).", tracking or "pending"),
                "type": "success",
                "sticky": False,
            },
        }

    def action_void_label(self):
        """Ask ShipStation to void (refund) THIS order's label. Explicit, single order, admin only.

        Disabled unless the instance enables it. Never bulk, scheduled or webhook driven: this is
        the only code path that calls the void endpoint. The durable "void requested" marker is
        committed first; a definite refusal restores the label, an unclear answer locks the order
        until an administrator has checked ShipStation.
        """
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can request a label void."))
        self.check_access("write")
        if self.instance_id.company_id not in self.env.companies:
            raise AccessError(_("This ShipStation instance is outside your active companies."))
        if not self.sudo().instance_id.allow_label_void:
            raise UserError(_("Void requests are switched off for this connection. An administrator can "
                              "enable 'Allow Void Label Requests' on the instance."))
        if self.label_state != "created" or not str(self.shipstation_shipment_id or "").isdigit():
            raise UserError(_("Only an active label bought from this order can be voided."))
        shipment_id = str(self.shipstation_shipment_id)
        void_vals = self._void_bookkeeping_vals()  # captured while the identifiers are still on the record
        self._claim_void_request()
        try:
            response = self.instance_id._ss_request(
                "POST", "/shipments/voidlabel", data={"shipmentId": int(shipment_id)})
        except (UserError, AccessError) as exc:
            if getattr(exc, "ambiguous", False):
                raise UserError(_(
                    "ShipStation did not give a clear answer, so the label may have been voided. Do NOT retry. "
                    "The next shipment sync will record the void if it happened; otherwise an administrator can "
                    "use 'Reset Label Request'. (%s)", exc)) from exc
            self._persist_label_state({"label_state": "created"})  # refused: the label is still active
            raise
        approved = response.get("approved") if isinstance(response, dict) else None
        if approved is False:
            self._persist_label_state({"label_state": "created"})
            raise UserError(_("ShipStation did not approve the void: %s",
                              (response.get("message") or _("no reason given"))))
        if approved is not True:
            raise UserError(_("ShipStation's answer to the void request was unclear. Do NOT retry; check this "
                              "order in ShipStation. An administrator can use 'Reset Label Request'."))
        # One write to this row, on the side cursor (see _persist_label_state), then other rows only.
        self._persist_label_state(void_vals)
        problems = []
        try:
            with self.env.cr.savepoint():
                self._record_void_in_mirror(shipment_id, void_vals["last_void_tracking_number"])
        except Exception:  # noqa: BLE001
            _logger.exception("ShipStation label %s voided but the mirror update failed", shipment_id)
            problems.append(_("updating the shipment record"))
        if problems:
            return self._rate_notice(
                _("Label voided - follow-up needed"),
                _("The void was approved but Odoo could not finish: %s. Run a shipment sync.", ", ".join(problems)),
                "warning")
        return self._rate_notice(
            _("Label voided"), _("ShipStation approved the void (refund). A replacement label can now be bought."))

    def _record_void_in_mirror(self, shipment_id, tracking):
        """Mark the shipment record voided and free the delivery (local work, other rows only)."""
        Shipment = self.env["shipstation.shipment.sync"]
        record = Shipment._upsert_from_payload(self.instance_id, {
            "shipmentId": shipment_id, "orderId": self.shipstation_order_id,
            "orderNumber": self.shipstation_order_number, "storeId": self.shipstation_store_id,
            "trackingNumber": tracking, "voided": True,
            "voidDate": self.instance_id._format_ss_datetime(fields.Datetime.now()),
        })
        self.instance_id.sudo().with_context(skip_order_label_bookkeeping=True)._reconcile_single_shipment(
            record.sudo())

    def action_reset_label_request(self):
        """Administrator confirms what happened in ShipStation and re-opens the order for a new action."""
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_admin"):
            raise AccessError(_("Only ShipStation administrators can reset a label request."))
        self.check_access("write")
        if self.label_state == "void_requested":
            self._release_label_request("created")
            return self._rate_notice(
                _("Void request reset"),
                _("The label is treated as active again. If ShipStation did void it, the next shipment sync records it."))
        if self.label_state not in ("requested", "failed"):
            raise UserError(_("There is no pending label request to reset."))
        if self.shipstation_shipment_id or self.tracking_number:
            raise UserError(_("A shipment is already recorded for this order; it cannot be reset."))
        self._release_label_request("none")
        return self._rate_notice(_("Label request reset"), _("A label can be requested again for this order."))

    def action_open_label(self):
        self.ensure_one()
        attachment = self.env["ir.attachment"].search([
            ("res_model", "=", self._name), ("res_id", "=", self.id),
            ("mimetype", "=", "application/pdf"),
        ], order="id desc", limit=1)
        if attachment:
            return {"type": "ir.actions.act_url", "url": "/web/content/%d?download=true" % attachment.id,
                    "target": "new"}
        url = str(self.label_url or "").strip()
        if not url:
            raise UserError(_("No label is available for this order."))
        if urlsplit(url).scheme != "https":
            raise UserError(_("Only secure (https) label links can be opened."))
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}

    def action_push_to_shipstation(self):
        self.ensure_one()
        self._check_rate_operator()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        payload = self._prepare_createorder_payload()
        # ShipStation uses createorder for both create and update semantics (keyed by orderKey).
        response = self.instance_id._ss_request("POST", "/orders/createorder", data=payload)
        order_id = response.get("orderId") or response.get("order_id")
        order_number = response.get("orderNumber")
        if order_id:
            self.shipstation_order_id = str(order_id)
        if order_number:
            self.shipstation_order_number = order_number
        self.synced_on = fields.Datetime.now()
        self.payload = str(response or payload)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Order Pushed"),
                "message": _("Order updated in ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_pull_from_shipstation(self):
        self.ensure_one()
        self._check_rate_operator()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        payload = self._get_order_payload()
        params = {}
        order_id = str(self.shipstation_order_id or payload.get("orderId") or "").strip()
        order_number = str(self.shipstation_order_number or payload.get("orderNumber") or "").strip()
        store_id = str(self.shipstation_store_id or self.instance_id._remote_store_id(payload)
                       or self.instance_id.store_id or "").strip()

        if order_id and order_id.isdigit():
            params["orderId"] = int(order_id)
        elif order_number:
            params["orderNumber"] = order_number

        if store_id and store_id.isdigit():
            params["storeId"] = int(store_id)

        if not params:
            raise UserError(
                _("Set a valid numeric ShipStation order ID or an order number before pulling.")
            )
        data = self.instance_id._ss_request("GET", "/orders", params=params)
        orders = data.get("orders", [])
        if not orders:
            raise UserError(_("No order found in ShipStation for the given ID/number."))
        synced = self._upsert_from_payload(self.instance_id, orders[0])
        if synced and synced.id != self.id:
            self.write(
                {
                    "shipstation_order_id": synced.shipstation_order_id,
                    "shipstation_order_number": synced.shipstation_order_number,
                    "shipstation_store_id": synced.shipstation_store_id,
                    "status": synced.status,
                    "customer_name": synced.customer_name,
                    "customer_email": synced.customer_email,
                    "total_amount": synced.total_amount,
                    "currency": synced.currency,
                    "order_date": synced.order_date,
                    "last_modified_date": synced.last_modified_date,
                    "synced_on": fields.Datetime.now(),
                    "payload": synced.payload,
                }
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Order Pulled"),
                "message": _("Order refreshed from ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def _upsert_from_payload(self, instance, order_data):
        overrides = self._extract_listing_overrides_from_mapping(instance, order_data)
        order_id = str(order_data.get("orderId") or "").strip()
        if not order_id:
            raise UserError(_("ShipStation order ID required to upsert the sync mirror."))
        order_number = order_data.get("orderNumber") or ""
        store_id = instance._attributed_store_id(order_data)
        existing = self.search(
            [
                ("shipstation_order_id", "=", order_id),
                ("instance_id", "=", instance.id),
            ],
            limit=2,
        )
        if len(existing) > 1:
            raise UserError(_("Duplicate order-sync mirrors: reconcile records before importing."))
        previous_status = str(existing.status or "").strip().lower() if existing else ""
        order_number_val = overrides.get("shipstation_order_number", order_number)
        total_amount_val = overrides.get("total_amount", order_data.get("orderTotal"))
        order_date_val = overrides.get("order_date", order_data.get("orderDate"))
        status_val = overrides.get("status", order_data.get("orderStatus"))
        normalized_status = str(status_val or "").strip().lower()
        # The V1 Order model has no customerName: the buyer's name is billTo.name (or shipTo.name).
        customer_name_val = overrides.get("customer_name", (
            order_data.get("customerName") or (order_data.get("billTo") or {}).get("name")
            or (order_data.get("shipTo") or {}).get("name") or order_data.get("customerUsername")))
        customer_email_val = overrides.get("customer_email", order_data.get("customerEmail"))
        vals = {
            "instance_id": instance.id,
            "shipstation_order_id": order_id or False,
            "shipstation_order_number": str(order_number_val).strip() if order_number_val not in (None, "") else False,
            "shipstation_store_id": store_id or False,
            "status": str(status_val).strip() if status_val not in (None, "") else False,
            "customer_name": str(customer_name_val).strip() if customer_name_val not in (None, "") else False,
            "customer_email": str(customer_email_val).strip() if customer_email_val not in (None, "") else False,
            "total_amount": self._to_float(total_amount_val, 0.0),
            # V1 orders state no currency; amounts are in the store's currency, configured on the connection.
            "currency": order_data.get("currencyCode") or instance._order_currency_code() or False,
            "order_date": instance._parse_ss_datetime(order_date_val),
            "last_modified_date": instance._parse_ss_datetime(order_data.get("modifyDate")),
            "synced_on": fields.Datetime.now(),
            "payload": str(order_data),
        }
        if existing:
            existing.write(vals)
            record = existing
        else:
            record = self.create(vals)

        shipments_exist = self.env["shipstation.shipment.sync"].search_count(
            [
                ("instance_id", "=", instance.id),
                "|",
                ("shipstation_order_id", "=", order_id or "__missing__"),
                ("shipstation_order_number", "=", order_number or "__missing__"),
            ]
        )
        should_sync_shipment = normalized_status == "shipped" and (
            previous_status != "shipped" or not shipments_exist
        )
        if should_sync_shipment:
            self.env["shipstation.shipment.sync"].sync_for_order(
                instance,
                {
                    "orderId": order_id,
                    "orderNumber": order_number,
                    "storeId": store_id,
                    "shipDate": order_data.get("shipDate"),
                    "modifyDate": order_data.get("modifyDate"),
                },
                create_placeholder=True,
            )
        return record
