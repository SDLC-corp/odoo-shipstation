"""S03L-M-N offline shipment safety regressions; no database/API required."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import ast
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent.parent
MODULE = ROOT/'shipstation_connector'
sys.argv[:] = [sys.argv[0]]

spec=importlib.util.spec_from_file_location('s03_loader',ROOT/'devtools/test_shipstation_api_s01b.py')
loader=importlib.util.module_from_spec(spec);spec.loader.exec_module(loader)
base, instance_mod, _ = loader.load_modules(MODULE)

fake_odoo=types.ModuleType('odoo');fake_odoo.fields=loader.FieldNamespace()
fake_odoo.models=SimpleNamespace(Model=object,AbstractModel=object)
fake_odoo._=lambda s:s
fake_odoo.api=SimpleNamespace(model_create_multi=lambda f:f,model=lambda f:f,depends=lambda *a:(lambda f:f))
fake_exc=types.ModuleType('odoo.exceptions')
fake_exc.UserError=base.UserError;fake_exc.AccessError=base.AccessError
with patch.dict(sys.modules, {'odoo':fake_odoo,'odoo.exceptions':fake_exc}):
    ss=importlib.util.spec_from_file_location('s03_shipment',MODULE/'models/shipstation_shipment_sync.py')
    shipment_mod=importlib.util.module_from_spec(ss);ss.loader.exec_module(shipment_mod)

class Rows(list):
    def __or__(self, other): return Rows([*self, *other])
    def filtered(self, fn): return Rows([r for r in self if fn(r)])
    def __getattr__(self, key):
        if len(self)!=1: raise AttributeError(key)
        return getattr(self[0],key)
    def __bool__(self): return len(self)>0

class Environment(dict):
    def __init__(self, cr, *args): super().__init__(*args);self.cr=cr

class FakeInstance(instance_mod.ShipStationInstance,base.ShipStationSyncBase):
    id=27;store_id='88';company_id=SimpleNamespace(id=3)
    last_shipment_sync_at=None;last_sync=None
    def ensure_one(self): pass
    def sudo(self): return self   # manual actions run as the integration after the admin check

class ShipmentSourceChecks(unittest.TestCase):
    def test_all_python_and_xml_parse(self):
        for file in MODULE.rglob('*.py'):ast.parse(file.read_text(encoding='utf-8'))
        for file in MODULE.rglob('*.xml'):ET.parse(file)

    def test_shipment_link_fields_exposed(self):
        s=(MODULE/'models/shipstation_shipment_sync.py').read_text()
        self.assertIn('picking_id = fields.Many2one("stock.picking"',s)
        self.assertIn('link_state = fields.Selection(',s)
        doc=ET.parse(MODULE/'views/shipstation_shipment_sync_view.xml')
        self.assertIn('link_state',[f.get('name') for f in doc.iter('field')])
        retry=[b for b in doc.iter('button') if b.get('name')=='action_retry_delivery_link']
        self.assertEqual(len(retry),2)
        self.assertTrue(all(b.get('groups')=='shipstation_connector.group_shipstation_admin' for b in retry))

    def test_no_unscoped_origin_matching_or_first_picking(self):
        s=(MODULE/'models/shipstation_shipment_sync.py').read_text()
        self.assertNotIn('("origin", "ilike", self.shipstation_order_number)',s)
        t=(MODULE/'models/shipstation_instance.py').read_text()
        method=t.split('    def _reconcile_single_shipment(',1)[1].split('    def _process_shipment_record(',1)[0]
        self.assertNotIn('[:1]',method)
        self.assertNotIn('("name", "ilike", carrier_code)',method)
        self.assertIn('picking.shipstation_tracking_number or picking.carrier_tracking_ref',method)

    def test_shipment_checkpoint_only_on_full_success(self):
        t=(MODULE/'models/shipstation_instance.py').read_text()
        method=t.split('    def _sync_shipments(',1)[1].split('    def _sync_products(',1)[0]
        self.assertIn('if not failed:',method)
        self.assertIn('with self.env.cr.savepoint():',method)
        self.assertIn('while page <= max_pages:',method)
        self.assertIn('status="failed" if failed else "success"',method)

class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.rec=shipment_mod.ShipStationShipmentSync()
        self.rec.search=Mock(return_value=Rows([]))
        self.rec.browse=Mock(return_value=Rows([]))
        self.instance=SimpleNamespace(id=27,store_id='88')

    def test_existing_package_id_wins(self):
        existing=SimpleNamespace(shipstation_store_id='88')
        self.rec.search.side_effect=[Rows([existing])]
        result=self.rec._find_existing_shipment(self.instance,'s1','o1','SO1','88','trk')
        self.assertIs(result[0],existing)
        self.assertIn(('shipstation_shipment_id','=','s1'),self.rec.search.call_args.args[0])

    def test_duplicate_real_ids_rejected(self):
        self.rec.search.return_value=Rows([SimpleNamespace(),SimpleNamespace()])
        with self.assertRaises(base.UserError):self.rec._find_existing_shipment(self.instance,'s1','o1','SO1','88')

    def test_wrong_store_fails_before_lookup(self):
        with self.assertRaises(base.UserError): self.rec._find_existing_shipment(self.instance,'s1','o1','SO1','99')
        self.rec.search.assert_not_called()

    def test_same_order_second_shipment_does_not_reuse_real_package(self):
        self.rec.search.side_effect=[Rows([]),Rows([])]
        self.rec._find_existing_shipment(self.instance,'new2','o1','SO1','88','newtrk')
        self.assertIn(('shipstation_shipment_id','=',False),self.rec.search.call_args.args[0])
        self.assertIn(('tracking_number','=',False),self.rec.search.call_args.args[0])

    def test_two_placeholders_require_review(self):
        self.rec.search.side_effect=[Rows([]),Rows([SimpleNamespace(),SimpleNamespace()])]
        with self.assertRaises(base.UserError):self.rec._find_existing_shipment(self.instance,'s1','o1','SO1','88')

    def test_tracking_only_exact_match(self):
        existing=SimpleNamespace()
        self.rec.search.return_value=Rows([existing])
        self.assertIs(self.rec._find_existing_shipment(self.instance,'','o1','SO1','88','trk')[0],existing)
        self.assertIn(('tracking_number','=','trk'),self.rec.search.call_args.args[0])


    def test_sync_for_order_skips_other_store_shipments(self):
        rec=shipment_mod.ShipStationShipmentSync()
        rec.browse=Mock(return_value=Rows([]))
        rec._upsert_from_payload=Mock(return_value=Rows([]))
        rec._upsert_placeholder_for_order=Mock(return_value=Rows([]))
        inst=SimpleNamespace(store_id='88',_ss_request=Mock(return_value={'shipments':[
            {'orderId':'o1','orderNumber':'SO1','storeId':'99','shipmentId':'wrong'},
            {'orderId':'o1','orderNumber':'SO1','storeId':'88','shipmentId':'right'}
        ]}))
        # Calling the upsert is sufficient to show store filtering occurs
        # before any local mutation.
        rec.sync_for_order(inst,{'orderId':'o1','orderNumber':'SO1','storeId':'88'})
        self.assertEqual(rec._upsert_from_payload.call_count,1)
        self.assertEqual(rec._upsert_from_payload.call_args.args[1]['shipmentId'],'right')

    def test_sync_for_order_rejects_invalid_shipment_row(self):
        rec=shipment_mod.ShipStationShipmentSync()
        rec.browse=Mock(return_value=Rows([]))
        rec._upsert_from_payload=Mock()
        inst=SimpleNamespace(store_id='88',_ss_request=Mock(return_value={'shipments':['broken']}))
        with self.assertRaises(base.UserError):
            rec.sync_for_order(inst,{'orderId':'o1','orderNumber':'SO1','storeId':'88'})
        rec._upsert_from_payload.assert_not_called()

    def test_payload_without_remote_shipment_or_tracking_rejected(self):
        self.rec._find_existing_shipment=Mock()
        with self.assertRaises(base.UserError):self.rec._upsert_from_payload(self.instance,{'orderId':'o1','storeId':'88'})
        self.rec._find_existing_shipment.assert_not_called()

class LinkTests(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance()
        self.mapping=Mock();self.mapping.search.return_value=Rows([])
        self.i.env=Environment(Mock(),{'shipstation.carrier.mapping':self.mapping})
        self.shipment=Mock(
            instance_id=SimpleNamespace(id=27),company_id=SimpleNamespace(id=3),
            shipstation_shipment_id='ship1',shipstation_order_id='order1',
            shipstation_order_number='SO1',shipstation_store_id='88',
            tracking_number='tracking1',carrier_code='ups',service_code='ground',ship_date=None,
        )
        self.shipment.write=Mock()
        self.pick=Mock(company_id=SimpleNamespace(id=3),state='assigned',picking_type_code='outgoing',
                       shipstation_shipment_id=None,shipstation_tracking_number=None,carrier_tracking_ref=None,id=91)
        self.order=Mock(company_id=SimpleNamespace(id=3),shipstation_instance_id=SimpleNamespace(id=27),picking_ids=Rows([self.pick]))
        self.i._find_order=Mock(return_value=self.order)

    def test_unowned_order_rejected(self):
        self.order.shipstation_instance_id=SimpleNamespace(id=999)
        with self.assertRaises(base.UserError):self.i._reconcile_single_shipment(self.shipment)
        self.pick.write.assert_not_called()

    def test_cross_company_shipment_rejected(self):
        self.shipment.company_id=SimpleNamespace(id=7)
        with self.assertRaises(base.UserError):self.i._reconcile_single_shipment(self.shipment)
        self.i._find_order.assert_not_called()

    def test_no_outgoing_picking_pending(self):
        self.order.picking_ids=Rows([])
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'pending')
        self.assertEqual(self.shipment.write.call_args.args[0]['link_state'],'pending')

    def test_multi_picking_review_and_no_update(self):
        other=Mock(company_id=SimpleNamespace(id=3),state='assigned',picking_type_code='outgoing',
                   shipstation_shipment_id=None,id=92)
        self.order.picking_ids=Rows([self.pick,other])
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'review')
        self.pick.write.assert_not_called(); other.write.assert_not_called()

    def test_existing_package_not_clobbered(self):
        self.pick.shipstation_shipment_id='another shipment'
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'review')
        self.pick.write.assert_not_called()

    def test_existing_tracking_not_clobbered(self):
        self.pick.carrier_tracking_ref='another tracking'
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'review')
        self.pick.write.assert_not_called()

    def test_no_tracking_pending_without_picking_write(self):
        self.shipment.tracking_number=False
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'pending')
        self.pick.write.assert_not_called()

    def test_single_clean_link_sets_tracking_and_link(self):
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'linked')
        vals=self.pick.write.call_args.args[0]
        self.assertEqual(vals['carrier_tracking_ref'],'tracking1')
        self.assertEqual(vals['shipstation_shipment_id'],'ship1')
        self.assertNotIn('carrier_id',vals)
        self.assertEqual(self.shipment.write.call_args.args[0]['link_state'],'linked')

    def test_matching_existing_shipment_id_can_relink(self):
        self.pick.shipstation_shipment_id='ship1';self.pick.carrier_tracking_ref='tracking1'
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'linked')

    def test_carrier_name_never_guessed(self):
        self.i._reconcile_single_shipment(self.shipment)
        self.mapping.search.assert_called_once()
        self.assertNotIn('carrier_id',self.pick.write.call_args.args[0])

    def test_wrong_company_picking_ignored(self):
        self.pick.company_id=SimpleNamespace(id=10)
        self.assertEqual(self.i._reconcile_single_shipment(self.shipment),'pending')
        self.pick.write.assert_not_called()

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance();self.i._get_sync_window=Mock(return_value=(datetime(2026,11,1),datetime(2026,11,5)))
        self.i._format_ss_datetime=Mock(return_value='2026-11-01T00:00:00')
        self.i._ss_request=Mock(return_value={'shipments':[{'shipmentId':'1','orderId':'1','storeId':'88'}]})
        self.i._process_shipment_record=Mock(return_value='linked')
        self.i._create_report=Mock()
        self.rollback=0;self.commits=0
        @contextmanager
        def savepoint():
            try:yield
            except Exception:
                self.rollback+=1;raise
            else:self.commits+=1
        self.i.env=Environment(SimpleNamespace(savepoint=savepoint))

    def test_all_success_advances_checkpoint(self):
        result=self.i._sync_shipments(mode='cron')
        self.assertEqual(result['linked'],1)
        self.assertEqual(result['failed'],0)
        self.assertTrue(result['checkpoint_advanced'])
        self.assertEqual(self.i.last_shipment_sync_at,datetime(2026,11,5))
        self.assertEqual(self.commits,1)

    def test_failed_record_holds_checkpoint(self):
        self.i._process_shipment_record.side_effect=ValueError('bad shipment')
        result=self.i._sync_shipments()
        self.assertEqual(result['failed'],1)
        self.assertFalse(result['checkpoint_advanced'])
        self.assertIsNone(self.i.last_shipment_sync_at)
        self.assertEqual(self.rollback,1)

    def test_page_transport_error_holds_checkpoint(self):
        self.i._ss_request.side_effect=RuntimeError('network down')
        result=self.i._sync_shipments()
        self.assertEqual(result['failed'],1)
        self.assertFalse(result['checkpoint_advanced'])

    def test_malformed_listing_holds_checkpoint(self):
        self.i._ss_request.return_value={'shipments':{}}
        result=self.i._sync_shipments()
        self.assertEqual(result['failed'],1)
        self.i._process_shipment_record.assert_not_called()

    def test_pending_and_review_preserve_checkpoint_but_are_visible(self):
        self.i._ss_request.return_value={'shipments':[{'shipmentId':'1'}, {'shipmentId':'2'}]}
        self.i._process_shipment_record.side_effect=['pending','review']
        result=self.i._sync_shipments()
        self.assertEqual((result['pending'],result['review'],result['failed']),(1,1,0))
        self.assertTrue(result['checkpoint_advanced'])

    def test_manual_notification_shows_warning_for_review(self):
        self.i._require_shipstation_admin=Mock()
        self.i._sync_shipments=Mock(return_value={'linked':1,'pending':0,'review':2,'failed':0})
        out=self.i.action_sync_shipments()
        self.assertEqual(out['params']['type'],'warning')
        self.assertTrue(out['params']['sticky'])

    def test_one_hundred_full_pages_stop_with_failed_checkpoint(self):
        self.i._ss_request.return_value={'shipments':[{'shipmentId':'1'}]*100}
        result=self.i._sync_shipments()
        self.assertEqual(self.i._ss_request.call_count,100)
        self.assertEqual(result['failed'],1)
        self.assertFalse(result['checkpoint_advanced'])

class ShipmentIngestTests(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance()
        self.mirror=Mock()
        self.mirror._upsert_from_payload.return_value=SimpleNamespace(id=10)
        self.cr=Mock()
        self.i.env=Environment(self.cr,{'shipstation.shipment.sync':self.mirror})
        self.i._reconcile_single_shipment=Mock(return_value='linked')
        self.payload={'shipmentId':'s1','orderId':'o1','storeId':'88','trackingNumber':'trk1'}

    def test_valid_shipment_locks_remote_identity_before_upsert(self):
        self.assertEqual(self.i._process_shipment_record(self.payload),'linked')
        self.assertIn('pg_advisory_xact_lock',self.cr.execute.call_args.args[0])
        self.mirror._upsert_from_payload.assert_called_once()
        self.i._reconcile_single_shipment.assert_called_once()

    def test_missing_order_id_fails_before_write(self):
        del self.payload['orderId']
        with self.assertRaises(base.UserError):self.i._process_shipment_record(self.payload)
        self.cr.execute.assert_not_called();self.mirror._upsert_from_payload.assert_not_called()

    def test_store_mismatch_fails_before_write(self):
        self.payload['storeId']='99'
        with self.assertRaises(base.UserError):self.i._process_shipment_record(self.payload)
        self.cr.execute.assert_not_called();self.mirror._upsert_from_payload.assert_not_called()

class ControllerMethods(unittest.TestCase):
    def test_tracking_javascript_scheme_rejected(self):
        r=shipment_mod.ShipStationShipmentSync();r.ensure_one=Mock();r.tracking_url='javascript:alert(1)'
        with self.assertRaises(base.UserError):r.action_open_tracking()

    def test_tracking_http_scheme_rejected(self):
        r=shipment_mod.ShipStationShipmentSync();r.ensure_one=Mock();r.tracking_url='http://carrier.example/track'
        with self.assertRaises(base.UserError):r.action_open_tracking()

    def test_tracking_https_accepted(self):
        r=shipment_mod.ShipStationShipmentSync();r.ensure_one=Mock();r.tracking_url='https://carrier.example/track/1'
        out=r.action_open_tracking();self.assertEqual(out['url'],r.tracking_url)

    def test_link_retry_rejects_non_admin(self):
        r=shipment_mod.ShipStationShipmentSync();r.ensure_one=Mock()
        r.env=SimpleNamespace(su=False,user=SimpleNamespace(has_group=lambda g:False))
        with self.assertRaises(base.AccessError):r.action_retry_delivery_link()

if __name__=='__main__':unittest.main(verbosity=2)
