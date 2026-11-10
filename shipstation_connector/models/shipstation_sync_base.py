import logging
import re
import time
from datetime import timedelta
from urllib.parse import urlsplit

import pytz
import requests

from odoo import fields, models, _
from odoo.exceptions import AccessError, UserError


_logger = logging.getLogger(__name__)

SHIPSTATION_V1_BASE_URL = "https://ssapi.shipstation.com"
SHIPSTATION_V2_BASE_URL = "https://api.shipstation.com"
# ShipStation V1 documents that every DateTime in requests and responses is PST/PDT.
SHIPSTATION_V1_TIMEZONE = "America/Los_Angeles"
# Never sleep longer than this inside a web worker or cron job, whatever the server asks.
MAX_RATE_LIMIT_WAIT = 60.0
# Odoo's default real-time limit per request is 120 s: one call may never spend more than this asleep.
MAX_TOTAL_WAIT = 45.0
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
_SHIPSTATION_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*shipstation\.com$")


class ShipStationAPIError(UserError):
    """An API failure that tells callers whether the remote side may have acted.

    ``ambiguous`` is True when the request might have been processed even though we
    did not receive a clean answer (timeouts, dropped connections, HTTP 5xx, bad
    bodies). It is False for definitive rejections (HTTP 4xx other than 408), where
    the remote system certainly did nothing. Paid, non-idempotent operations such as
    label purchase use it to decide whether a retry could create a duplicate.
    """

    def __init__(self, message, status_code=None, ambiguous=False):
        super().__init__(message)
        self.status_code = status_code
        self.ambiguous = ambiguous


class ShipStationSyncBase(models.AbstractModel):
    """Shared ShipStation API helpers for instance syncs."""

    _name = "shipstation.sync.base"
    _description = "ShipStation Sync Base"
    _abstract = True

    def _get_shipstation_auth_record(self):
        """Read credentials only after validating instance visibility and operator role.

        sudo is narrowly scoped to the instance credential fields. No user-visible
        method should return this record or secret values.
        """
        self.ensure_one()
        self.check_access("read")
        if not self.env.su and not self.env.user.has_group("shipstation_connector.group_shipstation_manager"):
            raise AccessError(_("ShipStation Manager or Admin privileges are required for API operations."))
        return self.sudo()

    def _normalize_shipstation_base_url(self, base_url):
        normalized = (base_url or SHIPSTATION_V2_BASE_URL).strip().rstrip("/")
        if not normalized:
            normalized = SHIPSTATION_V2_BASE_URL
        normalized = normalized.replace("https://ssapi.shipstation.com", SHIPSTATION_V2_BASE_URL)
        normalized = normalized.replace("http://ssapi.shipstation.com", SHIPSTATION_V2_BASE_URL)
        if normalized == "https://ssapi.shipstation.com":
            normalized = SHIPSTATION_V2_BASE_URL
        return normalized

    @staticmethod
    def _is_shipstation_url(url):
        """True only for ``https://*.shipstation.com`` without credentials or odd ports."""
        try:
            parts = urlsplit(url or "")
            port = parts.port
        except ValueError:
            return False
        return bool(
            parts.scheme == "https" and parts.hostname
            and _SHIPSTATION_HOST.match(parts.hostname.lower())
            and not parts.username and not parts.password and port in (None, 443)
        )

    def _custom_base_url_allowed(self):
        """Developers may point at a local mock server by setting this system parameter."""
        return self.env["ir.config_parameter"].sudo().get_param(
            "shipstation_connector.allow_custom_base_url") in ("1", "True", "true")

    # ------------------------------------------------------------------
    # Request logging
    # ------------------------------------------------------------------
    def _log_sync_request(self, status, method, endpoint, params=None, data=None, response_text=None, error_message=None):
        """Record an API call without ever breaking, or being rolled back with, the business flow.

        The row is written on its own cursor so a failed request is still visible after
        the calling transaction rolls back (for example when a UserError is raised to
        the user). Inside the test suite the registry hands out a cursor on the test
        transaction, so nothing is persisted there.
        """
        if self._name != "shipstation.instance":
            return
        instance_id = self.id
        vals = {
            "instance_id": instance_id,
            "operation": "request",
            "model_name": endpoint,
            "status": status,
            "method": method,
            "endpoint": endpoint,
            "request_payload": str(params if params else data)[:4000],
            "response_payload": (response_text or "")[:2000] or False,
            "error_message": error_message or False,
        }
        try:
            from odoo import SUPERUSER_ID, api  # local import: keeps the offline test loaders light
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '2s'")
                api.Environment(cr, SUPERUSER_ID, {})["shipstation.sync.log"].create(vals)
        except Exception:  # noqa: BLE001 - logging must never break business flow
            _logger.debug("ShipStation request log could not be written", exc_info=True)

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
            errors = payload.get("errors")  # ShipStation V2 error envelope
            if isinstance(errors, list):
                parts.extend(str(e.get("message")) for e in errors if isinstance(e, dict) and e.get("message"))
            if parts:
                return " | ".join(parts)
        return text or str(payload) or str(response.status_code)

    @staticmethod
    def _rate_limit_delay(response, fallback):
        """Seconds to wait before retrying; honours ``X-Rate-Limit-Reset`` (V1) / ``Retry-After``."""
        headers = getattr(response, "headers", None)
        for name in ("X-Rate-Limit-Reset", "Retry-After"):
            try:
                value = float(headers.get(name))
            except (AttributeError, TypeError, ValueError):
                continue
            if value >= 0:
                return min(max(value, 1.0), MAX_RATE_LIMIT_WAIT)
        return fallback

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _http_call(self, label, method, url, endpoint, *, auth=None, headers=None,
                   params=None, data=None, log=True):
        """Single HTTP transport for V1 and V2.

        * GET/HEAD are retried with back-off; every other verb is sent exactly once so a
          retry can never duplicate an order or a paid label.
        * Rate-limit responses wait for the server-provided reset time (capped).
        * Failures raise :class:`ShipStationAPIError`, flagged ambiguous when the remote
          side may have acted.
        """
        method = str(method).upper()
        retries = 3 if method in ("GET", "HEAD") else 0
        backoff = 1.0
        kwargs = {"params": params, "json": data, "timeout": 30}
        if auth is not None:
            kwargs["auth"] = auth
        if headers is not None:
            kwargs["headers"] = headers

        def fail(message, status_code=None, ambiguous=False, response=None):
            if log:
                self._log_sync_request(
                    "failed", method, endpoint, params=params, data=data,
                    response_text=(response.text or "")[:2000] if response is not None else None,
                    error_message=message,
                )
            raise ShipStationAPIError(message, status_code=status_code, ambiguous=ambiguous)

        waited = 0.0
        for attempt in range(retries + 1):
            try:
                response = requests.request(method, url, **kwargs)
            except requests.RequestException as exc:
                _logger.warning("ShipStation %s request error on %s: %s", label, url, exc)
                if attempt >= retries or waited + backoff > MAX_TOTAL_WAIT:
                    fail(str(exc), ambiguous=True)
                time.sleep(backoff)
                waited += backoff
                backoff = min(backoff * 2.0, 30.0)
                continue

            status = response.status_code
            if status in TRANSIENT_STATUSES:
                _logger.warning("ShipStation %s transient status %s on %s", label, status, url)
                delay = self._rate_limit_delay(response, backoff) if status == 429 else backoff
                if attempt >= retries or waited + delay > MAX_TOTAL_WAIT:
                    message = self._extract_ss_error_message(response)
                    if status == 429:
                        wait = int(self._rate_limit_delay(response, backoff))
                        message = "%s (rate limited; retry in about %s s)" % (message, wait)
                    fail("ShipStation API error %s on %s: %s" % (status, endpoint, message),
                         status_code=status, ambiguous=status != 429, response=response)
                time.sleep(delay)
                waited += delay
                backoff = min(backoff * 2.0, 30.0)
                continue

            if status >= 400:
                message = self._extract_ss_error_message(response)
                _logger.warning(
                    "ShipStation %s request failed %s %s status=%s response=%s",
                    label, method, url, status, (response.text or "")[:1000],
                )
                fail("ShipStation API error %s on %s: %s" % (status, endpoint, message),
                     status_code=status, ambiguous=status == 408, response=response)

            try:
                result = response.json() if response.text else {}
            except ValueError:
                fail("ShipStation returned an unreadable response for %s." % endpoint,
                     status_code=status, ambiguous=True, response=response)
            if log:
                self._log_sync_request(
                    "success", method, endpoint, params=params, data=data,
                    response_text=(response.text or "")[:2000],
                )
            return result

        return {}

    def _ss_request(self, method, endpoint, params=None, data=None):
        """Perform ShipStation V1 requests (HTTP Basic, ``ssapi.shipstation.com``)."""
        self.ensure_one()
        credentials = self._get_shipstation_auth_record()
        if not credentials.api_key or not credentials.api_secret:
            raise UserError(_("ShipStation API credentials are missing."))

        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        return self._http_call(
            "v1", method, f"{SHIPSTATION_V1_BASE_URL}{endpoint}", endpoint,
            auth=(credentials.api_key, credentials.api_secret), params=params, data=data,
        )

    def _ss_v2_request(self, method, endpoint, params=None, data=None):
        """Perform ShipStation v2 API requests using API-Key header auth."""
        self.ensure_one()
        credentials = self._get_shipstation_auth_record()
        if not credentials.v2_api_key:
            raise UserError(_("ShipStation V2 API key is missing. Configure it on the instance."))

        base_url = self._normalize_shipstation_base_url(self.base_url)
        if not self._is_shipstation_url(base_url) and not self._custom_base_url_allowed():
            # The V2 key is sent to this host; never let a typo or tampering redirect it.
            raise UserError(_("The ShipStation V2 base URL must be an https://*.shipstation.com address."))
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        if not endpoint.startswith("/v2/"):
            endpoint = f"/v2{endpoint}"
        return self._http_call(
            "v2", method, f"{base_url}{endpoint}", endpoint,
            headers={"API-Key": str(credentials.v2_api_key or "").strip()},
            params=params, data=data,
        )

    def _ss_legacy_products_request(self, params=None):
        """Fallback for legacy product listing when v2 products is unauthorized."""
        self.ensure_one()
        credentials = self._get_shipstation_auth_record()
        if not credentials.api_key or not credentials.api_secret:
            raise UserError(_("ShipStation API credentials are missing."))

        url = f"{SHIPSTATION_V1_BASE_URL}/products"
        retries = 1
        backoff = 1.0
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    url,
                    auth=(credentials.api_key, credentials.api_secret),
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                _logger.warning("ShipStation legacy product request error on %s: %s", url, exc)
                if attempt >= retries:
                    raise ShipStationAPIError(str(exc), ambiguous=True)
                time.sleep(backoff)
                continue

            if response.status_code >= 400:
                message = self._extract_ss_error_message(response)
                _logger.warning(
                    "ShipStation legacy product request failed %s status=%s params=%s response=%s",
                    url,
                    response.status_code,
                    params,
                    (response.text or "")[:1000],
                )
                raise ShipStationAPIError(
                    f"ShipStation API error {response.status_code} on /products: {message}",
                    status_code=response.status_code)

            return response.json() if response.text else {}

        return {}

    def _fetch_products_with_fallback(self, *, page=1, page_size=100, sku=None, product_id=None, legacy_params=None):
        """Prefer V2 when configured; preserve V1-only product synchronization."""
        self.ensure_one()
        fallback_params = dict(legacy_params or {})
        if sku:
            fallback_params["sku"] = sku
        if product_id:
            fallback_params["productId"] = product_id
        if not self._get_shipstation_auth_record().v2_api_key:
            return self._ss_legacy_products_request(params=fallback_params)
        v2_params = {"page": page, "page_size": page_size}
        if sku:
            v2_params["sku"] = sku
        if product_id:
            v2_params["product_id"] = product_id
        try:
            return self._ss_v2_request("GET", "/products", params=v2_params)
        except UserError as exc:
            message = str(exc)
            if " 401 " not in f" {message} " and " 403 " not in f" {message} " and "Access denied" not in message:
                raise
            _logger.warning(
                "ShipStation V2 product listing unauthorized; attempting legacy V1 products (if configured)."
            )
        return self._ss_legacy_products_request(params=fallback_params)

    # ------------------------------------------------------------------
    # Date / time helpers (ShipStation V1 speaks Pacific time)
    # ------------------------------------------------------------------
    @staticmethod
    def _pacific_time_is_ambiguous(value):
        """True when ``value`` (naive UTC) falls in the hour that occurs twice when DST ends.

        ShipStation V1 takes Pacific wall-clock times without an offset, so during that single
        hour a timestamp does not identify one instant.
        """
        zone = pytz.timezone(SHIPSTATION_V1_TIMEZONE)
        local = pytz.utc.localize(value).astimezone(zone)
        try:
            zone.localize(local.replace(tzinfo=None), is_dst=None)
        except pytz.exceptions.AmbiguousTimeError:
            return True
        except pytz.exceptions.NonExistentTimeError:  # not reachable from a real UTC instant
            return False
        return False

    @staticmethod
    def _remote_store_id(data):
        """The store a ShipStation V1 payload names, or "" when it names none.

        Orders carry it at ``advancedOptions.storeId`` (a top-level ``storeId`` is tolerated). Shipments
        carry no store at all: they are scoped by the ``storeId`` *query filter* of the request.
        """
        if not isinstance(data, dict):
            return ""
        value = data.get("storeId")
        if value in (None, ""):
            advanced = data.get("advancedOptions")
            value = advanced.get("storeId") if isinstance(advanced, dict) else None
        return "" if value in (None, "") else str(value).strip()

    def _attributed_store_id(self, data):
        """Store to record on a mirror: what the payload names, else the store the request was filtered by."""
        return self._remote_store_id(data) or str(self.store_id or "").strip()

    def _remote_store_matches(self, data):
        """False only when the payload NAMES a store other than the configured one.

        A payload that names no store is accepted: with a configured store the request itself was
        filtered by it (orders, shipments and webhook look-ups all send ``storeId``).
        """
        configured = str(self.store_id or "").strip()
        remote = self._remote_store_id(data)
        return not (configured and remote and remote != configured)

    def _is_only_instance_of_company(self):
        return self.env["shipstation.instance"].sudo().with_context(active_test=False).search_count(
            [("company_id", "=", self.company_id.id)]) == 1

    def _get_sync_window(self, last_sync_at, days_back=7):
        end_dt = fields.Datetime.now()
        start_dt = last_sync_at or (end_dt - timedelta(days=days_back))
        if self._pacific_time_is_ambiguous(start_dt):
            # The wall-clock label would be ambiguous (both 01:30 PDT and 01:30 PST read "01:30").
            # Starting two hours earlier lands on an unambiguous label before the repeated hour;
            # re-reading a little is harmless because every import is idempotent.
            start_dt -= timedelta(hours=2)
        return start_dt, end_dt

    def _format_ss_datetime(self, value, query=False):
        """Format an Odoo (UTC) datetime as ShipStation V1 expects: Pacific wall-clock time.

        ``query=True`` uses the documented ``yyyy-mm-dd hh:mm:ss`` form for URL parameters;
        otherwise an ISO 8601 ``T`` form is produced for JSON bodies.
        """
        if not value:
            return False
        dt = fields.Datetime.to_datetime(value)
        if not dt:
            return False
        local = pytz.utc.localize(dt).astimezone(pytz.timezone(SHIPSTATION_V1_TIMEZONE))
        return local.strftime("%Y-%m-%d %H:%M:%S" if query else "%Y-%m-%dT%H:%M:%S")

    def _format_ss_date(self, value):
        """Pacific calendar date (``yyyy-mm-dd``), as used for label ship dates."""
        formatted = self._format_ss_datetime(value)
        return formatted.split("T")[0] if formatted else False

    def _parse_ss_datetime(self, value):
        """Parse ShipStation values like ``2026-02-24T10:10:14.0000000`` (Pacific) into naive UTC."""
        if not value:
            return False
        clean = str(value).strip()
        explicit_utc = clean.endswith("Z")
        clean = clean.replace("T", " ").split(".")[0].replace("Z", "").strip()
        try:
            naive = fields.Datetime.to_datetime(clean)
        except Exception:  # noqa: BLE001
            return False
        if not naive:
            return False
        if explicit_utc:
            return naive
        local = pytz.timezone(SHIPSTATION_V1_TIMEZONE).localize(naive, is_dst=False)
        return local.astimezone(pytz.utc).replace(tzinfo=None)

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
        shared_or_company = ["|", ("company_id", "=", False), ("company_id", "=", self.company_id.id)]
        if email:
            candidates = Partner.search([("email", "=", email)] + shared_or_company, limit=2)
            if len(candidates) > 1:
                raise UserError(_("Multiple customers share the email in this company; resolve before importing."))
            partner = candidates
        if not partner:
            candidates = Partner.search(
                [("name", "=", name), ("zip", "=", zip_code)] + shared_or_company,
                limit=2,
            )
            if len(candidates) > 1:
                raise UserError(_("Ambiguous customer name/address; resolve before importing."))
            partner = candidates
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
                    "|", ("company_id", "=", False), ("company_id", "=", self.company_id.id),
                ],
                limit=2,
            )
            if len(shipping_partner) > 1:
                raise UserError(_("Ambiguous delivery address; resolve before importing."))
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
                # Odoo 19 no longer copies a first contact's address onto an address-less
                # parent on create; keep giving the customer the address it had before.
                address_fields = ("street", "street2", "city", "state_id", "zip", "country_id")
                if not any(partner[f] for f in address_fields):
                    partner.write({f: shipping_partner[f].id if f.endswith("_id") else shipping_partner[f]
                                   for f in address_fields})

        return partner, shipping_partner

    def _get_or_create_product(self, sku, name, price):
        Product = self.env["product.product"].with_company(self.company_id)
        product = Product.search([
            ("default_code", "=", sku), "|",
            ("company_id", "=", False), ("company_id", "=", self.company_id.id),
        ], limit=2) if sku else False
        if product and len(product) > 1:
            raise UserError(_("Ambiguous SKU across shared and company products; resolve before importing."))
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
        """Fail closed on ambiguous identities, even when historical data is present."""
        SaleOrder = self.env["sale.order"].with_company(self.company_id)
        company_domain = [("company_id", "=", self.company_id.id)]
        if order_id:
            domain = company_domain + [
                ("shipstation_instance_id", "=", self.id),
                ("shipstation_order_id", "=", str(order_id)),
            ]
            matches = SaleOrder.search(domain, limit=2)
            if len(matches) > 1:
                raise UserError(_("Duplicate Odoo orders for the same ShipStation instance and order ID."))
            if matches:
                if store_id and matches.shipstation_store_id and matches.shipstation_store_id != str(store_id):
                    raise UserError(_("ShipStation order has a conflicting store identity."))
                return matches

        # Carefully adopt pre-upgrade sales: exact external ID + company, and only those with no prior
        # instance ownership. The published build recorded a blank store (ShipStation orders carry none at
        # the top level), so a blank legacy store is accepted when this is the company's only connection:
        # then ownership is unambiguous and an already-imported order is never imported twice.
        if order_id:
            store_values = [str(store_id) if store_id else False]
            if store_id and self._is_only_instance_of_company():
                store_values.append(False)
            legacy = SaleOrder.search(company_domain + [
                ("shipstation_instance_id", "=", False),
                ("shipstation_order_id", "=", str(order_id)),
                ("shipstation_store_id", "in", store_values),
            ], limit=2)
            if len(legacy) > 1:
                raise UserError(_("Ambiguous legacy ShipStation order: manual reconciliation required."))
            if legacy:
                return legacy

        if order_number and store_id:
            matches = SaleOrder.search(company_domain + [
                ("shipstation_instance_id", "=", self.id),
                ("shipstation_order_number", "=", str(order_number)),
                ("shipstation_store_id", "=", str(store_id)),
            ], limit=2)
            if len(matches) > 1:
                raise UserError(_("Duplicate order numbers within this ShipStation instance/store."))
            if matches:
                if order_id and matches.shipstation_order_id and matches.shipstation_order_id != str(order_id):
                    raise UserError(_("Order number is linked to a different ShipStation order ID."))
                return matches
        return SaleOrder.browse()

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
