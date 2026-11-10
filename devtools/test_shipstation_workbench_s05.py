"""Lightweight, self-contained S05 contracts. No Odoo DB or external APIs required.

Usage: python -B devtools/test_shipstation_workbench_s05.py <repository-root>
"""
import ast
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
sys.argv[:] = [sys.argv[0]]
MODULE = ROOT / "shipstation_connector"


class AccessError(Exception):
    pass


class UserError(Exception):
    pass


class ValidationError(Exception):
    pass


class DummyFields:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


fake_odoo = types.ModuleType("odoo")
fake_odoo.api = types.SimpleNamespace(constrains=lambda *names: (lambda fn: fn))
fake_odoo.models = types.SimpleNamespace(Model=object)
fake_odoo.fields = DummyFields()
def fake_datetime_field(*args, **kwargs):
    return None
fake_datetime_field.now = lambda: "2026-11-05 12:00:00"
fake_odoo.fields.Datetime = fake_datetime_field
fake_odoo._ = lambda text: text
fake_exc = types.ModuleType("odoo.exceptions")
fake_exc.AccessError = AccessError
fake_exc.UserError = UserError
fake_exc.ValidationError = ValidationError
spec = importlib.util.spec_from_file_location("s05_workbench_contract", MODULE / "models/shipstation_workbench.py")
loaded = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"odoo": fake_odoo, "odoo.exceptions": fake_exc}):
    spec.loader.exec_module(loaded)
Workbench = loaded.ShipStationShipmentWorkbench


class Shipment:
    def __init__(self, company, outcome="linked", owner=None, link_state="review"):
        self.id = id(self) % 999999
        self.company_id = company
        self.link_state = link_state
        self.review_owner_id = owner
        self.review_check_count = 0
        self.values = {}
        self.instance_id = types.SimpleNamespace(
            _reconcile_single_shipment=lambda s: outcome,
        )
        # Reconciliation runs as the integration after the role/company checks (``sudo()``).
        self.instance_id.sudo = lambda: self.instance_id

    def sudo(self):
        return self

    def write(self, values):
        self.values.update(values)
        self.review_check_count = values.get("review_check_count", self.review_check_count)


class Records:
    _check_workbench_batch_size = Workbench._check_workbench_batch_size
    _check_workbench_operator = Workbench._check_workbench_operator
    _workbench_notice = staticmethod(Workbench._workbench_notice)
    action_bulk_recheck_delivery_links = Workbench.action_bulk_recheck_delivery_links
    action_claim_review = Workbench.action_claim_review
    action_release_review = Workbench.action_release_review

    def __init__(self, shipments, *, role="admin", company="one", user=None):
        self.items = shipments
        self.user = user or types.SimpleNamespace(id=42)
        self.user.has_group = lambda group: role == "admin" or (role == "manager" and group.endswith("manager"))
        self.env = types.SimpleNamespace(
            su=False, user=self.user, companies=[company],
            cr=types.SimpleNamespace(savepoint=lambda: nullcontext()),
        )
        self.access_checks = []

    def __bool__(self):
        return bool(self.items)

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def check_access_rights(self, operation):
        self.access_checks.append(operation)

    def check_access_rule(self, operation):
        self.access_checks.append(operation)

    def write(self, vals):
        for item in self.items:
            item.write(vals)

    def filtered(self, fn):
        return Records([r for r in self if fn(r)], role="manager", user=self.user)


class WorkbenchRuntime(unittest.TestCase):
    def test_reader_cannot_claim_shipments(self):
        records = Records([Shipment("one")], role="reader")
        with self.assertRaises(AccessError):
            records.action_claim_review()

    def test_manager_can_claim_unresolved_only(self):
        open_record = Shipment("one")
        linked = Shipment("one", link_state="linked")
        records = Records([open_record, linked], role="manager")
        action = records.action_claim_review()
        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(open_record.values["review_owner_id"], 42)
        self.assertEqual(linked.values, {})

    def test_manager_cannot_bulk_recheck(self):
        records = Records([Shipment("one")], role="manager")
        with self.assertRaises(AccessError):
            records.action_bulk_recheck_delivery_links()

    def test_admin_rechecks_locally_and_counts_outcomes(self):
        linked = Shipment("one", outcome="linked")
        review = Shipment("one", outcome="review")
        records = Records([linked, review])
        result = records.action_bulk_recheck_delivery_links()
        self.assertIn("Linked: 1", result["params"]["message"])
        self.assertIn("Review: 1", result["params"]["message"])
        self.assertEqual(linked.review_check_count, 1)
        self.assertEqual(review.review_check_count, 1)

    def test_wrong_company_rejected_before_reconcile(self):
        records = Records([Shipment("second")], role="admin", company="one")
        with self.assertRaises(AccessError):
            records.action_bulk_recheck_delivery_links()

    def test_batch_limit_blocks_large_selection(self):
        records = Records([Shipment("one") for _ in range(101)], role="admin")
        with self.assertRaises(UserError):
            records.action_bulk_recheck_delivery_links()

    def test_failed_reconcile_does_not_abort_remaining(self):
        bad = Shipment("one")
        def fails(_):
            raise ValueError("test failure")
        bad.instance_id._reconcile_single_shipment = fails
        good = Shipment("one", outcome="linked")
        records = Records([bad, good])
        with patch.object(loaded._logger, "exception"):
            action = records.action_bulk_recheck_delivery_links()
        self.assertIn("Failed: 1", action["params"]["message"])
        self.assertEqual(good.review_check_count, 1)
        self.assertEqual(bad.review_check_count, 0)

    def test_nonowner_manager_cannot_release(self):
        other_user = types.SimpleNamespace(id=7)
        record = Shipment("one", owner=other_user)
        records = Records([record], role="manager")
        with self.assertRaises(AccessError):
            records.action_release_review()


class WorkbenchSource(unittest.TestCase):
    def test_xml_and_manifest_are_connected(self):
        ET.parse(MODULE / "views/shipstation_workbench_views.xml")
        tree = ET.parse(MODULE / "views/menu.xml").getroot()
        self.assertTrue(tree.findall(".//menuitem[@id='menu_shipstation_workbench']"))
        source = (MODULE / "__manifest__.py").read_text(encoding="utf-8")
        self.assertIn('"views/shipstation_workbench_views.xml"', source)

    def test_action_uses_existing_shipments_and_open_queue(self):
        root = ET.parse(MODULE / "views/shipstation_workbench_views.xml").getroot()
        actions = root.findall(".//record[@model='ir.actions.act_window']")
        self.assertEqual(len(actions), 1)
        data = {f.get("name"): f.text for f in actions[0].findall("field")}
        self.assertEqual(data["res_model"], "shipstation.shipment.sync")
        self.assertIn("search_default_open_queue", data["context"])

    def test_backend_contains_no_remote_write_calls(self):
        source = (MODULE / "models/shipstation_workbench.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        reconcile = ast.get_source_segment(source, funcs["action_bulk_recheck_delivery_links"])
        self.assertNotIn("_ss_request", reconcile)
        self.assertNotIn("createlabel", reconcile.lower())
        self.assertNotIn("stock.quant", reconcile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
