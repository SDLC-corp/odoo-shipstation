import ast
import base64

import requests

from odoo import fields, models, _
from odoo.exceptions import UserError


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
                "quantity": qty,
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
        allowed_status = {"awaiting_payment", "awaiting_shipment", "on_hold", "cancelled"}
        if status not in allowed_status:
            status = "awaiting_shipment"
        payload["orderStatus"] = status

        customer_email = str(base.get("customerEmail") or source.get("customerEmail") or "").strip()
        if customer_email:
            payload["customerEmail"] = customer_email

        customer_username = str(source.get("customerUsername") or "").strip()
        if customer_username:
            payload["customerUsername"] = customer_username

        store_id = str(base.get("storeId") or source.get("storeId") or self.shipstation_store_id or self.instance_id.store_id or "").strip()
        if store_id and store_id.isdigit():
            payload["storeId"] = int(store_id)

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

    def action_get_rates(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        order_id = self._get_order_id_for_rates()
        if not order_id:
            raise UserError(_("ShipStation order ID is required to get rates."))
        request = {"orderId": order_id}
        if self.carrier_code:
            request["carrierCode"] = self.carrier_code
        if self.service_code:
            request["serviceCode"] = self.service_code
        if self.package_code:
            request["packageCode"] = self.package_code
        if self.confirmation:
            request["confirmation"] = self.confirmation

        response = self.instance_id._ss_request("POST", "/shipments/getrates", data=request)
        rates = response if isinstance(response, list) else response.get("rates", [])
        self.rate_ids.unlink()
        for rate in rates:
            self.env["shipstation.rate"].create({
                "order_id": self.id,
                "carrier_code": rate.get("carrierCode"),
                "service_code": rate.get("serviceCode"),
                "package_code": rate.get("packageCode") or rate.get("packageType"),
                "confirmation": rate.get("confirmation"),
                "carrier_friendly_name": rate.get("carrierFriendlyName"),
                "service_friendly_name": rate.get("serviceFriendlyName"),
                "package_friendly_name": rate.get("packageFriendlyName"),
                "shipment_cost": float(rate.get("shipmentCost") or 0.0),
                "other_cost": float(rate.get("otherCost") or 0.0),
                "tax_amount": float(rate.get("taxAmount") or 0.0),
                "delivery_days": int(rate.get("deliveryDays") or 0) or False,
                "estimated_delivery_date": self.instance_id._parse_ss_datetime(
                    rate.get("estimatedDeliveryDate")
                ),
                "payload": str(rate),
            })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Rates Updated"),
                "message": _("ShipStation rates retrieved."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_select_best_rate(self):
        self.ensure_one()
        if not self.rate_ids:
            raise UserError(_("No rates found. Click Get Rates first."))
        best = sorted(
            self.rate_ids,
            key=lambda r: (r.shipment_cost or 0.0) + (r.other_cost or 0.0) + (r.tax_amount or 0.0),
        )[0]
        self.write(
            {
                "carrier_code": best.carrier_code,
                "service_code": best.service_code,
                "package_code": best.package_code,
                "confirmation": best.confirmation,
            }
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Best Rate Selected"),
                "message": _("Cheapest rate has been applied to the order."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_create_label(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        order_id = self._get_order_id_for_rates()
        if not order_id:
            raise UserError(_("ShipStation order ID is required to create a label."))
        carrier_code = self.carrier_code
        service_code = self.service_code
        package_code = self.package_code
        if not (carrier_code and service_code and package_code):
            raise UserError(_("Set carrier, service, and package before creating a label."))

        request = {
            "orderId": order_id,
            "carrierCode": carrier_code,
            "serviceCode": service_code,
            "packageCode": package_code,
        }
        if self.confirmation:
            request["confirmation"] = self.confirmation
        ship_date = self.ship_date or fields.Datetime.now()
        request["shipDate"] = self.instance_id._format_ss_datetime(ship_date)

        response = self.instance_id._ss_request("POST", "/shipments/createlabel", data=request)
        self.shipstation_shipment_id = str(response.get("shipmentId") or "") or False
        self.tracking_number = response.get("trackingNumber") or False
        self.tracking_url = response.get("trackingUrl") or False
        self.label_url = response.get("labelDownload") or False
        self.payload = str(response) if response else self.payload

        # Download and attach label PDF to related delivery order when available.
        if self.label_url:
            try:
                pdf_resp = requests.get(self.label_url, timeout=30)
                if pdf_resp.status_code < 400 and pdf_resp.content:
                    order = self.instance_id._find_order(
                        self.shipstation_order_id,
                        self.shipstation_order_number,
                        self.shipstation_store_id,
                    )
                    target_model = "sale.order"
                    target_id = order.id if order else False
                    picking = order.picking_ids.filtered(
                        lambda p: p.state != "cancel" and p.picking_type_code == "outgoing"
                    )[:1] if order else False
                    if picking:
                        target_model = "stock.picking"
                        target_id = picking.id
                    if target_id:
                        self.env["ir.attachment"].create(
                            {
                                "name": f"ShipStation_Label_{self.shipstation_order_number or self.id}.pdf",
                                "datas": base64.b64encode(pdf_resp.content),
                                "mimetype": "application/pdf",
                                "res_model": target_model,
                                "res_id": target_id,
                            }
                        )
            except Exception:
                # Do not fail label creation if attachment download/storage fails.
                pass

        if self.shipstation_shipment_id:
            self.env["shipstation.shipment.sync"]._upsert_from_payload(self.instance_id, {
                "shipmentId": self.shipstation_shipment_id,
                "orderId": self.shipstation_order_id,
                "orderNumber": self.shipstation_order_number,
                "storeId": self.shipstation_store_id,
                "carrierCode": carrier_code,
                "trackingNumber": self.tracking_number,
                "serviceCode": service_code,
                "shipDate": self.instance_id._format_ss_datetime(ship_date),
                "shipmentStatus": "label_created",
            })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Label Created"),
                "message": _("Shipment label created in ShipStation."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_open_label(self):
        self.ensure_one()
        if not self.label_url:
            raise UserError(_("No label download URL available."))
        return {
            "type": "ir.actions.act_url",
            "url": self.label_url,
            "target": "new",
        }

    def action_push_to_shipstation(self):
        self.ensure_one()
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))
        payload = self._prepare_createorder_payload()
        # ShipStation uses createorder for both create and update semantics.
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
        if not self.instance_id:
            raise UserError(_("Missing ShipStation instance."))

        payload = self._get_order_payload()
        params = {}
        order_id = str(self.shipstation_order_id or payload.get("orderId") or "").strip()
        order_number = str(self.shipstation_order_number or payload.get("orderNumber") or "").strip()
        store_id = str(self.shipstation_store_id or payload.get("storeId") or self.instance_id.store_id or "").strip()

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
        order_id = str(order_data.get("orderId") or "")
        order_number = order_data.get("orderNumber") or ""
        store_id = str(order_data.get("storeId") or "")
        order_number_val = overrides.get("shipstation_order_number", order_number)
        total_amount_val = overrides.get("total_amount", order_data.get("orderTotal"))
        order_date_val = overrides.get("order_date", order_data.get("orderDate"))
        status_val = overrides.get("status", order_data.get("orderStatus"))
        customer_name_val = overrides.get("customer_name", order_data.get("customerName") or order_data.get("customerUsername"))
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
            "currency": order_data.get("currencyCode"),
            "order_date": instance._parse_ss_datetime(order_date_val),
            "last_modified_date": instance._parse_ss_datetime(order_data.get("modifyDate")),
            "synced_on": fields.Datetime.now(),
            "payload": str(order_data),
        }
        existing = self.search(
            [
                ("shipstation_order_id", "=", order_id),
                ("instance_id", "=", instance.id),
            ],
            limit=1,
        )
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)
