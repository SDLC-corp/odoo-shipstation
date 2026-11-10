"""S09 isolated contract tests. They do not contact Odoo or ShipStation."""
from pathlib import Path
from types import SimpleNamespace
import ast
import unittest
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
ADDON=ROOT/'shipstation_connector'
class AccessError(Exception):pass
class UserError(Exception):pass

def load_method(filename,class_name,method_name):
    tree=ast.parse((ADDON/'models'/filename).read_text(encoding='utf-8'))
    klass=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==class_name)
    fn=next(n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name==method_name)
    fn.decorator_list=[]
    mod=ast.Module(body=[fn],type_ignores=[])
    ast.fix_missing_locations(mod)
    ns={'AccessError':AccessError,'UserError':UserError,'_':lambda s:s}
    exec(compile(mod,'s09_isolated','exec'),ns)
    return ns[method_name]

class FakeConnection:
    def __init__(self,**kwargs):
        attrs=dict(active=True,company_id=SimpleNamespace(id=1),api_key='***',api_secret='***',v2_api_key='***',store_id='123',
                   cron_sync_inventory=False,inventory_warehouse_id=None,
                   webhook_order_create=False,webhook_order_update=False,webhook_shipment_update=False,
                   webhook_target_url=False,webhook_token='***')
        attrs.update(kwargs)
        self.__dict__.update(attrs)
        self.env=SimpleNamespace(su=False,user=SimpleNamespace(has_group=lambda g: True))
    def ensure_one(self):pass
    def _require_shipstation_admin(self):
        if not self.env.user.has_group('shipstation_connector.group_shipstation_admin'):
            raise AccessError('Admin only')

class S09SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnostic=staticmethod(load_method('shipstation_setup_assistant.py','ShipStationSetupAssistant','_get_local_setup_diagnostic'))
        cls.friendly=staticmethod(load_method('shipstation_instance.py','ShipStationInstance','_friendly_connection_issue'))
        cls.test_v1=staticmethod(load_method('shipstation_instance.py','ShipStationInstance','action_test_v1_connection'))

    def test_missing_v1_is_blocker(self):
        r=self.diagnostic(FakeConnection(api_key='',api_secret=''))
        self.assertEqual(r['state'],'blocked')
        self.assertIn('V1 API key',r['next_step'])

    def test_ready_is_only_local_and_no_api_call(self):
        r=self.diagnostic(FakeConnection(webhook_order_update=True,webhook_target_url='https://example.test/webhook'))
        self.assertEqual(r['state'],'ready')
        self.assertIn('Local checks only',r['checklist'])
        self.assertNotIn('https://example.test/webhook',r['checklist'])

    def test_optional_v2_is_not_a_blocker(self):
        r=self.diagnostic(FakeConnection(v2_api_key=''))
        self.assertEqual(r['state'],'attention')
        self.assertIn('V2 key',r['next_step'])

    def test_inventory_without_warehouse_is_blocker(self):
        r=self.diagnostic(FakeConnection(cron_sync_inventory=True))
        self.assertEqual(r['state'],'blocked')
        self.assertIn('warehouse',r['next_step'])

    def test_warehouse_cross_company_is_blocker(self):
        r=self.diagnostic(FakeConnection(inventory_warehouse_id=SimpleNamespace(company_id=SimpleNamespace(id=99))))
        self.assertEqual(r['state'],'blocked')

    def test_webhook_enabled_without_https_target_reminder(self):
        r=self.diagnostic(FakeConnection(webhook_order_update=True))
        self.assertIn('HTTPS',r['checklist'])
        self.assertEqual(r['state'],'attention')

    def test_disabled_connector_is_blocker(self):
        r=self.diagnostic(FakeConnection(active=False))
        self.assertEqual(r['state'],'blocked')

    def test_diagnostic_rejects_non_admin(self):
        obj=FakeConnection()
        obj.env.user.has_group=lambda g:False
        with self.assertRaises(AccessError):self.diagnostic(obj)

    def test_credentials_not_echoed(self):
        creds='SECRET-AAA-bbb-999'
        r=self.diagnostic(FakeConnection(api_key=creds,api_secret=creds,v2_api_key=creds,webhook_token=creds))
        self.assertNotIn(creds,' '.join(str(x) for x in r.values()))

    def test_friendly_error_never_contains_raw_remote_payload(self):
        err=UserError('ShipStation API error 401 on /stores: customer email secret@example.test')
        message=self.friendly('V1',err)
        self.assertIn('rejected',message)
        self.assertNotIn('secret@example.test',message)

    def test_nonstandard_errors_are_generic(self):
        message=self.friendly('V2',UserError('network: API-Key=secret123'))
        self.assertNotIn('secret123',message)
        self.assertIn('Verify credentials',message)

    def test_v1_connection_test_wraps_error_and_no_second_call(self):
        obj=FakeConnection()
        obj._friendly_connection_issue=self.friendly
        calls=[]
        def failing(method,path):
            calls.append((method,path));raise UserError('ShipStation API error 403 on /stores: TOKEN:SECRET')
        obj._ss_request=failing
        with self.assertRaises(UserError) as ex:self.test_v1(obj)
        self.assertEqual(calls,[('GET','/stores')])
        self.assertNotIn('TOKEN:SECRET',str(ex.exception))

    def test_source_and_view_integration(self):
        model=(ADDON/'models/shipstation_setup_assistant.py').read_text()
        self.assertNotIn('requests.',model)
        self.assertNotIn('._ss_request(',model)
        self.assertNotIn('.write(',model)
        self.assertNotIn('.sudo(',model)
        manifest=(ADDON/'models/__init__.py').read_text()
        self.assertIn('from . import shipstation_setup_assistant',manifest)
        root=ET.parse(ADDON/'views/shipstation_instance_view.xml').getroot()
        for field in ('setup_state','setup_summary','setup_checklist','setup_next_step'):
            self.assertTrue(any(el.get('name')==field for el in root.iter('field')))
        buttons=[el for el in root.iter('button') if el.get('name')=='action_check_local_setup']
        self.assertEqual(len(buttons),1)
        self.assertIn('group_shipstation_admin',buttons[0].get('groups'))
        self.assertTrue(any(el.get('string')=='Setup Checklist' for el in root.iter('page')))

    def test_operations_home_setup_link_admin_only(self):
        js=(ADDON/'static/src/js/shipstation_operations_home.js').read_text()
        xml=(ADDON/'static/src/xml/shipstation_operations_home.xml').read_text()
        self.assertIn('case "connections":',js)
        self.assertIn('if (!data.can_admin) return;',js)
        self.assertIn('domain = scoped; break;',js)
        self.assertIn('t-if="this.state.data.can_admin"',xml)  # Owl 3 (Odoo 20) template scope
        self.assertIn('this.openQueue(\'connections\')',xml)

    def test_python_xml_static_parsing(self):
        py=xml=0
        for file in ADDON.rglob('*.py'):
            ast.parse(file.read_text(encoding='utf-8-sig'));py+=1
        for file in ADDON.rglob('*.xml'):
            ET.parse(file);xml+=1
        # Everything must parse; the counts are floors (tests/ added Python files in S13).
        self.assertGreaterEqual(py,43)
        self.assertGreaterEqual(xml,26)

if __name__=='__main__': unittest.main(verbosity=2)
