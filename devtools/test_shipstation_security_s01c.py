"""Offline security contract tests for S01C. No Odoo server or network needed."""
from __future__ import annotations
import ast
import importlib.util
import csv
from io import StringIO
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

ADMIN = 'shipstation_connector.group_shipstation_admin'
MANAGER = 'shipstation_connector.group_shipstation_manager'
READER = 'shipstation_connector.group_shipstation_user_readonly'


class SecurityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(cls.path)
        cls.mod=cls.root/'shipstation_connector'
        with (cls.mod/'security/ir.model.access.csv').open(encoding='utf-8-sig',newline='') as file:
            cls.acl_rows=list(csv.DictReader(file))
        cls.by_id={row['id']:row for row in cls.acl_rows}
        cls.security=ET.parse(cls.mod/'security/shipstation_security.xml').getroot()
        cls.rules=ET.parse(cls.mod/'security/shipstation_record_rules.xml').getroot()
        cls.menus=ET.parse(cls.mod/'views/menu.xml').getroot()

    def test_no_internal_user_wildcard_acl(self):
        self.assertTrue(self.acl_rows)
        self.assertFalse([r for r in self.acl_rows if r['group_id:id'] in ('', 'base.group_user')])

    def test_original_xml_ids_are_reused_to_revoke_old_grants(self):
        original=['instance','order_sync','rate','shipment_sync','product_sync','inventory',
                  'customer_sync','category_sync','attribute_sync','store_mapping','carrier_mapping',
                  'automation_rule','sync_log','field','field_mapping','report','report_line',
                  'ai_insight','dashboard']
        for name in original:
            row=self.by_id[f'access_shipstation_{name}']
            self.assertEqual(row['group_id:id'],ADMIN,name)
        old_readonly=self.by_id['access_shipstation_sync_log_readonly']
        self.assertEqual([old_readonly[k] for k in ('perm_read','perm_write','perm_create','perm_unlink')],['0']*4)

    def test_admin_and_manager_acl_distinction(self):
        self.assertEqual(self.by_id['access_shipstation_instance']['perm_write'],'1')
        for role in ('manager','reader'):
            row=self.by_id[f'access_shipstation_instance_{role}']
            self.assertEqual(row['perm_write'],'0')
            self.assertEqual(row['perm_create'],'0')
        self.assertNotIn('access_shipstation_dashboard_manager',self.by_id)
        self.assertNotIn('access_shipstation_dashboard_reader',self.by_id)
        for name in ('store_mapping','field_mapping','automation_rule','carrier_mapping'):
            self.assertNotIn(f'access_shipstation_{name}_manager',self.by_id)

    def test_reader_cannot_write_or_purchase_labels_by_acl(self):
        for row in self.acl_rows:
            if row['group_id:id']==READER:
                self.assertEqual((row['perm_write'],row['perm_create'],row['perm_unlink']),('0','0','0'))

    def test_company_rules_are_global_for_instance_and_children(self):
        rule_records=self.rules.findall('record')
        self.assertEqual(len(rule_records),25)  # S07 adds four protected fulfillment and returns models
        for record in rule_records:
            fields={field.get('name'):field.text for field in record.findall('field')}
            self.assertEqual(record.attrib.get('model'),'ir.rule')
            self.assertIn('company_ids',fields.get('domain_force') or '')
            self.assertNotIn('groups',fields)
        self.assertTrue(any(record.attrib.get('id')=='rule_shipstation_rate_company' for record in rule_records))
        self.assertTrue(any(record.attrib.get('id')=='rule_shipstation_instance_company' for record in rule_records))

    def test_role_hierarchy_maintains_existing_role_ids(self):
        ids={r.attrib.get('id') for r in self.security.findall('record')}
        self.assertTrue({'group_shipstation_admin','group_shipstation_manager',
                         'group_shipstation_user_readonly','base.group_system'}.issubset(ids))

    def test_instance_view_hides_admin_only_buttons_for_nonadmins(self):
        root=ET.parse(self.mod/'views/shipstation_instance_view.xml').getroot()
        actions=list(root.iter('button'))
        self.assertGreaterEqual(len(actions),8)
        for action in actions:
            self.assertEqual(action.get('groups'),ADMIN,action.get('name'))
        for page in root.iter('page'):
            self.assertEqual(page.get('groups'),ADMIN,page.get('string'))

    def test_menu_restrictions(self):
        menus={m.attrib['id']:m for m in self.menus.iter('menuitem')}
        self.assertEqual(menus['menu_shipstation_instances'].get('groups'),'group_shipstation_admin')
        self.assertEqual(menus['menu_shipstation_dashboard'].get('groups'),'group_shipstation_admin')
        self.assertEqual(menus['menu_shipstation_config'].get('groups'),'group_shipstation_admin')
        self.assertIn('group_shipstation_manager',menus['menu_shipstation_root'].get('groups'))

    def test_secret_fields_have_model_level_group_protection(self):
        code=(self.mod/'models/shipstation_instance.py').read_text(encoding='utf8')
        tree=ast.parse(code)
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ShipStationInstance')
        found={}
        for stmt in cls.body:
            if isinstance(stmt,ast.Assign) and isinstance(stmt.value,ast.Call):
                for target in stmt.targets:
                    if isinstance(target,ast.Name) and target.id in ('api_key','api_secret','v2_api_key'):
                        found[target.id]=next((k.value.value for k in stmt.value.keywords
                            if k.arg=='groups' and isinstance(k.value,ast.Constant)),None)
        self.assertEqual(found,{'api_key':ADMIN,'api_secret':ADMIN,'v2_api_key':ADMIN})

    def test_private_auth_boundary_restricts_operator_and_checks_company(self):
        source=(self.mod/'models/shipstation_sync_base.py').read_text(encoding='utf8')
        tree=ast.parse(source)
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='ShipStationSyncBase')
        methods={n.name:n for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
        method=ast.get_source_segment(source,methods['_get_shipstation_auth_record'])
        self.assertIn('self.check_access("read")',method)
        self.assertIn('group_shipstation_manager',method)
        self.assertIn('return self.sudo()',method)
        for name in ('_ss_request','_ss_v2_request','_ss_legacy_products_request'):
            self.assertIn('self._get_shipstation_auth_record()',ast.get_source_segment(source,methods[name]))

    def test_public_instance_actions_are_guarded(self):
        source=(self.mod/'models/shipstation_instance.py').read_text(encoding='utf8')
        tree=ast.parse(source)
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='ShipStationInstance')
        for node in cls.body:
            if isinstance(node,ast.FunctionDef) and node.name.startswith('action_'):
                body=ast.get_source_segment(source,node)
                if node.name=='action_test_connection':
                    self.assertIn('self.action_test_v1_connection()',body)
                else:
                    self.assertIn('self._require_shipstation_admin()',body,node.name)

    def test_dashboard_and_retry_have_server_authorization(self):
        dashboard=(self.mod/'models/shipstation_dashboard.py').read_text(encoding='utf8')
        for marker in ('    def get_dashboard_data(self):\n        self._require_shipstation_dashboard_admin()',
                       '    def generate_ai_insights(self, range=30):\n        self._require_shipstation_dashboard_admin()'):
            self.assertIn(marker,dashboard)
        log=(self.mod/'models/shipstation_sync_log.py').read_text(encoding='utf8')
        self.assertIn('self.check_access("write")',log)
        self.assertIn('group_shipstation_admin',log)

    def test_rules_are_loaded_from_manifest(self):
        manifest=(self.mod/'__manifest__.py').read_text(encoding='utf8')
        self.assertIn('"security/shipstation_record_rules.xml"',manifest)
        self.assertLess(manifest.index('"security/ir.model.access.csv"'),
                        manifest.index('"security/shipstation_record_rules.xml"'))


class RuntimeSecurityContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project=Path(SecurityContract.path)
        test_b=project/'devtools/test_shipstation_api_s01b.py'
        spec=importlib.util.spec_from_file_location('s01c_import_testb',test_b)
        contract=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contract)
        cls.base,cls.instance_mod,_=contract.load_modules(project/'shipstation_connector')

    def fake_instance(self,role,company_visible=True):
        parent=self.instance_mod.ShipStationInstance
        base=self.base.ShipStationSyncBase
        class FakeInstance(parent,base):
            _name='not.shipstation.instance'
            api_key='fake-v1-key'
            api_secret='fake-v1-secret'
            v2_api_key='fake-v2-key'
            base_url='https://api.shipstation.com'
            def ensure_one(self): return None
            def sudo(self): return self
            def check_access(self,operation):
                if not company_visible:
                    raise self_module.AccessError('Company record rule denied')
                return None
        self_module=self.base
        instance=FakeInstance()
        user=SimpleNamespace(has_group=lambda group: (role=='admin' or
            (role=='manager' and group==MANAGER)))
        instance.env=SimpleNamespace(su=False,user=user)
        return instance

    def test_readonly_user_cannot_send_v1_api_request(self):
        instance=self.fake_instance('reader')
        with patch.object(self.base.requests,'request',create=True) as send:
            with self.assertRaises(self.base.AccessError):
                instance._ss_request('POST','/orders/createorder',data={'orderNumber':'X'})
        send.assert_not_called()

    def test_readonly_user_cannot_use_public_connection_action(self):
        instance=self.fake_instance('reader')
        with patch.object(self.base.requests,'request',create=True) as send:
            with self.assertRaises(self.base.AccessError):
                instance.action_test_v1_connection()
        send.assert_not_called()

    def test_manager_cannot_use_admin_only_instance_action(self):
        instance=self.fake_instance('manager')
        with patch.object(self.base.requests,'request',create=True) as send:
            with self.assertRaises(self.base.AccessError):
                instance.action_test_v2_connection()
        send.assert_not_called()

    def test_manager_can_call_api_after_company_visibility_checked(self):
        instance=self.fake_instance('manager')
        response=Mock(status_code=200,text='{"orders": []}')
        response.json.return_value={'orders':[]}
        with patch.object(self.base.requests,'request',return_value=response,create=True) as send:
            self.assertEqual(instance._ss_request('GET','/orders'),{'orders':[]})
        self.assertEqual(send.call_args.kwargs['auth'],('fake-v1-key','fake-v1-secret'))

    def test_manager_wrong_company_denied_before_api(self):
        instance=self.fake_instance('manager',company_visible=False)
        with patch.object(self.base.requests,'request',create=True) as send:
            with self.assertRaises(self.base.AccessError):
                instance._ss_request('GET','/orders')
        send.assert_not_called()


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: python -B test_shipstation_security_s01c.py <project-root>')
    SecurityContract.path=sys.argv[1]
    sys.argv[:]=[sys.argv[0]]
    unittest.main(verbosity=2)
