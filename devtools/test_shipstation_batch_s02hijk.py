"""S02H–K offline regression: order identity, checkpoint, partial failure, no paid-write retry.

python -B devtools/test_shipstation_batch_s02hijk.py <repository-root>
Does not load Odoo, PostgreSQL, credentials or contact ShipStation.
"""
import ast
from contextlib import contextmanager
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
MODULE = ROOT / 'shipstation_connector'
sys.argv[:] = [sys.argv[0]]

spec = importlib.util.spec_from_file_location('s02_loader', ROOT/'devtools/test_shipstation_api_s01b.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
base, instance_mod, inventory_mod = loader.load_modules(MODULE)

# Order listing and sale model, using the same mocked Odoo module bindings
import types
fake_odoo = types.ModuleType('odoo')
fake_odoo.fields = loader.FieldNamespace()
fake_odoo.models = SimpleNamespace(Model=object, AbstractModel=object)
fake_odoo._ = lambda s: s
fake_odoo.api = SimpleNamespace(model_create_multi=lambda f:f, model=lambda f:f, depends=lambda *a:(lambda f:f))
fake_exc = types.ModuleType('odoo.exceptions')
fake_exc.UserError = base.UserError
fake_exc.AccessError = base.AccessError
with patch.dict(sys.modules, {'odoo':fake_odoo,'odoo.exceptions':fake_exc}):
    def read_module(name, path):
        spec=importlib.util.spec_from_file_location(name, MODULE/path)
        obj=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(obj)
        return obj
    order_mod=read_module('s02_orders','models/shipstation_order_sync.py')

class FakeInstance(instance_mod.ShipStationInstance, base.ShipStationSyncBase):
    _name = 'offline.not.real.instance'
    id = 27
    store_id = '88'
    order_status = 'all'
    order_sync_days_back = 7
    last_order_sync_at = None
    last_sync = None
    api_key = 'fake'
    api_secret = 'fake'
    v2_api_key = 'fake'
    base_url = 'https://api.shipstation.com'
    company_id = SimpleNamespace(id=3)
    def ensure_one(self): return None
    def _get_shipstation_auth_record(self): return self
    def sudo(self): return self   # manual actions run as the integration after the admin check

class FakeRecordset:
    def __init__(self, records): self.records = records
    def __bool__(self): return bool(self.records)
    def __len__(self): return len(self.records)
    def __getattr__(self, attr):
        if len(self.records) != 1: raise AttributeError(attr)
        return getattr(self.records[0], attr)

class SourceChecks(unittest.TestCase):
    def test_syntax_and_one_import_pipeline(self):
        for p in MODULE.rglob('*.py'):
            ast.parse(p.read_text(encoding='utf8'), filename=str(p))
        s = (MODULE/'models/shipstation_instance.py').read_text()
        self.assertEqual(s.count('    def _sync_orders(self, mode="manual"):'),1)
        method=s.split('    def _sync_orders(self, mode="manual"):',1)[1].split('    def _sync_shipments(',1)[0]
        self.assertIn('with self.env.cr.savepoint():',method)
        self.assertIn('if not failed and not used_undated_fallback:',method)
        self.assertNotIn('order.order_line.unlink()',method)
        self.assertEqual(method.count('self._import_order_record(payload)'),1)

    def test_models_link_instance(self):
        s=(MODULE/'models/shipstation_sale.py').read_text()
        self.assertIn('shipstation_instance_id = fields.Many2one("shipstation.instance"',s)
        s=(MODULE/'models/shipstation_order_sync.py').read_text()
        self.assertIn('limit=2,',s.split('    def _upsert_from_payload(',1)[1])

    def test_both_api_adapters_never_retry_post(self):
        # V1 and V2 now share ONE transport, so the "only GET/HEAD are retried" rule exists exactly once.
        s=(MODULE/'models/shipstation_sync_base.py').read_text()
        self.assertEqual(s.count('retries = 3 if method in ("GET", "HEAD") else 0'),1)
        self.assertEqual(s.count('return self._http_call('),2)

    def test_label_repurchase_guard_before_api(self):
        s=(MODULE/'models/shipstation_order_sync.py').read_text().split('    def action_create_label(self):',1)[1].split('    def action_open_label(',1)[0]
        self.assertLess(s.index('if self.shipstation_shipment_id or self.tracking_number or self.label_url:'),s.index('_ss_request("POST"'))


def with_store_helpers(ns):
    """Give a stub instance the REAL store helpers (the production implementation, not a re-statement)."""
    from types import MethodType
    ns._remote_store_id = base.ShipStationSyncBase._remote_store_id
    ns._attributed_store_id = MethodType(base.ShipStationSyncBase._attributed_store_id, ns)
    ns._remote_store_matches = MethodType(base.ShipStationSyncBase._remote_store_matches, ns)
    return ns

class IdentityChecks(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance();self.i.env=Mock()
        self.sale=Mock()
        self.sale.with_company.return_value=self.sale
        self.sale.browse.return_value=FakeRecordset([])
        self.instances=Mock()
        self.instances.sudo.return_value.with_context.return_value.search_count.return_value=2   # several connections
        self.i.env={'sale.order':self.sale,'shipstation.instance':self.instances}

    def test_scoped_lookup_uses_instance_and_company(self):
        self.sale.search.side_effect=[FakeRecordset([SimpleNamespace(shipstation_store_id='88')])]
        result=self.i._find_order('120','SO120','88')
        self.assertTrue(result)
        domain=self.sale.search.call_args.args[0]
        self.assertIn(('shipstation_instance_id','=',27),domain)
        self.assertIn(('company_id','=',3),domain)

    def test_duplicate_order_identity_rejected(self):
        self.sale.search.return_value=FakeRecordset([SimpleNamespace(),SimpleNamespace()])
        with self.assertRaises(base.UserError): self.i._find_order('120','SO120','88')

    def test_cross_store_conflict_rejected(self):
        self.sale.search.return_value=FakeRecordset([SimpleNamespace(shipstation_store_id='99')])
        with self.assertRaises(base.UserError): self.i._find_order('120','SO120','88')

    def test_legacy_order_adopted_only_same_company_store(self):
        self.sale.search.side_effect=[FakeRecordset([]),FakeRecordset([SimpleNamespace()])]
        self.assertTrue(self.i._find_order('120','SO120','88'))
        domain=self.sale.search.call_args.args[0]
        self.assertIn(('shipstation_instance_id','=',False),domain)
        self.assertIn(('shipstation_store_id','in',['88']),domain)   # several connections: a blank store is ambiguous

    def test_blank_store_legacy_order_adopted_only_when_company_has_one_connection(self):
        # The published build recorded a blank store because real orders carry none at the top level.
        self.instances.sudo.return_value.with_context.return_value.search_count.return_value=1
        self.sale.search.side_effect=[FakeRecordset([]),FakeRecordset([SimpleNamespace()])]
        self.assertTrue(self.i._find_order('120','SO120','88'))
        domain=self.sale.search.call_args.args[0]
        self.assertIn(('shipstation_store_id','in',['88',False]),domain)
        self.assertIn(('company_id','=',3),domain)

    def test_number_lookup_never_uses_other_store(self):
        self.sale.search.side_effect=[FakeRecordset([]),FakeRecordset([]),FakeRecordset([])]
        self.assertFalse(self.i._find_order('120','SO120','88'))
        domain=self.sale.search.call_args.args[0]
        self.assertIn(('shipstation_instance_id','=',27),domain)
        self.assertIn(('shipstation_store_id','=','88'),domain)

class ImportChecks(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance()
        self.cr=Mock()
        self.sale=Mock();self.sale.with_company.return_value=self.sale
        self.mirror=Mock()
        self.i.env=SimpleNamespace(cr=self.cr)
        class Store(dict):
            def __init__(self,cr, *a,**kw): super().__init__(*a,**kw);self.cr=cr
        self.i.env=Store(self.cr,{'sale.order':self.sale,'shipstation.order.sync':self.mirror})
        self.i._find_order=Mock(return_value=False)
        self.i._parse_ss_datetime=Mock(return_value=datetime(2026,11,5,10,0))
        self.i._find_partner=Mock(return_value=(SimpleNamespace(id=1),SimpleNamespace(id=2)))
        self.i._prepare_lines=Mock(return_value=[(0,0,{'name':'Demo'})])
        self.i._apply_store_mapping_to_order_vals=Mock(side_effect=lambda vals,store: vals)
        self.i._apply_automation_rules_to_order_vals=Mock(side_effect=lambda d,vals: vals)
        self.data={'orderId':120,'orderNumber':'SO120','storeId':88,'orderStatus':'awaiting_shipment'}

    def test_store_mismatch_fails_before_db(self):
        self.data['storeId']=99
        with self.assertRaises(base.UserError):self.i._import_order_record(self.data)
        self.cr.execute.assert_not_called()

    def test_missing_order_id_fails_closed(self):
        self.data.pop('orderId')
        with self.assertRaises(base.UserError):self.i._import_order_record(self.data)
        self.cr.execute.assert_not_called()

    def test_new_order_uses_company_and_instance(self):
        self.assertEqual(self.i._import_order_record(self.data),'processed')
        vals=self.sale.create.call_args.args[0]
        self.assertEqual(vals['company_id'],3)
        self.assertEqual(vals['shipstation_instance_id'],27)
        self.assertEqual(vals['shipstation_order_id'],'120')
        self.assertIn('order_line', vals)
        self.cr.execute.assert_called_once()
        self.assertIn('pg_advisory_xact_lock',self.cr.execute.call_args.args[0])
        self.mirror._upsert_from_payload.assert_called_once()

    def test_confirmed_order_is_metadata_only(self):
        sale=Mock(state='sale', shipstation_last_modified_date=None)
        self.i._find_order.return_value=sale
        self.i._find_partner.side_effect=AssertionError('must not recreate customer')
        self.i._import_order_record(self.data)
        fields=sale.write.call_args.args[0]
        self.assertNotIn('partner_id',fields)
        self.assertNotIn('order_line',fields)
        sale.action_cancel.assert_not_called()

    def test_stale_update_skips_mutation(self):
        sale=Mock(state='draft',shipstation_last_modified_date=datetime(2026,11,5,11,0))
        self.i._find_order.return_value=sale
        self.assertEqual(self.i._import_order_record(self.data),'stale')
        sale.write.assert_not_called()
        self.mirror._upsert_from_payload.assert_not_called()

    def test_existing_draft_updates_lines_only_inside_operation(self):
        sale=Mock(state='draft',shipstation_last_modified_date=None)
        self.i._find_order.return_value=sale
        self.i._import_order_record(self.data)
        sale.order_line.unlink.assert_called_once()
        self.assertTrue(any('order_line' in call.args[0] for call in sale.write.call_args_list))
        self.sale.create.assert_not_called()

class PipelineChecks(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance()
        self.cr=Mock()
        counters={'commits':0,'rollbacks':0}
        @contextmanager
        def savepoint():
            try: yield
            except Exception:
                counters['rollbacks']+=1
                raise
            else: counters['commits']+=1
        self.cr.savepoint=savepoint
        self.i.env=SimpleNamespace(cr=self.cr)
        self.i._get_sync_window=Mock(return_value=(datetime(2026,11,1),datetime(2026,11,5)))
        self.i._fetch_order_pages=Mock(return_value=iter([[{'orderNumber':'1'},{'orderNumber':'2'}]]))
        self.i._create_report=Mock()
        self.i._import_order_record=Mock(side_effect=['processed','processed'])
        self.counters=counters

    def test_full_success_advances_checkpoint(self):
        result=self.i._sync_orders(mode='cron')
        self.assertEqual(result['processed'],2)
        self.assertTrue(result['checkpoint_advanced'])
        self.assertEqual(self.i.last_order_sync_at,datetime(2026,11,5))
        self.assertEqual(self.counters['commits'],2)

    def test_partial_record_failure_holds_checkpoint_and_reports(self):
        self.i._import_order_record.side_effect=['processed',ValueError('bad record')]
        result=self.i._sync_orders(mode='cron')
        self.assertEqual(result['failed'],1)
        self.assertFalse(result['checkpoint_advanced'])
        self.assertIsNone(self.i.last_order_sync_at)
        self.assertEqual(self.counters['commits'],1)
        self.assertEqual(self.counters['rollbacks'],1)
        self.assertTrue(any(c.kwargs.get('status')=='failed' for c in self.i._create_report.call_args_list))

    def test_network_error_holds_checkpoint(self):
        def generator(*args,**kwargs):
            yield [{'orderNumber':'1'}]
            raise RuntimeError('transport error')
        self.i._fetch_order_pages.side_effect=generator
        self.i._import_order_record.side_effect=None
        self.i._import_order_record.return_value='processed'
        result=self.i._sync_orders(mode='cron')
        self.assertEqual(result['processed'],1)
        self.assertEqual(result['failed'],1)
        self.assertFalse(result['checkpoint_advanced'])

    def test_manual_no_date_fallback_never_marks_window_complete(self):
        self.i._fetch_order_pages.side_effect=[iter([]),iter([[{'orderNumber':'1'}]])]
        self.i._import_order_record.side_effect=None
        self.i._import_order_record.return_value='processed'
        result=self.i._sync_orders(mode='manual')
        self.assertEqual(result['processed'],1)
        self.assertFalse(result['checkpoint_advanced'])
        self.assertTrue(any(call.kwargs.get('dated') is False for call in self.i._fetch_order_pages.call_args_list))

    def test_manual_button_reports_failure_as_warning(self):
        self.i._require_shipstation_admin=Mock()
        self.i._sync_orders=Mock(return_value={'processed':2,'failed':1,'stale':0})
        response=self.i.action_sync_orders()
        self.assertEqual(response['params']['type'],'warning')
        self.assertIn('1 failed',response['params']['message'])

class PaginationChecks(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance();self.i._format_ss_datetime=Mock(return_value='2026-11-05T00:00:00')

    def test_invalid_payload_rejected(self):
        self.i._ss_request=Mock(return_value={'orders':{}})
        with self.assertRaises(base.UserError):list(self.i._fetch_order_pages(dated=False))

    def test_page_cap_fails_closed(self):
        self.i._ss_request=Mock(return_value={'orders':[{}]*100})
        with self.assertRaises(base.UserError):list(self.i._fetch_order_pages(dated=False,max_pages=2))
        self.assertEqual(self.i._ss_request.call_count,2)

    def test_incremental_pages_are_bounded(self):
        self.i._ss_request=Mock(return_value={'orders':[{'orderId':1}]})
        pages=list(self.i._fetch_order_pages(datetime(2026,11,1),datetime(2026,11,5)))
        self.assertEqual(len(pages),1)
        self.assertEqual(self.i._ss_request.call_args.args[:2],('GET','/orders'))
        self.assertIn('modifyDateStart',self.i._ss_request.call_args.kwargs['params'])


class CrossCompanyReferenceChecks(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance()
        self.partner=Mock()
        self.product=Mock()
        self.product.with_company.return_value=self.product
        self.i.env={'res.partner':self.partner,'product.product':self.product}

    def test_customer_ambiguous_email_fails_without_creation(self):
        self.partner.search.return_value=[SimpleNamespace(id=1),SimpleNamespace(id=2)]
        with self.assertRaises(base.UserError): self.i._find_partner({'customerEmail':'demo@example.invalid'})
        self.partner.create.assert_not_called()
        self.assertIn(('company_id','=',3),self.partner.search.call_args.args[0])

    def test_product_duplicate_sku_fails_without_creation(self):
        self.product.search.return_value=[SimpleNamespace(id=1),SimpleNamespace(id=2)]
        with self.assertRaises(base.UserError): self.i._get_or_create_product('SKU1','Item',1)
        self.product.create.assert_not_called()
        self.assertIn(('company_id','=',3),self.product.search.call_args.args[0])

    def test_mirror_duplicate_id_fails_without_update(self):
        rec=order_mod.ShipStationOrderSync()
        rec._extract_listing_overrides_from_mapping=Mock(return_value={})
        rec.search=Mock(return_value=[SimpleNamespace(),SimpleNamespace()])
        rec.write=Mock()
        with self.assertRaises(base.UserError):
            rec._upsert_from_payload(with_store_helpers(SimpleNamespace(id=27,store_id='88')),{'orderId':120,'orderNumber':'SO120'})
        rec.write.assert_not_called()

    def test_repeat_label_purchase_blocked(self):
        rec=order_mod.ShipStationOrderSync()
        rec.ensure_one=Mock()
        # S07 now checks caller permissions before the purchase guard.
        rec._check_rate_operator=Mock()
        rec.instance_id=SimpleNamespace(id=27)
        rec.shipstation_shipment_id='SHIPMENT'
        rec.tracking_number=None
        rec.label_url=None
        rec._get_order_id_for_rates=Mock()
        with self.assertRaises(base.UserError): rec.action_create_label()
        rec._get_order_id_for_rates.assert_not_called()

    def test_imported_order_is_not_pushed_back(self):
        self.i._create_report=Mock()
        order=SimpleNamespace(shipstation_instance_id=SimpleNamespace(id=27),shipstation_order_id='120')
        sale=Mock();sale.with_company.return_value=sale;sale.search.return_value=[order];sale.search_count.return_value=1
        # Odoo 15: the open-delivery condition is a stock.picking sub-select (no 'any' operator).
        self.i.env={'sale.order':sale,'stock.picking':Mock()}
        self.i._ss_request=Mock()
        self.i._push_orders(mode='manual')
        self.i._ss_request.assert_not_called()

class RequestSafety(unittest.TestCase):
    def setUp(self):
        self.i=FakeInstance()

    def test_no_auto_retry_on_v1_post_503(self):
        response=Mock(status_code=503,text='{"message":"temporary"}')
        response.json.return_value={'message':'temporary'}
        with patch.object(base.requests,'request',return_value=response,create=True) as req:
            with self.assertRaises(base.UserError):self.i._ss_request('POST','/shipments/createlabel',data={'x':1})
        self.assertEqual(req.call_count,1)

    def test_no_auto_retry_on_v2_post_timeout(self):
        ex=base.requests.RequestException('timeout')
        with patch.object(base.requests,'request',side_effect=ex,create=True) as req:
            with self.assertRaises(base.UserError):self.i._ss_v2_request('POST','/labels',data={'x':1})
        self.assertEqual(req.call_count,1)

if __name__=='__main__':unittest.main(verbosity=2)
