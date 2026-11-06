"""Pure validation helpers for legacy ShipStation V1 webhook notifications.

ShipStation V1 does not document signed notifications. We therefore authenticate
an account-specific high-entropy callback URL and pin resource URLs to the V1
host, while keeping the untrusted supplied URL out of HTTP request execution.
"""
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit


# ShipStation serves V1 from numbered clusters too (ssapi.shipstation.com, ssapi6.shipstation.com ...).
# The supplied URL is never fetched - requests are rebuilt against the fixed V1 host - so accepting
# the numbered hosts only avoids rejecting genuine notifications.
_V1_HOST = re.compile(r"ssapi[0-9]{0,3}\.shipstation\.com")

V1_WEBHOOK_TARGETS = {
    "ORDER_NOTIFY": ("/orders", "orders"),
    "ITEM_ORDER_NOTIFY": ("/orders", "orders"),
    "SHIP_NOTIFY": ("/shipments", "shipments"),
    "ITEM_SHIP_NOTIFY": ("/shipments", "shipments"),
}


class WebhookValidationError(ValueError):
    pass


def validate_v1_notification(payload, expected_store_id=None):
    """Return (event_type, endpoint, params, dedupe_hash); never fetch URL.

    No fallback to a default instance is permitted. The calling controller
    authenticates the instance using its callback secret first.
    """
    if not isinstance(payload, dict):
        raise WebhookValidationError("Expected a single JSON object")
    event_type = payload.get("resource_type")
    if not isinstance(event_type, str) or event_type not in V1_WEBHOOK_TARGETS:
        raise WebhookValidationError("Unsupported V1 webhook event")
    resource_url = payload.get("resource_url")
    if not isinstance(resource_url, str) or not resource_url or len(resource_url) > 2048:
        raise WebhookValidationError("Missing or oversized resource_url")
    try:
        parsed = urlsplit(resource_url)
        port = parsed.port  # raises ValueError on malformed ports
    except ValueError as exc:
        raise WebhookValidationError("Malformed resource_url") from exc
    if (parsed.scheme != "https" or not _V1_HOST.fullmatch(parsed.hostname or "")
            or parsed.username or parsed.password or port not in (None, 443)
            or parsed.fragment):
        raise WebhookValidationError("Untrusted resource_url origin")
    endpoint, _key = V1_WEBHOOK_TARGETS[event_type]
    if parsed.path != endpoint:
        raise WebhookValidationError("Event type and resource path mismatch")
    try:
        query = parse_qsl(parsed.query, keep_blank_values=False, strict_parsing=True,
                          max_num_fields=20)
    except ValueError as exc:
        raise WebhookValidationError("Invalid resource_url query") from exc
    params = {}
    for key, val in query:
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,49}", key)
                or len(val) > 200 or key in params):
            raise WebhookValidationError("Invalid or duplicate resource_url parameter")
        params[key] = val
    payload_store = str(payload.get("storeId") or "").strip()
    # ShipStation's own notifications spell the parameter "storeID"; accept any capitalisation.
    query_store = next((str(v).strip() for k, v in params.items() if k.lower() == "storeid"), "")
    expected = str(expected_store_id or "").strip()
    if payload_store and query_store and payload_store != query_store:
        raise WebhookValidationError("Conflicting store identifiers")
    if expected and any(s != expected for s in (payload_store, query_store) if s):
        raise WebhookValidationError("Notification belongs to another store")
    # Same meaningful notification receives the same idempotency key even if
    # JSON serialization, whitespace, or URL parameter order differs.
    canonical = json.dumps([event_type, endpoint, sorted(params.items()), payload_store],
                           separators=(",", ":"), ensure_ascii=True)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return event_type, endpoint, params, fingerprint
