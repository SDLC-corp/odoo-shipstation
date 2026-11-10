"""S06 contract tests without Odoo DB or ShipStation network access."""
import ast
import hashlib
import json
import pathlib
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "shipstation_connector"


class UserError(Exception):
    pass


def _(text):
    return text


class DateTime:
    @staticmethod
    def now():
        import datetime
        return datetime.datetime(2026, 11, 5, 15, 0, 0)

    @staticmethod
    def to_datetime(x):
        return x


class FakeOrder:
    carrier_code = "fedex"
    service_code = False
    package_code = "package"
    confirmation = "none"
    rate_from_postal = "78703"
    rate_to_country = "us"
    rate_to_postal = "20500"
    rate_to_state = "DC"
    rate_to_city = "Washington"
    rate_residential = False
    rate_weight = 24.0
    rate_weight_units = "ounces"
    rate_length = 7.0
    rate_width = 5.0
    rate_height = 6.0
    rate_dimension_units = "inches"

    def ensure_one(self):
        return self


class ShippingSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ADDON / "models/shipstation_order_sync.py").read_text(encoding="utf-8")
        syntax = ast.parse(source)
        model = next(n for n in syntax.body if isinstance(n, ast.ClassDef) and n.name == "ShipStationOrderSync")
        cls.methods = {n.name: n for n in model.body if isinstance(n, ast.FunctionDef)}
        fake = type("TestOrder", (FakeOrder,), {})
        context = {"UserError": UserError, "_": _, "math": __import__("math"),
                   "hashlib": hashlib, "json": json,
                   "fields": SimpleNamespace(Datetime=DateTime),
                   "timedelta": __import__("datetime").timedelta}
        for name in ("_build_v1_rate_request", "_rate_fingerprint", "_ensure_fresh_rate_quote"):
            node = cls.methods[name]
            single = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(single)
            exec(compile(single, "test_method", "exec"), context)
            setattr(fake, name, context[name])
        cls.fake_cls = fake

    def test_v1_rates_uses_documented_origin_destination_and_weight(self):
        request = self.fake_cls()._build_v1_rate_request()
        self.assertEqual(request["fromPostalCode"], "78703")
        self.assertEqual(request["toCountry"], "US")
        self.assertEqual(request["weight"], {"value": 24.0, "units": "ounces"})
        self.assertEqual(request["dimensions"]["length"], 7.0)
        self.assertNotIn("orderId", request)

    def test_requires_carrier(self):
        o = self.fake_cls()
        o.carrier_code = ""
        with self.assertRaises(UserError):
            o._build_v1_rate_request()

    def test_requires_valid_destination(self):
        o = self.fake_cls()
        o.rate_to_country = "USA"
        with self.assertRaises(UserError):
            o._build_v1_rate_request()

    def test_rejects_zero_weight(self):
        o = self.fake_cls()
        o.rate_weight = 0
        with self.assertRaises(UserError):
            o._build_v1_rate_request()

    def test_rejects_partial_dimensions(self):
        o = self.fake_cls()
        o.rate_height = 0
        with self.assertRaises(UserError):
            o._build_v1_rate_request()

    def test_rejects_nonfinite_weight(self):
        o = self.fake_cls()
        o.rate_weight = float("nan")
        with self.assertRaises(UserError):
            o._build_v1_rate_request()

    def test_fingerprint_changes_on_destination_change(self):
        o = self.fake_cls()
        before = o._rate_fingerprint(o._build_v1_rate_request())
        o.rate_to_postal = "90210"
        self.assertNotEqual(before, o._rate_fingerprint(o._build_v1_rate_request()))

    def test_quotes_fail_closed_when_changed(self):
        o = self.fake_cls()
        o.rate_ids = [1]
        o.rate_checked_at = DateTime.now()
        o.rate_request_hash = o._rate_fingerprint(o._build_v1_rate_request())
        o.rate_to_postal = "90210"
        with self.assertRaises(UserError):
            o._ensure_fresh_rate_quote()

    def test_no_auto_label_purchase(self):
        action = self.methods["action_get_rates"]
        calls = [n for n in ast.walk(action) if isinstance(n, ast.Constant) and isinstance(n.value,str)]
        self.assertTrue(any("/shipments/getrates" in n.value for n in calls))
        self.assertFalse(any("createlabel" in n.value.lower() for n in calls))

    def test_only_valid_rates_replace_existing_quotes(self):
        src = ast.get_source_segment((ADDON/'models/shipstation_order_sync.py').read_text(),self.methods['action_get_rates'])
        self.assertLess(src.index('prepared = []'), src.index('self.rate_ids.sudo().unlink()'))

    def test_all_model_methods_still_in_class(self):
        self.assertIn("action_create_label", self.methods)
        self.assertIn("action_push_to_shipstation", self.methods)
        self.assertIn("action_apply_package_preset", self.methods)

    def test_extra_models_have_company_acl_and_record_rules(self):
        csv=(ADDON/'security/ir.access.csv').read_text()  # Odoo 20: ir.access.csv, model by name
        rules=(ADDON/'security/shipstation_record_rules.xml').read_text()
        for item in ('shipstation_package_preset','shipstation_carrier_catalog'):
            self.assertIn(','+item.replace('_','.')+',',csv)
            self.assertIn('model_'+item,rules)

    def test_carrier_discovery_read_only_remote(self):
        src=(ADDON/'models/shipstation_carrier_discovery.py').read_text()
        self.assertIn('self._require_shipstation_admin()',src)
        self.assertIn('"GET", "/carriers"',src)
        self.assertNotIn('"POST"',src)

    def test_views_and_models_parse(self):
        for file in ADDON.rglob('*.xml'):
            ET.parse(file)
        for file in ADDON.rglob('*.py'):
            ast.parse(file.read_text(encoding='utf-8-sig'))

if __name__=='__main__':
    unittest.main(verbosity=2)
