import logging
import time
from datetime import timedelta

import requests

from odoo import fields, models, _
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class ShipStationSyncBase(models.AbstractModel):
    """Shared ShipStation API helpers for instance syncs."""

    _name = "shipstation.sync.base"
    _description = "ShipStation Sync Base"
    _abstract = True

    def _log_sync_request(self, status, method, endpoint, params=None, data=None, response_text=None, error_message=None):
        if self._name != "shipstation.instance":
            return
        try:
            self.env.cr.execute("SELECT to_regclass(%s)", ("shipstation_sync_log",))
            exists = self.env.cr.fetchone()[0]
            if not exists:
                return
            self.env["shipstation.sync.log"].create(
                {
                    "instance_id": self.id,
                    "operation": "request",
                    "model_name": endpoint,
                    "status": status,
                    "method": method,
                    "endpoint": endpoint,
                    "request_payload": str(params if params else data),
                    "response_payload": response_text or False,
                    "error_message": error_message or False,
                }
            )
        except Exception:
            # Logging must never break business flow.
            return

    def _extract_ss_error_message(self, response):
        text = (response.text or "").strip()
        try:
            payload = response.json()
        except ValueError:
            return text or str(response.status_code)

        if isinstance(payload, dict):
            parts = []
            for key in ("Message", "MessageDetail", "ExceptionMessage", "message", "detail", "error"):
                value = payload.get(key)
                if value:
                    parts.append(str(value))
            if parts:
                return " | ".join(parts)
        return text or str(payload) or str(response.status_code)

    def _ss_request(self, method, endpoint, params=None, data=None):
        """Perform ShipStation API requests with retry/backoff."""
        self.ensure_one()
        if not self.api_key or not self.api_secret:
            raise UserError(_("ShipStation API credentials are missing."))

        base_url = (self.base_url or "https://ssapi.shipstation.com").rstrip("/")
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        url = f"{base_url}{endpoint}"

        retries = 3
        backoff = 1.0
        for attempt in range(retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    auth=(self.api_key, self.api_secret),
                    params=params,
                    json=data,
                    timeout=30,
                )
            except requests.RequestException as exc:
                _logger.warning("ShipStation request error on %s: %s", url, exc)
                if attempt >= retries:
                    raise UserError(str(exc))
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                _logger.warning("ShipStation transient status %s on %s", response.status_code, url)
                if attempt >= retries:
                    message = self._extract_ss_error_message(response)
                    self._log_sync_request(
                        "failed",
                        method,
                        endpoint,
                        params=params,
                        data=data,
                        response_text=(response.text or "")[:2000],
                        error_message=message,
                    )
                    raise UserError(f"ShipStation API error {response.status_code} on {endpoint}: {message}")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue

            if response.status_code >= 400:
                message = self._extract_ss_error_message(response)
                _logger.warning(
                    "ShipStation request failed %s %s status=%s params=%s data=%s response=%s",
                    method,
                    url,
                    response.status_code,
                    params,
                    data,
                    (response.text or "")[:1000],
                )
                self._log_sync_request(
                    "failed",
                    method,
                    endpoint,
                    params=params,
                    data=data,
                    response_text=(response.text or "")[:2000],
                    error_message=message,
                )
                raise UserError(f"ShipStation API error {response.status_code} on {endpoint}: {message}")

            result = response.json() if response.text else {}
            self._log_sync_request(
                "success",
                method,
                endpoint,
                params=params,
                data=data,
                response_text=(response.text or "")[:2000],
            )
            return result

        return {}

    def _get_sync_window(self, last_sync_at, days_back=7):
        end_dt = fields.Datetime.now()
        start_dt = last_sync_at or (end_dt - timedelta(days=days_back))
        return start_dt, end_dt

    def _format_ss_datetime(self, value):
        """Format datetime for ShipStation API (UTC, ISO 8601)."""
        if not value:
            return False
        dt = fields.Datetime.to_datetime(value)
        if not dt:
            return False
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    def _parse_ss_datetime(self, value):
        """Parse ShipStation datetime values like 2026-02-24T10:10:14.0000000."""
        if not value:
            return False
        clean = str(value)
        if "T" in clean:
            clean = clean.replace("T", " ")
        if "." in clean:
            clean = clean.split(".")[0]
        if "Z" in clean:
            clean = clean.replace("Z", "")
        try:
            return fields.Datetime.to_datetime(clean)
        except Exception:
            return False

    def _create_report(self, operation, status, message="", mode="manual", reference=None):
        report = self.env["shipstation.report"].create({
            "instance_id": self.id,
            "operation": operation,
            "status": status,
            "message": message,
            "mode": mode,
            "reference": reference,
        })
        self.env["shipstation.report.line"].create({
            "report_id": report.id,
            "record_type": operation,
            "name": message or operation,
            "status": "success" if status == "success" else "error",
            "error_message": message if status == "failed" else False,
            "reference": reference,
        })
        return report

    def _find_partner(self, order_data):
        Partner = self.env["res.partner"]
        bill_to = order_data.get("billTo") or {}
        ship_to = order_data.get("shipTo") or {}
        name = order_data.get("customerName") or bill_to.get("name") or "ShipStation Customer"
        email = order_data.get("customerEmail") or bill_to.get("email")
        zip_code = ship_to.get("postalCode") or ""

        partner = False
        if email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if not partner:
            partner = Partner.search([("name", "=", name), ("zip", "=", zip_code)], limit=1)
        if not partner:
            partner = Partner.create({
                "name": name,
                "email": email,
                "phone": bill_to.get("phone"),
                "company_id": self.company_id.id,
            })

        shipping_partner = partner
        if ship_to:
            shipping_partner = Partner.search(
                [
                    ("parent_id", "=", partner.id),
                    ("type", "=", "delivery"),
                    ("name", "=", ship_to.get("name") or name),
                    ("zip", "=", zip_code),
                ],
                limit=1,
            )
            if not shipping_partner:
                state = self.env["res.country.state"].search(
                    [
                        ("code", "=", ship_to.get("state")),
                        ("country_id.code", "=", ship_to.get("country")),
                    ],
                    limit=1,
                )
                country = self.env["res.country"].search(
                    [("code", "=", ship_to.get("country"))], limit=1
                )
                shipping_partner = Partner.create({
                    "parent_id": partner.id,
                    "type": "delivery",
                    "name": ship_to.get("name") or name,
                    "street": ship_to.get("street1"),
                    "street2": ship_to.get("street2"),
                    "city": ship_to.get("city"),
                    "state_id": state.id,
                    "zip": ship_to.get("postalCode"),
                    "country_id": country.id,
                    "phone": ship_to.get("phone"),
                    "email": ship_to.get("email"),
                    "company_id": self.company_id.id,
                })

        return partner, shipping_partner

    def _get_or_create_product(self, sku, name, price):
        Product = self.env["product.product"].with_company(self.company_id)
        product = Product.search([("default_code", "=", sku)], limit=1) if sku else False
        if product:
            return product
        product_vals = {
            "name": name or sku or "ShipStation Item",
            "default_code": sku or False,
            "list_price": price or 0.0,
            "company_id": self.company_id.id,
        }
        product_vals["type"] = "consu" if sku else "service"
        return Product.create(product_vals)

    def _prepare_lines(self, order_data):
        lines = []
        for item in order_data.get("items", []):
            sku = item.get("sku")
            name = item.get("name")
            qty = float(item.get("quantity") or 0.0)
            price = float(item.get("unitPrice") or 0.0)
            product = self._get_or_create_product(sku, name, price)
            lines.append((0, 0, {
                "product_id": product.id,
                "name": name or product.display_name,
                "product_uom_qty": qty,
                "price_unit": price,
            }))
        return lines

    def _find_order(self, order_id, order_number, store_id):
        SaleOrder = self.env["sale.order"].with_company(self.company_id)
        if order_id:
            order = SaleOrder.search([("shipstation_order_id", "=", order_id)], limit=1)
            if order:
                return order
        if not order_number:
            return SaleOrder.browse()
        domain = [("shipstation_order_number", "=", order_number)]
        if store_id:
            domain.append(("shipstation_store_id", "=", store_id))
        return SaleOrder.search(domain, limit=1)

    def _get_field_mappings(self, model_name):
        return self.env["shipstation.field.mapping"].search(
            [
                ("instance_id", "=", self.id),
                ("model", "=", model_name),
                ("active", "=", True),
            ]
        )

    def _get_record_field_value(self, record, field_name):
        if not record or field_name not in record._fields:
            return False
        value = record[field_name]
        field = record._fields[field_name]
        if field.type == "datetime":
            return self._format_ss_datetime(value)
        if field.type == "date":
            return fields.Date.to_string(value) if value else False
        if field.type in ("many2one",):
            return value.display_name if value else False
        if field.type in ("one2many", "many2many"):
            return ", ".join(value.mapped("display_name")) if value else False
        return value

    def _set_payload_value(self, payload, key, value):
        if not key:
            return
        parts = key.split(".")
        current = payload
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def _apply_field_mappings(self, payload, record, model_name):
        for mapping in self._get_field_mappings(model_name):
            value = self._get_record_field_value(record, mapping.odoo_field_id.name)
            if value is None or value is False:
                continue
            if value == "":
                continue
            self._set_payload_value(payload, mapping.shipstation_field_key.name, value)
        return payload
