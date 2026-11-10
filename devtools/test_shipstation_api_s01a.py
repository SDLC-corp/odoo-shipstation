"""Offline contract tests for S01A. Runs WITHOUT Odoo or ShipStation credentials."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


def load_source(module_path: pathlib.Path):
    """Supply only the tiny odoo import surface needed for URL tests."""
    odoo = types.ModuleType("odoo")
    odoo.models = types.SimpleNamespace(AbstractModel=object)
    odoo.fields = types.SimpleNamespace()
    odoo._ = lambda value: value
    exceptions = types.ModuleType("odoo.exceptions")
    exceptions.UserError = type("UserError", (Exception,), {})
    exceptions.AccessError = type("AccessError", (Exception,), {})
    requests = types.ModuleType("requests")
    requests.RequestException = type("RequestException", (Exception,), {})
    with patch.dict(sys.modules, {"odoo": odoo, "odoo.exceptions": exceptions, "requests": requests}):
        spec = importlib.util.spec_from_file_location(
            "shipstation_sync_base_s01a", module_path / "models" / "shipstation_sync_base.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class ApiRoutingContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_source(pathlib.Path(cls.module_dir).resolve())

    def setUp(self):
        class FakeInstance(self.mod.ShipStationSyncBase):
            _name = "not.shipstation.instance"  # disables DB logging in this isolated test
            api_key = "dummy-v1-key"
            api_secret = "dummy-v1-secret"
            v2_api_key = "dummy-v2-key"
            base_url = "https://api.shipstation.com"

            def ensure_one(self):
                return None

            def _get_shipstation_auth_record(self):
                return self

        self.instance = FakeInstance()
        self.response = Mock(status_code=200, text='{"ok": true}')
        self.response.json.return_value = {"ok": True}

    def test_v1_order_get_uses_v1_host_and_basic_auth(self):
        with patch.object(self.mod.requests, "request", return_value=self.response, create=True) as send:
            self.instance._ss_request("GET", "/orders", params={"page": 1})
        args, kwargs = send.call_args
        self.assertEqual(args[:2], ("GET", "https://ssapi.shipstation.com/orders"))
        self.assertEqual(kwargs["auth"], ("dummy-v1-key", "dummy-v1-secret"))
        self.assertNotIn("headers", kwargs)

    def test_v1_post_never_uses_v2_host(self):
        with patch.object(self.mod.requests, "request", return_value=self.response, create=True) as send:
            self.instance._ss_request("POST", "/orders/createorder", data={"orderNumber": "DEMO"})
        self.assertEqual(send.call_args.args[1], "https://ssapi.shipstation.com/orders/createorder")

    def test_v2_product_get_uses_v2_host_and_header(self):
        with patch.object(self.mod.requests, "request", return_value=self.response, create=True) as send:
            self.instance._ss_v2_request("GET", "/products", params={"page": 1})
        args, kwargs = send.call_args
        self.assertEqual(args[:2], ("GET", "https://api.shipstation.com/v2/products"))
        self.assertEqual(kwargs["headers"], {"API-Key": "dummy-v2-key"})
        self.assertNotIn("auth", kwargs)

    def test_fallback_v1_products_uses_v1_host(self):
        with patch.object(self.mod.requests, "get", return_value=self.response, create=True) as send:
            self.instance._ss_legacy_products_request(params={"page": 1})
        args, kwargs = send.call_args
        self.assertEqual(args[0], "https://ssapi.shipstation.com/products")
        self.assertEqual(kwargs["auth"], ("dummy-v1-key", "dummy-v1-secret"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python test_shipstation_api_s01a.py <module-directory>")
    ApiRoutingContract.module_dir = sys.argv[1]
    sys.argv[:] = [sys.argv[0]]
    unittest.main(verbosity=2)
