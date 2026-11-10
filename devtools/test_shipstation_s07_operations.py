"""S07 focused offline contract checks; never connects to Odoo/Postgres/ShipStation."""
import ast
import csv
import io
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'shipstation_connector'

class UserError(Exception):
    pass

class FakeLines(list):
    def filtered(self, fn):
        return FakeLines(x for x in self if fn(x))

class RateStub:
    def __init__(self, code='ups', service='ground', package='box'):
        self.carrier_code = code
        self.service_code = service
        self.package_code = package


class MethodContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ADDON/'models/shipstation_fulfillment_batch.py'
        tree = ast.parse(path.read_text())
        cls.classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}
        cls.methods = {
            class_name: {n.name: n for n in c.body if isinstance(n, ast.FunctionDef)}
            for class_name, c in cls.classes.items()
        }
        method = cls.methods['ShipStationFulfillmentBatchLine']['_review_local_preparation']
        # Reuse the *production* method, not a recreated test algorithm.
        module = ast.Module(body=[method], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace={'UserError':UserError,'_':lambda s:s}
        exec(compile(module,'s07_isolated','exec'),namespace)
        cls.review = namespace['_review_local_preparation']

    def make_line(self, *, order_instance=1, batch_instance=1, status='awaiting_shipment',
                  label=None, tracking=None, quote_ok=True, carrier='ups',service='ground',package='box'):
        order=SimpleNamespace(instance_id=order_instance, status=status,
                              shipstation_shipment_id=None,label_url=label,tracking_number=tracking,
                              shipstation_order_id='12345',carrier_code=carrier,service_code=service,package_code=package,
                              rate_ids=FakeLines([RateStub()]),
                              _ensure_fresh_rate_quote=Mock(side_effect=None if quote_ok else UserError('stale')))
        obj=SimpleNamespace(order_id=order,batch_id=SimpleNamespace(instance_id=batch_instance),ensure_one=lambda:None)
        return obj,order

    def test_ready_with_current_quote(self):
        obj,order=self.make_line()
        state,msg=self.__class__.review(obj)
        self.assertEqual(state,'ready')
        self.assertIn('manually',msg)
        order._ensure_fresh_rate_quote.assert_called_once()

    def test_label_prevents_new_purchase(self):
        obj,_=self.make_line(label='https://cdn.example/label.pdf')
        self.assertEqual(self.__class__.review(obj)[0],'shipped')

    def test_tracking_prevents_new_purchase(self):
        obj,_=self.make_line(tracking='TRACKING')
        self.assertEqual(self.__class__.review(obj)[0],'shipped')

    def test_wrong_instance_blocks(self):
        obj,_=self.make_line(order_instance=2)
        self.assertEqual(self.__class__.review(obj)[0],'blocked')

    def test_canceled_order_blocks(self):
        obj,_=self.make_line(status='cancelled')
        self.assertEqual(self.__class__.review(obj)[0],'blocked')

    def test_missing_carrier_blocks(self):
        obj,_=self.make_line(carrier='')
        self.assertEqual(self.__class__.review(obj)[0],'blocked')

    def test_expired_quote_blocks(self):
        obj,_=self.make_line(quote_ok=False)
        self.assertEqual(self.__class__.review(obj)[0],'blocked')

    def test_unquoted_service_blocks(self):
        obj,_=self.make_line(service='air')
        self.assertEqual(self.__class__.review(obj)[0],'blocked')

    def test_return_state_locked_down(self):
        code=(ADDON/'models/shipstation_return_case.py').read_text()
        self.assertIn('("state", "approved_by_id", "approved_at")',code)
        self.assertIn('super(ShipStationReturnCase, self).write({"state": "review"})',code)
        self.assertIn('self._require_operator(admin=True)',code)
        self.assertIn('Return cases must start as drafts',code)
        self.assertIn('Return approval metadata is set only by approval actions',code)

    def test_return_requires_linked_source(self):
        code=(ADDON/'models/shipstation_return_case.py').read_text()
        self.assertIn('if not vals.get("order_id") and not vals.get("shipment_id"):',code)
        self.assertIn('rec.shipment_id.instance_id != rec.instance_id',code)
        self.assertIn('Return order and shipment do not refer to the same',code)

    def test_return_cannot_change_lines_after_approval(self):
        code=(ADDON/'models/shipstation_return_case.py').read_text()
        self.assertIn('Return items cannot be changed after approval',code)
        self.assertIn('Return items cannot be deleted after approval',code)

    def test_return_cannot_finalize_without_note(self):
        code=(ADDON/'models/shipstation_return_case.py').read_text()
        self.assertIn('not (self.resolution_note or "").strip()',code)
        self.assertIn('if self.state != "approved"',code)

    def test_bulk_cap_and_group_guard(self):
        code=(ADDON/'models/shipstation_fulfillment_batch.py').read_text()
        self.assertIn('MAX_FULFILLMENT_ORDERS = 100',code)
        self.assertIn('group_shipstation_manager',code)
        self.assertIn('self.check_access_rights("read")',code)
        self.assertIn('self.check_access_rule("read")',code)
        self.assertIn('open fulfillment batch',code)

    def test_batch_state_not_set_by_direct_write(self):
        code=(ADDON/'models/shipstation_fulfillment_batch.py').read_text()
        self.assertIn('("state", "instance_id", "company_id", "last_reviewed_at")',code)
        self.assertIn('Use batch readiness review to update line status',code)
        self.assertIn('UNIQUE(batch_id, order_id)',code)

    def test_batch_actions_cannot_make_label_requests(self):
        code=(ADDON/'models/shipstation_fulfillment_batch.py').read_text()
        self.assertNotIn('_ss_request(',code)
        self.assertNotIn('action_create_label(',code)
        self.assertNotIn('stock.quant',code)

    def test_return_actions_cannot_change_stock_or_refunds(self):
        code=(ADDON/'models/shipstation_return_case.py').read_text()
        self.assertNotIn('_ss_request(',code)
        self.assertNotIn('stock.quant',code)
        self.assertNotIn('account.move',code)

    def test_paid_label_has_early_server_side_group_check(self):
        code=(ADDON/'models/shipstation_order_sync.py').read_text()
        method=code.split('    def action_create_label(self):',1)[1].split('    def action_open_label(self):',1)[0]
        self.assertLess(method.index('self._check_rate_operator()'),method.index('_ss_request("POST"'))

    def test_four_new_models_have_company_rules_and_acl(self):
        rules=ET.parse(ADDON/'security/shipstation_record_rules.xml')
        xml_ids={r.get('id') for r in rules.iter('record')}
        csv_lines=list(csv.DictReader(io.StringIO((ADDON/'security/ir.model.access.csv').read_text())))
        for name in ('fulfillment_batch','fulfillment_batch_line','return_case','return_case_line'):
            self.assertIn(f'rule_shipstation_s07_{name}_company',xml_ids)
            acl=[r for r in csv_lines if r['model_id:id']==f'model_shipstation_{name}']
            self.assertEqual(len(acl),3)
            for r in acl:
                if 'user_readonly' in r['group_id:id']:
                    self.assertEqual((r['perm_write'],r['perm_create'],r['perm_unlink']),('0','0','0'))

    def test_views_and_actions_registered(self):
        view=ADDON/'views/shipstation_s07_fulfillment_returns_views.xml'
        root=ET.parse(view).getroot()
        ids={rec.get('id') for rec in root.iter('record')}
        self.assertIn('action_shipstation_s07_make_batch',ids)
        self.assertIn('action_shipstation_s07_return_cases',ids)
        self.assertIn('view_shipstation_s07_start_return_shipment',ids)
        manifest=(ADDON/'__manifest__.py').read_text()
        self.assertIn('views/shipstation_s07_fulfillment_returns_views.xml',manifest)
        menu=(ADDON/'views/menu.xml').read_text()
        self.assertIn('menu_shipstation_s07_return_cases',menu)
        self.assertIn('menu_shipstation_s07_fulfillment_batches',menu)

    def test_static_parsing_all_sources(self):
        for file in ADDON.rglob('*.py'):ast.parse(file.read_text(encoding='utf-8-sig'))
        for file in ADDON.rglob('*.xml'):ET.parse(file)


if __name__ == '__main__':
    unittest.main(verbosity=2)
