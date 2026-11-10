"""Offline contract tests for independent ShipStation V1/V2 credentials.

Usage: python -B test_shipstation_api_s01b.py <addon-directory>
No Odoo server, database, credentials or network required.
"""
from __future__ import annotations
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class FieldConstructor:
    def __call__(self, *args, **kwargs):
        return None
    def now(self):
        return None


class FieldNamespace:
    def __getattr__(self, name):
        return FieldConstructor()


def load_modules(module):
    fake_odoo = types.ModuleType("odoo")
    fake_odoo.fields = FieldNamespace()
    fake_odoo.models = types.SimpleNamespace(Model=object, AbstractModel=object)
    fake_odoo._ = lambda value: value
    fake_odoo.api = types.SimpleNamespace(**{_n: (lambda *a, **k: a[0] if len(a) == 1 and callable(a[0]) and not k else (lambda fn: fn)) for _n in ("model", "model_create_multi", "depends", "depends_context", "constrains", "autovacuum", "onchange", "returns")})
    fake_exc = types.ModuleType("odoo.exceptions")
    fake_exc.UserError = type("UserError", (Exception,), {})
    fake_exc.AccessError = type("AccessError", (Exception,), {})
    fake_exc.ValidationError = type("ValidationError", (Exception,), {})
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = type("RequestException", (Exception,), {})
    with patch.dict(sys.modules, {"odoo": fake_odoo, "odoo.exceptions": fake_exc,
                                  "requests": fake_requests}):
        def read(name, relative):
            spec = importlib.util.spec_from_file_location(name, module / relative)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        return (read("s01b_base", "models/shipstation_sync_base.py"),
                read("s01b_instance", "models/shipstation_instance.py"),
                read("s01b_inventory", "models/shipstation_inventory.py"))


class CredentialContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.instance_mod, cls.inventory_mod = load_modules(Path(cls.module_dir))

    def setUp(self):
        parent = self.instance_mod.ShipStationInstance
        base = self.base.ShipStationSyncBase
        class FakeInstance(parent, base):
            _name = "s01b.no_database"
            api_key = "TEST_V1_KEY"
            api_secret = "TEST_V1_SECRET"
            v2_api_key = "TEST_V2_KEY"
            base_url = "https://api.shipstation.com"
            def ensure_one(self):
                pass

            def _get_shipstation_auth_record(self):
                return self
            def _require_shipstation_admin(self):
                return None
        self.instance = FakeInstance()
        self.response = Mock(status_code=200, text='{"products": [], "stores": []}')
        self.response.json.return_value = {"products": [], "stores": []}

    def test_v1_connection_checks_legacy_endpoint_using_basic_auth(self):
        with patch.object(self.base.requests, "request", return_value=self.response, create=True) as send:
            notice = self.instance.action_test_v1_connection()
        self.assertEqual(send.call_args.args[:2], ("GET", "https://ssapi.shipstation.com/stores"))
        self.assertEqual(send.call_args.kwargs["auth"], ("TEST_V1_KEY", "TEST_V1_SECRET"))
        self.assertNotIn("headers", send.call_args.kwargs)
        self.assertIn("V1", notice["params"]["title"])

    def test_v2_connection_checks_carriers_using_separate_key(self):
        with patch.object(self.base.requests, "request", return_value=self.response, create=True) as send:
            notice = self.instance.action_test_v2_connection()
        self.assertEqual(send.call_args.args[:2], ("GET", "https://api.shipstation.com/v2/carriers"))
        self.assertEqual(send.call_args.kwargs["headers"], {"API-Key": "TEST_V2_KEY"})
        self.assertNotIn("auth", send.call_args.kwargs)
        self.assertIn("V2", notice["params"]["title"])

    def test_legacy_action_still_checks_v1(self):
        with patch.object(self.base.requests, "request", return_value=self.response, create=True) as send:
            self.instance.action_test_connection()
        self.assertEqual(send.call_args.args[1], "https://ssapi.shipstation.com/stores")

    def test_v2_without_key_fails_without_http_request(self):
        self.instance.v2_api_key = None
        with patch.object(self.base.requests, "request", create=True) as send:
            with self.assertRaises(self.base.UserError if hasattr(self.base, "UserError") else Exception) as caught:
                self.instance.action_test_v2_connection()
        self.assertIn("V2 API key is missing", str(caught.exception))
        send.assert_not_called()

    def test_v1_only_products_fall_back_without_v2_call(self):
        self.instance.v2_api_key = None
        with patch.object(self.base.requests, "get", return_value=self.response, create=True) as get, \
             patch.object(self.base.requests, "request", create=True) as request:
            result = self.instance._fetch_products_with_fallback(sku="SKU-1")
        self.assertEqual(result, {"products": [], "stores": []})
        self.assertEqual(get.call_args.args[0], "https://ssapi.shipstation.com/products")
        self.assertEqual(get.call_args.kwargs["auth"], ("TEST_V1_KEY", "TEST_V1_SECRET"))
        self.assertEqual(get.call_args.kwargs["params"]["sku"], "SKU-1")
        request.assert_not_called()

    def test_invalid_v2_key_does_not_fake_success(self):
        bad = Mock(status_code=401, text='{"message": "Unauthorized"}')
        bad.json.return_value = {"message": "Unauthorized"}
        with patch.object(self.base.requests, "request", return_value=bad, create=True):
            with self.assertRaises(Exception) as caught:
                self.instance.action_test_v2_connection()
        self.assertIn("401", str(caught.exception))

    def test_inventory_v2_product_lookup_uses_v2_key(self):
        inventory = self.inventory_mod.ShipStationInventory()
        with patch.object(self.inventory_mod.requests, "get", return_value=self.response, create=True) as get:
            inventory._fetch_v2_product_payload(self.instance, "SKU-2")
        self.assertEqual(get.call_args.kwargs["headers"], {"api-key": "TEST_V2_KEY"})
        self.assertEqual(get.call_args.args[0], "https://api.shipstation.com/v2/products")

    def test_inventory_v2_stock_lookup_uses_v2_key(self):
        inventory = self.inventory_mod.ShipStationInventory()
        with patch.object(self.inventory_mod.requests, "get", return_value=self.response, create=True) as get:
            inventory._fetch_v2_inventory_payload(self.instance, "SKU-2")
        self.assertEqual(get.call_args.kwargs["headers"], {"API-Key": "TEST_V2_KEY"})
        self.assertEqual(get.call_args.args[0], "https://api.shipstation.com/v2/inventory")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -B test_shipstation_api_s01b.py <addon-directory>")
    CredentialContract.module_dir = sys.argv[1]
    sys.argv[:] = [sys.argv[0]]
    unittest.main(verbosity=2)
