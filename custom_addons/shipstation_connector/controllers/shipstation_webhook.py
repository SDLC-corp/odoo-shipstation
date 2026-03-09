import json
import logging
from urllib.parse import parse_qs, urlparse

from odoo import http
from odoo.http import request


_logger = logging.getLogger(__name__)


class ShipStationWebhookController(http.Controller):
    def _parse_payload(self):
        payload = {}
        raw = request.httprequest.data or b""
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = {}
        if not payload:
            payload = request.params or {}

        # Some providers send nested JSON in "payload" field.
        if isinstance(payload, dict):
            nested = payload.get("payload")
            if isinstance(nested, str):
                try:
                    parsed = json.loads(nested)
                    if isinstance(parsed, (dict, list)):
                        payload = parsed
                except Exception:
                    pass
        return payload

    def _extract_events(self, payload):
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            if isinstance(payload.get("events"), list):
                return [p for p in payload.get("events") if isinstance(p, dict)]
            if isinstance(payload.get("data"), list):
                return [p for p in payload.get("data") if isinstance(p, dict)]
            return [payload]
        return []

    def _as_int(self, value):
        text = str(value or "").strip()
        return int(text) if text.isdigit() else False

    def _from_resource_url(self, event):
        resource_url = str(event.get("resource_url") or event.get("resourceUrl") or "").strip()
        if not resource_url:
            return {}
        try:
            parsed = urlparse(resource_url)
            query = parse_qs(parsed.query or "")
        except Exception:
            return {}
        result = {}
        for key in ("orderId", "orderNumber", "shipmentId", "storeId", "productId", "sku", "customerId", "email"):
            val = query.get(key)
            if val and val[0]:
                result[key] = val[0]
        return result

    def _find_instance(self, env, event):
        store_id = str(event.get("storeId") or "").strip()
        domain = [("active", "=", True)]
        if store_id:
            by_store = env["shipstation.instance"].search(domain + [("store_id", "=", store_id)], limit=1)
            if by_store:
                return by_store
        return env["shipstation.instance"].search(domain, limit=1)

    def _event_allows_processing(self, instance, event):
        # Backward compatibility: if webhook toggles are all disabled/not configured,
        # keep processing enabled to avoid silently dropping events.
        if not any([instance.webhook_order_create, instance.webhook_order_update, instance.webhook_shipment_update]):
            return True

        event_name = str(event.get("event") or event.get("eventName") or event.get("action") or "").lower()
        resource_type = str(event.get("resource_type") or event.get("resourceType") or "").lower()
        text = f"{event_name} {resource_type}"

        if "order" in text and ("create" in text or "new" in text) and not instance.webhook_order_create:
            return False
        if "order" in text and ("update" in text or "modify" in text) and not instance.webhook_order_update:
            return False
        if "shipment" in text and not instance.webhook_shipment_update:
            return False
        return True

    def _upsert_order_from_event(self, env, instance, event):
        params = {}
        order_id = self._as_int(event.get("orderId"))
        order_number = str(event.get("orderNumber") or "").strip()
        store_id = self._as_int(event.get("storeId") or instance.store_id)
        if order_id:
            params["orderId"] = order_id
        elif order_number:
            params["orderNumber"] = order_number
        if store_id:
            params["storeId"] = store_id
        if not params:
            return False
        data = instance._ss_request("GET", "/orders", params=params)
        orders = (data or {}).get("orders") or []
        if not orders:
            return False
        env["shipstation.order.sync"]._upsert_from_payload(instance, orders[0])
        return True

    def _upsert_shipment_from_event(self, env, instance, event):
        params = {}
        shipment_id = self._as_int(event.get("shipmentId"))
        store_id = self._as_int(event.get("storeId") or instance.store_id)
        if shipment_id:
            params["shipmentId"] = shipment_id
        order_id = self._as_int(event.get("orderId"))
        if order_id and not params:
            params["orderId"] = order_id
        if store_id:
            params["storeId"] = store_id
        if not params:
            return False
        data = instance._ss_request("GET", "/shipments", params=params)
        shipments = (data or {}).get("shipments") or []
        if not shipments:
            return False
        env["shipstation.shipment.sync"]._upsert_from_payload(instance, shipments[0])
        return True

    def _upsert_product_from_event(self, env, instance, event):
        params = {}
        product_id = event.get("productId")
        sku = str(event.get("sku") or "").strip()
        if product_id:
            params["productId"] = self._as_int(product_id) or str(product_id).strip()
        elif sku:
            params["sku"] = sku
        if not params:
            return False
        data = instance._ss_request("GET", "/products", params=params)
        products = (data or {}).get("products") or []
        if not products:
            return False
        product_data = products[0]
        env["shipstation.product.sync"]._upsert_from_payload(instance, product_data)
        if "shipstation.inventory" in env:
            env["shipstation.inventory"]._upsert_from_payload(instance, product_data)
        return True

    def _upsert_customer_from_event(self, env, instance, event):
        customer_id = self._as_int(event.get("customerId"))
        email = str(event.get("email") or event.get("customerEmail") or "").strip()
        customer_data = {}
        if customer_id:
            customer_data = instance._ss_request("GET", f"/customers/{customer_id}")
        elif email:
            data = instance._ss_request("GET", "/customers", params={"email": email})
            customer_data = ((data or {}).get("customers") or [{}])[0]
        if not customer_data:
            return False
        env["shipstation.customer.sync"]._upsert_from_payload(instance, customer_data)
        return True

    def _detect_targets(self, event):
        text = " ".join(
            [
                str(event.get("event") or ""),
                str(event.get("eventName") or ""),
                str(event.get("action") or ""),
                str(event.get("resource_type") or event.get("resourceType") or ""),
                str(event.get("resource_url") or event.get("resourceUrl") or ""),
            ]
        ).lower()
        has_order = bool(event.get("orderId") or event.get("orderNumber") or "order" in text)
        has_shipment = bool(event.get("shipmentId") or "shipment" in text or "tracking" in text)
        has_product = bool(event.get("productId") or event.get("sku") or "product" in text or "inventory" in text)
        has_customer = bool(event.get("customerId") or event.get("email") or event.get("customerEmail") or "customer" in text)
        return has_order, has_shipment, has_product, has_customer

    @http.route("/shipstation/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def shipstation_webhook(self, **kwargs):
        env = request.env.sudo()
        payload = self._parse_payload()
        events = self._extract_events(payload)

        if not events:
            _logger.warning("ShipStation webhook received empty/invalid payload: %s", payload)
            return request.make_json_response({"status": "ignored", "reason": "empty_payload"})

        processed = 0
        failed = 0
        skipped = 0

        for base_event in events:
            event = dict(base_event or {})
            event.update(self._from_resource_url(event))

            instance = self._find_instance(env, event)
            if not instance:
                skipped += 1
                _logger.warning("ShipStation webhook event skipped: no active instance. event=%s", event)
                continue

            if not self._event_allows_processing(instance, event):
                skipped += 1
                continue

            reference = event.get("orderNumber") or event.get("shipmentId") or event.get("sku") or event.get("customerId")
            has_order, has_shipment, has_product, has_customer = self._detect_targets(event)

            try:
                did_any = False
                if has_order:
                    did_any = self._upsert_order_from_event(env, instance, event) or did_any
                if has_shipment:
                    did_any = self._upsert_shipment_from_event(env, instance, event) or did_any
                if has_product:
                    did_any = self._upsert_product_from_event(env, instance, event) or did_any
                if has_customer:
                    did_any = self._upsert_customer_from_event(env, instance, event) or did_any

                # Fallback: if event type is unclear, keep data fresh using targeted sync windows.
                if not did_any:
                    text = str(event).lower()
                    if "order" in text:
                        instance._sync_orders(mode="webhook")
                        did_any = True
                    if "shipment" in text or "tracking" in text:
                        instance._sync_shipments(mode="webhook")
                        did_any = True
                    if "product" in text or "inventory" in text:
                        instance._sync_products(mode="webhook")
                        instance._sync_inventory(mode="webhook")
                        did_any = True
                    if "customer" in text:
                        instance._sync_customers(mode="webhook")
                        did_any = True

                env["shipstation.report"].create(
                    {
                        "instance_id": instance.id,
                        "operation": "Webhook",
                        "status": "success",
                        "message": json.dumps(event, ensure_ascii=True)[:2000],
                        "mode": "webhook",
                        "reference": reference,
                    }
                )
                processed += 1
            except Exception as exc:
                failed += 1
                _logger.exception("ShipStation webhook processing failed: %s | event=%s", exc, event)
                env["shipstation.report"].create(
                    {
                        "instance_id": instance.id,
                        "operation": "Webhook",
                        "status": "failed",
                        "message": str(exc)[:2000],
                        "mode": "webhook",
                        "reference": reference,
                    }
                )

        status = "ok" if failed == 0 else "partial"
        return request.make_json_response(
            {
                "status": status,
                "events_received": len(events),
                "processed": processed,
                "failed": failed,
                "skipped": skipped,
            }
        )
