"""Offline, self-contained S01D contract tests (no Odoo database or network)."""
import ast
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
MODULE = ROOT / "shipstation_connector"


def load_security():
    spec = importlib.util.spec_from_file_location("s01d_webhook_security", MODULE / "services/shipstation_webhook_security.py")
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


security = load_security()


class ResourceValidation(unittest.TestCase):
    def notify(self, url="https://ssapi.shipstation.com/orders?storeId=123&page=1", t="ORDER_NOTIFY", **kw):
        return {"resource_type": t, "resource_url": url, **kw}

    def test_accepts_v1_order_url_and_store(self):
        event, path, params, digest = security.validate_v1_notification(self.notify(), "123")
        self.assertEqual((event, path), ("ORDER_NOTIFY", "/orders"))
        self.assertEqual(params["storeId"], "123")
        self.assertEqual(len(digest), 64)

    def test_accepts_shipment_url(self):
        event, path, *_ = security.validate_v1_notification(self.notify(
            "https://ssapi.shipstation.com/shipments?storeId=123", "SHIP_NOTIFY"), "123")
        self.assertEqual((event, path), ("SHIP_NOTIFY", "/shipments"))

    def test_rejects_unauthorized_origins(self):
        urls = [
            "http://ssapi.shipstation.com/orders?storeId=123",
            "https://ssapi.shipstation.com.evil.test/orders?storeId=123",
            "https://ssapi.shipstation.com@evil.test/orders?storeId=123",
            "https://127.0.0.1/orders?storeId=123",
            "https://ssapi.shipstation.com:444/orders?storeId=123",
            "https://ssapi.shipstation.com/orders/../shipments?storeId=123",
            "https://ssapi.shipstation.com/shipments?storeId=123",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(security.WebhookValidationError):
                security.validate_v1_notification(self.notify(url), "123")

    def test_numbered_v1_hosts_are_accepted_but_lookalikes_are_not(self):
        # ShipStation V1 is also served from numbered clusters (ssapi6.shipstation.com ...).
        for host in ("ssapi.shipstation.com", "ssapi6.shipstation.com", "ssapi12.shipstation.com"):
            event, path, params, _ = security.validate_v1_notification(
                self.notify("https://%s/orders?storeId=123" % host), "123")
            self.assertEqual((event, path), ("ORDER_NOTIFY", "/orders"), host)
        for host in ("ssapi1234.shipstation.com", "evilssapi.shipstation.com", "ssapi6.shipstation.com.evil.test",
                     "ssapi6.shipstation.com@evil.test", "ssapi-6.shipstation.com", "api.shipstation.com",
                     "ssapi6.shipstation.co", "xssapi6.shipstation.com"):
            with self.subTest(host=host), self.assertRaises(security.WebhookValidationError):
                security.validate_v1_notification(self.notify("https://%s/orders?storeId=123" % host), "123")

    def test_storeid_parameter_case_is_ignored(self):
        # ShipStation's own examples spell the query parameter "storeID".
        event, path, params, _ = security.validate_v1_notification(
            self.notify("https://ssapi.shipstation.com/orders?storeID=123"), "123")
        self.assertEqual(event, "ORDER_NOTIFY")
        with self.assertRaises(security.WebhookValidationError):
            security.validate_v1_notification(self.notify("https://ssapi.shipstation.com/orders?storeID=999"), "123")

    def test_rejects_store_mismatch(self):
        with self.assertRaises(security.WebhookValidationError):
            security.validate_v1_notification(self.notify(), "777")

    def test_rejects_payload_query_conflict(self):
        with self.assertRaises(security.WebhookValidationError):
            security.validate_v1_notification(self.notify(storeId="2"), "123")

    def test_rejects_unsupported_v2_event(self):
        with self.assertRaises(security.WebhookValidationError):
            security.validate_v1_notification(self.notify(t="SHIPMENT_CREATED_V2"))

    def test_rejects_malformed_and_array_body(self):
        for bad in ([self.notify()], {}, {"resource_type": "ORDER_NOTIFY"}):
            with self.subTest(bad=bad), self.assertRaises(security.WebhookValidationError):
                security.validate_v1_notification(bad)

    def test_dedupes_equivalent_resource_url_queries(self):
        a = self.notify("https://ssapi.shipstation.com/orders?page=1&storeId=123")
        b = self.notify("https://ssapi.shipstation.com/orders?storeId=123&page=1")
        self.assertEqual(security.validate_v1_notification(a)[-1],
                         security.validate_v1_notification(b)[-1])

    def test_distinct_events_get_distinct_fingerprints(self):
        a = self.notify("https://ssapi.shipstation.com/orders?page=1&storeId=123")
        b = self.notify("https://ssapi.shipstation.com/orders?page=2&storeId=123")
        self.assertNotEqual(security.validate_v1_notification(a)[-1],
                            security.validate_v1_notification(b)[-1])

    def test_rejects_url_query_bombs(self):
        value = "&".join(f"foo{i}=v" for i in range(30))
        with self.assertRaises(security.WebhookValidationError):
            security.validate_v1_notification(self.notify("https://ssapi.shipstation.com/orders?" + value))


class WiringContract(unittest.TestCase):
    def test_manifest_loads_after_model_and_menu(self):
        manifest = ast.literal_eval((MODULE / "__manifest__.py").read_text())
        data = manifest["data"]
        self.assertIn("views/shipstation_webhook_inbox_views.xml", data)
        self.assertGreater(data.index("views/shipstation_webhook_inbox_views.xml"), data.index("views/menu.xml"))

    def test_inbox_has_unique_constraint_and_worker_lock(self):
        model = (MODULE / "models/shipstation_webhook_inbox.py").read_text()
        # Odoo 17 declares table constraints with _sql_constraints (models.Constraint is Odoo 19+).
        self.assertIn("_sql_constraints", model)
        self.assertIn("UNIQUE(instance_id, fingerprint)", model)
        self.assertIn("FOR UPDATE SKIP LOCKED", model)
        self.assertIn("self.env.cr.savepoint()", model)
        self.assertIn("next_retry_at", model)
        self.assertIn("if instance.store_id", model)

    def test_old_anonymous_endpoint_is_deactivated(self):
        controller = (MODULE / "controllers/shipstation_webhook.py").read_text()
        self.assertIn('"/shipstation/webhook"', controller)
        self.assertIn('"error": "not_found"', controller)
        self.assertNotIn('self._find_instance(', controller)
        self.assertNotIn('_sync_orders(mode="webhook")', controller)
        self.assertIn('hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))', controller)

    def test_only_readonly_v1_notifications_accepted(self):
        src = (MODULE / "services/shipstation_webhook_security.py").read_text()
        self.assertNotIn("requests.get", src)
        self.assertIn('_V1_HOST.fullmatch(parsed.hostname or "")', src)
        self.assertNotIn('"SHIPMENT_CREATED_V2":', src)

    def test_cron_and_access_rules_wired(self):
        ET.parse(MODULE / "data/ir_cron.xml")
        ET.parse(MODULE / "security/shipstation_record_rules.xml")
        ET.parse(MODULE / "views/shipstation_webhook_inbox_views.xml")
        self.assertIn("model._cron_process_webhooks", (MODULE / "data/ir_cron.xml").read_text())
        self.assertIn("group_shipstation_admin,1,1,1,1", (MODULE / "security/ir.model.access.csv").read_text())
        self.assertIn("rule_shipstation_webhook_inbox_company", (MODULE / "security/shipstation_record_rules.xml").read_text())

    def test_secret_only_on_admin_form(self):
        instance = (MODULE / "models/shipstation_instance.py").read_text()
        self.assertIn('default=lambda self: secrets.token_urlsafe(32)', instance)
        self.assertIn("group_shipstation_admin", instance)
        self.assertIn("action_rotate_v1_webhook_token", instance)
        form = (MODULE / "views/shipstation_instance_view.xml").read_text()
        self.assertIn('name="webhook_target_url"', form)
        self.assertIn('name="action_rotate_v1_webhook_token"', form)

    def test_python_and_xml_syntax(self):
        for path in MODULE.rglob("*.py"):
            ast.parse(path.read_text(), filename=str(path))
        for path in MODULE.rglob("*.xml"):
            ET.parse(path)


class ControllerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Stub only the Odoo HTTP framework. No runtime imports and no network.
        odoo = types.ModuleType("odoo")
        http = types.ModuleType("odoo.http")
        http.Controller = type("Controller", (), {})
        http.route = lambda *args, **kw: lambda fn: fn
        http.request = MagicMock()
        odoo.http = http
        psy = types.ModuleType("psycopg2")
        psy.IntegrityError = type("IntegrityError", (Exception,), {})
        app = types.ModuleType("shipstation_connector")
        app.__path__ = [str(MODULE)]
        services = types.ModuleType("shipstation_connector.services")
        services.__path__ = [str(MODULE / "services")]
        controllers = types.ModuleType("shipstation_connector.controllers")
        controllers.__path__ = [str(MODULE / "controllers")]
        with patch.dict(sys.modules, {"odoo": odoo, "odoo.http": http, "psycopg2": psy,
                                      "shipstation_connector": app,
                                      "shipstation_connector.services": services,
                                      "shipstation_connector.controllers": controllers}):
            spec = importlib.util.spec_from_file_location(
                "shipstation_connector.controllers.shipstation_webhook",
                MODULE / "controllers/shipstation_webhook.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        cls.mod = mod

    def setUp(self):
        self.ctrl = self.mod.ShipStationWebhookController()
        self.req = self.mod.request
        self.req.reset_mock()
        instance = MagicMock()
        instance.active = True
        instance.id = 23
        instance.store_id = "123"
        instance.webhook_token = "x" * 43
        instance.webhook_order_create = True
        instance.webhook_order_update = False
        instance.webhook_shipment_update = True
        self.instance = instance
        inst_model = MagicMock()
        inst_model.sudo.return_value.browse.return_value.exists.return_value = instance
        self.inbox = MagicMock()
        self.inbox.search.return_value = False
        inbox_model = MagicMock()
        inbox_model.sudo.return_value = self.inbox
        self.req.env.__getitem__.side_effect = lambda key: inst_model if key == "shipstation.instance" else inbox_model
        self.req.httprequest.mimetype = "application/json"
        self.req.httprequest.content_length = 100
        self.req.httprequest.data = json.dumps({
            "resource_type": "ORDER_NOTIFY",
            "resource_url": "https://ssapi.shipstation.com/orders?storeId=123&page=1"
        }).encode()
        self.req.make_json_response.side_effect = lambda body, status: {"body":body, "code":status}

    def test_missing_secret_fails_before_payload_or_inbox(self):
        result = self.ctrl.shipstation_webhook_v1(23, "wrong-token")
        self.assertEqual(result["code"], 404)
        self.inbox.create.assert_not_called()

    def test_invalid_store_fails_before_enqueue(self):
        self.req.httprequest.data = self.req.httprequest.data.replace(b"storeId=123", b"storeId=321")
        result = self.ctrl.shipstation_webhook_v1(23, self.instance.webhook_token)
        self.assertEqual(result["code"], 422)
        self.inbox.create.assert_not_called()

    def test_repeated_event_is_acknowledged_no_enqueue(self):
        self.inbox.search.return_value = MagicMock()
        result = self.ctrl.shipstation_webhook_v1(23, self.instance.webhook_token)
        self.assertEqual(result["body"]["status"], "duplicate")
        self.inbox.create.assert_not_called()

    def test_valid_notification_enqueued_without_remote_call(self):
        self.req.env.cr.savepoint.return_value.__enter__ = MagicMock()
        self.req.env.cr.savepoint.return_value.__exit__ = MagicMock(return_value=False)
        result = self.ctrl.shipstation_webhook_v1(23, self.instance.webhook_token)
        self.assertEqual(result["code"], 202)
        self.inbox.create.assert_called_once()
        self.instance._ss_request.assert_not_called()



class WorkerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        odoo = types.ModuleType("odoo")
        fields = types.ModuleType("odoo.fields")
        models = types.ModuleType("odoo.models")
        for field in ("Many2one", "Char", "Text", "Integer", "Selection", "Datetime"):
            setattr(fields, field, lambda *args, **kwargs: None)
        models.Model = object
        models.Constraint = lambda *args, **kwargs: None
        odoo.fields, odoo.models = fields, models
        odoo.api = types.SimpleNamespace(**{_n: (lambda *a, **k: a[0] if len(a) == 1 and callable(a[0]) and not k else (lambda fn: fn)) for _n in ("model", "model_create_multi", "depends", "depends_context", "constrains", "autovacuum", "onchange", "returns")})
        odoo._ = lambda text, *args, **kwargs: text
        exceptions = types.ModuleType("odoo.exceptions")
        exceptions.UserError = type("UserError", (Exception,), {})
        exceptions.AccessError = type("AccessError", (Exception,), {})
        app = types.ModuleType("shipstation_connector")
        app.__path__ = [str(MODULE)]
        services = types.ModuleType("shipstation_connector.services")
        services.__path__ = [str(MODULE / "services")]
        model_pkg = types.ModuleType("shipstation_connector.models")
        model_pkg.__path__ = [str(MODULE / "models")]
        with patch.dict(sys.modules, {"odoo": odoo, "odoo.fields": fields,
                                      "odoo.models": models, "odoo.exceptions": exceptions,
                                      "shipstation_connector": app,
                                      "shipstation_connector.services": services,
                                      "shipstation_connector.models": model_pkg}):
            spec = importlib.util.spec_from_file_location(
                "shipstation_connector.models.shipstation_webhook_inbox",
                MODULE / "models/shipstation_webhook_inbox.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        cls.inbox_model = mod.ShipStationWebhookInbox

    def setUp(self):
        self.record = MagicMock()
        self.record.instance_id.active = True
        self.record.instance_id.webhook_token = "secret"
        self.record.instance_id.store_id = "123"
        self.record.instance_id.webhook_order_create = True
        self.record.instance_id.webhook_order_update = False
        self.record.event_type = "ORDER_NOTIFY"
        self.record.resource_url = "https://ssapi.shipstation.com/orders?storeId=123"
        self.record.payload_store_id = "123"
        # Documented V1 shape: an order names its store only at advancedOptions.storeId (or not at all).
        self.record.instance_id._ss_request.return_value = {
            "orders": [{"orderId": 1, "advancedOptions": {"storeId": 123}},
                       {"orderId": 2, "advancedOptions": {"storeId": 999}},
                       {"orderId": 3, "advancedOptions": None}],
            "pages": 1,
        }

        def matches(item):      # the production rule (ShipStationSyncBase._remote_store_matches)
            named = str(item.get("storeId") or (item.get("advancedOptions") or {}).get("storeId") or "")
            return not named or named == "123"
        self.record.instance_id._remote_store_matches.side_effect = matches

    def test_worker_fetches_pinned_endpoint_only_and_filters_store(self):
        self.inbox_model._process_v1_notification(self.record)
        self.record.instance_id._ss_request.assert_called_once_with(
            "GET", "/orders", params={"storeId": "123", "page": 1, "pageSize": 100})
        service = self.record.env["shipstation.order.sync"]
        self.assertEqual([c.args[1]["orderId"] for c in service._upsert_from_payload.call_args_list], [1, 3],
                         "an order naming another store is skipped; one naming none was scoped by the request filter")

    def test_worker_rejects_disabled_order_notifications(self):
        self.record.instance_id.webhook_order_create = False
        with self.assertRaises(ValueError):
            self.inbox_model._process_v1_notification(self.record)
        self.record.instance_id._ss_request.assert_not_called()

    def test_worker_fails_on_unexpected_response(self):
        self.record.instance_id._ss_request.return_value = {"wrong": []}
        with self.assertRaises(ValueError):
            self.inbox_model._process_v1_notification(self.record)

    def test_worker_rejects_collection_over_max_pages(self):
        self.record.instance_id._ss_request.return_value = {"orders": [], "pages": 5}
        with self.assertRaises(ValueError):
            self.inbox_model._process_v1_notification(self.record)

    def test_worker_uses_shipment_model_for_shipment_event(self):
        self.record.event_type = "SHIP_NOTIFY"
        self.record.resource_url = "https://ssapi.shipstation.com/shipments?storeId=123"
        self.record.instance_id.webhook_shipment_update = True
        self.record.instance_id._ss_request.return_value = {
            "shipments": [{"shipmentId": 99, "storeId": 123}], "pages": 1,
        }
        self.inbox_model._process_v1_notification(self.record)
        self.record.env["shipstation.shipment.sync"]._upsert_from_payload.assert_called_once()


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
