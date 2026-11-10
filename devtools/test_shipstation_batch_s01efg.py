"""S01E–G source and offline behavior tests. No Odoo DB, API keys, HTTP, or stock writes.

Usage: python -B test_shipstation_batch_s01efg.py <git-root>
"""
import ast
import importlib.util
from pathlib import Path
import re
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import xml.etree.ElementTree as ET


PROJECT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
MODULE = PROJECT / "shipstation_connector"
sys.argv[:] = [sys.argv[0]]


def source(relative):
    return (MODULE / relative).read_text(encoding="utf-8")


def load_python_module(path, name, extra=None):
    class FieldConstructor:
        def __call__(self, *args, **kwargs): return None
    class FieldNamespace:
        def __getattr__(self, key):
            if key == "Datetime":
                fn = FieldConstructor()
                fn.now = lambda: "2026-11-05"
                return fn
            return FieldConstructor()
    fields = FieldNamespace()
    fake_odoo = types.ModuleType("odoo")
    fake_odoo.api = SimpleNamespace(**{_n: (lambda *a, **k: a[0] if len(a) == 1 and callable(a[0]) and not k else (lambda fn: fn)) for _n in ("model", "model_create_multi", "depends", "depends_context", "constrains", "autovacuum", "onchange", "returns")})
    fake_odoo.fields = fields
    fake_odoo.models = SimpleNamespace(Model=object, Index=lambda *a, **k: None,
                                       Constraint=lambda *a, **k: None)
    fake_odoo._ = lambda text: text
    fake_exc = types.ModuleType("odoo.exceptions")
    fake_exc.UserError = type("UserError", (Exception,), {})
    fake_exc.AccessError = type("AccessError", (Exception,), {})
    fake_exc.ValidationError = type("ValidationError", (Exception,), {})
    fake_tools = types.ModuleType("odoo.tools")
    fake_sql = types.ModuleType("odoo.tools.sql")
    fake_sql.create_index = lambda *a, **k: None   # 18.0 creates the create_date index in init()
    fake_tools.sql = fake_sql
    fake_odoo.tools = fake_tools
    modules = {"odoo": fake_odoo, "odoo.exceptions": fake_exc, "odoo.tools": fake_tools, "odoo.tools.sql": fake_sql}
    if extra:
        modules.update(extra)
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class SourceSafetyTests(unittest.TestCase):
    def test_legacy_auto_stock_toggle_has_no_effect_on_writes(self):
        t = source("models/shipstation_instance.py")
        method = t.split("    def _sync_inventory(self, mode=\"manual\"):", 1)[1].split("    def _sync_categories(", 1)[0]
        self.assertNotIn("_update_available_quantity", method)
        self.assertNotIn("inventory_update_odoo_stock and", method)
        self.assertNotIn("stock.quant", method.split("        while True:", 1)[1])
        self.assertIn("_snapshot_odoo_inventory", method)
        self.assertIn('status="failed" if failed else "success"', method)

    def test_inventory_snapshot_is_read_only(self):
        t = source("models/shipstation_inventory.py")
        method = t.split("    def _snapshot_odoo_inventory(self, instance):", 1)[1].split("    def action_refresh_inventory", 1)[0]
        self.assertIn("_get_available_quantity", method)
        self.assertNotIn("_update_available_quantity", method)
        self.assertNotIn("action_apply_inventory", method)
        self.assertNotIn("_set_inventory_quantity", method)
        self.assertIn("ambiguous_sku", method)
        self.assertIn("remote_stock_verified", method)
        self.assertIn("warehouse.company_id.id != instance.company_id.id", method)

    def test_unverified_remote_stock_not_reported_as_zero(self):
        t = source("models/shipstation_inventory.py")
        self.assertIn('"remote_stock_verified": bool(verified)', t)
        self.assertIn('return float(existing.stock_level or 0.0), {}, final_issue or "unresolved"', t)
        dash = source("models/shipstation_dashboard.py")
        self.assertIn("inv.remote_stock_verified IS TRUE", dash)
        self.assertIn('("remote_stock_verified", "=", True)', dash)

    def test_inventory_sync_actions_have_server_authorization(self):
        code = source("models/shipstation_inventory.py")
        self.assertIn("def _require_inventory_sync_admin(self):", code)
        self.assertIn('group_shipstation_admin', code)
        for name in ("action_refresh_inventory", "action_update_inventory"):
            method = code.split(f"    def {name}(self):", 1)[1].split("    def ", 1)[0]
            self.assertIn("self._require_inventory_sync_admin()", method)

    def test_inventory_view_shows_comparison(self):
        doc = ET.parse(MODULE / "views/shipstation_inventory_view.xml")
        names = [f.get("name") for f in doc.iter("field")]
        for name in ("remote_stock_verified", "odoo_stock_level", "stock_difference", "reconciliation_state"):
            self.assertIn(name, names)
        buttons = [b for b in doc.iter("button")]
        for button in buttons:
            self.assertEqual(button.get("groups"), "shipstation_connector.group_shipstation_admin")

    def test_old_stock_toggle_not_in_settings_view(self):
        t = source("views/shipstation_instance_view.xml")
        self.assertNotIn('name="inventory_update_odoo_stock"', t)
        self.assertIn('name="inventory_warehouse_id"', t)

    def test_dashboard_sql_compartmentalized(self):
        t = source("models/shipstation_dashboard.py")
        self.assertIn("WHERE si.company_id = %s", t)
        self.assertIn("WHERE instance_id = ANY(%s)", t)
        self.assertIn("AND si.company_id = %s", t)
        self.assertIn("WHERE si.company_id = %s\n                ORDER BY l.create_date", t)
        self.assertNotIn("WHERE l.status = 'failed'\n                ORDER BY", t)

    def test_ai_company_isolation_in_model_and_rule(self):
        t = source("models/shipstation_ai_insight.py")
        self.assertIn('company_id = fields.Many2one(', t)
        self.assertIn('("company_id", "=", values.get("company_id") or self.env.company.id)', t)
        rules = ET.parse(MODULE / "security/shipstation_record_rules.xml")
        ids = {r.get("id"): r for r in rules.iter("record")}
        rule = ids["rule_shipstation_ai_insight_company"]
        self.assertIn("('company_id', 'in', company_ids)", ET.tostring(rule, encoding="unicode"))

    def test_dashboard_does_not_sum_mixed_currency(self):
        t = source("models/shipstation_dashboard.py")
        self.assertIn("revenue_currencies", t)
        self.assertIn('summary["revenue_available"]', t)
        view = source("static/src/xml/shipstation_dashboard_templates.xml")
        self.assertNotIn('>$<t t-esc="formatMoney(', view)
        self.assertIn("Mixed / unknown currency", view)

    def test_raw_retry_only_get_and_known_paths(self):
        method = source("models/shipstation_sync_log.py").split("    def action_retry_sync(self):", 1)[1]
        self.assertIn('self.method.upper() != "GET"', method)
        self.assertIn('"shipments", "products", "customers", "stores"', method)
        self.assertIn('path.startswith("//")', method)

    def test_python_and_xml_syntax(self):
        for file in MODULE.rglob("*.py"):
            ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for file in MODULE.rglob("*.xml"):
            ET.parse(file)


class InventoryRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the proven offline Odoo model loader from S01B.
        file = PROJECT / "devtools/test_shipstation_api_s01b.py"
        spec = importlib.util.spec_from_file_location("s01efg_testb", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls._base, cls._instance, cls._inventory = module.load_modules(MODULE)
        cls.UserError = cls._inventory.UserError

    def make_inventory(self, verified=True, sku="ABC", qty=9):
        cls = self._inventory.ShipStationInventory
        obj = cls()
        obj.sku = sku
        obj.stock_level = qty
        obj.remote_stock_verified = verified
        obj.ensure_one = lambda: None
        obj.check_access = Mock()
        obj.write = Mock()
        return obj

    def make_instance(self, company_id=1, warehouse_company_id=1, warehouse=True):
        c = SimpleNamespace(id=company_id)
        w = SimpleNamespace(company_id=SimpleNamespace(id=warehouse_company_id), lot_stock_id="LOC") if warehouse else None
        return SimpleNamespace(company_id=c, inventory_warehouse_id=w, ensure_one=lambda: None, check_access=Mock())

    def setup_models(self, inv, products):
        prod_model = Mock()
        prod_model.with_company.return_value.search.return_value = products
        quant = Mock()
        quant.sudo.return_value.with_company.return_value._get_available_quantity.return_value = 6.0
        inv.env = {"product.product": prod_model, "stock.quant": quant}
        return prod_model, quant

    def test_verified_quantity_creates_difference_snapshot(self):
        inv = self.make_inventory()
        product, quant = self.setup_models(inv, ["PRODUCT"])
        state = inv._snapshot_odoo_inventory(self.make_instance())
        self.assertEqual(state, "difference")
        vals = inv.write.call_args.args[0]
        self.assertEqual(vals["odoo_stock_level"], 6.0)
        self.assertEqual(vals["stock_difference"], 3.0)
        quant._update_available_quantity.assert_not_called()
        self.assertEqual(product.with_company.return_value.search.call_args.kwargs["limit"], 2)

    def test_unverified_remote_qty_does_not_touch_quant(self):
        inv = self.make_inventory(verified=False, qty=0)
        _, quant = self.setup_models(inv, ["PRODUCT"])
        self.assertEqual(inv._snapshot_odoo_inventory(self.make_instance()), "unverified")
        quant.sudo.assert_not_called()

    def test_missing_warehouse_skips_product_lookup(self):
        inv = self.make_inventory()
        product, quant = self.setup_models(inv, ["PRODUCT"])
        self.assertEqual(inv._snapshot_odoo_inventory(self.make_instance(warehouse=False)), "no_warehouse")
        product.with_company.assert_not_called()
        quant.sudo.assert_not_called()

    def test_cross_company_warehouse_fails_before_stock_lookup(self):
        inv = self.make_inventory()
        product, quant = self.setup_models(inv, ["PRODUCT"])
        with self.assertRaises(self.UserError):
            inv._snapshot_odoo_inventory(self.make_instance(warehouse_company_id=2))
        product.with_company.assert_not_called()
        quant.sudo.assert_not_called()

    def test_ambiguous_sku_never_chooses_first_product(self):
        inv = self.make_inventory()
        _, quant = self.setup_models(inv, ["P1", "P2"])
        self.assertEqual(inv._snapshot_odoo_inventory(self.make_instance()), "ambiguous_sku")
        quant.sudo.assert_not_called()

    def test_missing_sku_is_explicit(self):
        inv = self.make_inventory(sku="")
        _, quant = self.setup_models(inv, ["P1"])
        self.assertEqual(inv._snapshot_odoo_inventory(self.make_instance()), "missing_sku")
        quant.sudo.assert_not_called()

    def test_zero_remote_quantity_can_be_verified(self):
        cls = self._inventory.ShipStationInventory
        obj = cls()
        obj.search = lambda *a, **k: False
        obj.create = lambda vals: SimpleNamespace(id=7, stock_status="out_of_stock", **vals)
        obj.resolve_stock_level = lambda *a: (0.0, {}, False)
        obj._extract_stock_candidates = lambda *a: {}
        obj._parse_ss_datetime = lambda *a: None
        instance = SimpleNamespace(id=17, _parse_ss_datetime=lambda s: None)
        vals = obj._upsert_from_payload(instance, {"sku": "ZERO", "name": "zero"})
        self.assertTrue(vals.remote_stock_verified)
        self.assertEqual(vals.stock_level, 0.0)

    def test_missing_remote_quantity_is_not_verified_zero(self):
        cls = self._inventory.ShipStationInventory
        obj = cls()
        obj.search = lambda *a, **k: False
        obj.create = lambda vals: SimpleNamespace(id=7, stock_status="out_of_stock", **vals)
        obj.resolve_stock_level = lambda *a: (None, {}, "unresolved")
        obj._extract_stock_candidates = lambda *a: {}
        instance = SimpleNamespace(id=17, _parse_ss_datetime=lambda s: None)
        vals = obj._upsert_from_payload(instance, {"sku": "UNAVAILABLE", "name": "unknown"})
        self.assertFalse(vals.remote_stock_verified)
        self.assertEqual(vals.stock_level, 0.0)


class DashboardRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pkg = types.ModuleType("shipstation_connector")
        pkg.__path__ = [str(MODULE)]
        models_pkg = types.ModuleType("shipstation_connector.models")
        models_pkg.__path__ = [str(MODULE / "models")]
        services = types.ModuleType("shipstation_connector.services")
        services.__path__ = [str(MODULE / "services")]
        ai = types.ModuleType("shipstation_connector.services.shipstation_ai_service")
        ai.ShipStationAIService = object
        cls.mod = load_python_module(
            MODULE / "models/shipstation_dashboard.py", "shipstation_connector.models.shipstation_dashboard",
            {"shipstation_connector": pkg, "shipstation_connector.models": models_pkg,
             "shipstation_connector.services": services,
             "shipstation_connector.services.shipstation_ai_service": ai},
        )

    def test_dashboard_all_sql_queries_are_scoped(self):
        class Cursor:
            def __init__(self):
                self.queries = []
                self.current = ""
            def execute(self, sql, args=None):
                self.current = sql
                self.queries.append((sql, args))
            def fetchall(self):
                if "FROM shipstation_instance si" in self.current:
                    return [(10, "Company 21", "C21", True, None)]
                if "FROM shipstation_order_sync" in self.current:
                    return [(10, "USD", 2, 25.0), (10, "INR", 1, 200.0)]
                if "FROM shipstation_sync_log l" in self.current or "FROM shipstation_sync_log\n" in self.current:
                    return []
                if "FROM shipstation_inventory inv" in self.current:
                    return []
                return []
            def fetchone(self):
                return [0]
        cursor = Cursor()
        dashboard = self.mod.ShipStationDashboard()
        dashboard.env = SimpleNamespace(cr=cursor, company=SimpleNamespace(id=21, currency_id=SimpleNamespace(name="USD")))
        dashboard._require_shipstation_dashboard_admin = Mock()
        dashboard._table_exists = lambda table: True
        dashboard._latest_ai_insight = lambda days: {}
        result = dashboard.get_dashboard_data()
        self.assertEqual(result["summary"]["instances"], 1)
        self.assertEqual(result["summary"]["orders"], 3)
        self.assertIsNone(result["summary"]["revenue"])
        self.assertFalse(result["summary"]["revenue_available"])
        self.assertFalse(result["instances"][0]["revenue_available"])
        self.assertGreaterEqual(len(cursor.queries), 10)
        for sql, args in cursor.queries:
            if "SELECT to_regclass" in sql:
                continue
            self.assertIsNotNone(args, sql)
            # Every query is parameterized by company ID or by scoped IDs.
            self.assertTrue(21 in args or args == ([10],), (sql, args))

    def test_dashboard_ai_lookup_has_current_company_scope(self):
        dashboard = self.mod.ShipStationDashboard()
        searcher = Mock()
        searcher.sudo.return_value.search.return_value = False
        dashboard.env = SimpleNamespace(
            company=SimpleNamespace(id=8),
            __getitem__=None,
        )
        # SimpleNamespace does not support __getitem__ type special methods; use dict-like.
        class FakeEnv:
            company = SimpleNamespace(id=8)
            def __getitem__(self, key): return searcher
        dashboard.env = FakeEnv()
        dashboard._latest_ai_insight(30)
        dom = searcher.sudo.return_value.search.call_args.args[0]
        self.assertIn(("company_id", "=", 8), dom)


class SafeRetryRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_python_module(
            MODULE / "models/shipstation_sync_log.py", "s01efg_synclog",
            cls.fake_framework(),
        )
    @staticmethod
    def fake_framework():
        # Default stubs already cover this model's field and exception imports.
        return {}

    def make_log(self, method, endpoint):
        log = self.mod.ShipStationSyncLog()
        log.ensure_one = lambda: None
        log.env = SimpleNamespace(su=True)
        log.check_access = Mock()
        log.status = "failed"
        log.method = method
        log.endpoint = endpoint
        log.request_payload = "{'page': 1}"
        log.retry_count = 0
        log.instance_id = Mock()
        log.instance_id._ss_request.return_value = {"orders": []}
        log.write = Mock()
        return log

    def test_post_label_retry_refused_before_api(self):
        log = self.make_log("POST", "/shipments/createlabel")
        with self.assertRaises(self.mod.UserError):
            log.action_retry_sync()
        log.instance_id._ss_request.assert_not_called()

    def test_delete_retry_refused_before_api(self):
        log = self.make_log("DELETE", "/orders/10")
        with self.assertRaises(self.mod.UserError):
            log.action_retry_sync()
        log.instance_id._ss_request.assert_not_called()

    def test_unknown_get_route_refused(self):
        log = self.make_log("GET", "//example.test")
        with self.assertRaises(self.mod.UserError):
            log.action_retry_sync()
        log.instance_id._ss_request.assert_not_called()

    def test_known_get_retried(self):
        log = self.make_log("GET", "/orders")
        log.action_retry_sync()
        log.instance_id._ss_request.assert_called_once()
        log.write.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
