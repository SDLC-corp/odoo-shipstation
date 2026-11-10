"""S08 lightweight source and isolated data contract tests. No Odoo or ShipStation connection."""
from pathlib import Path
from types import SimpleNamespace
import ast
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "shipstation_connector"

class AccessError(Exception):
    pass

class FakeModel:
    def __init__(self, name):
        self.name = name
        self.query_calls = []
    def check_access(self, access):
        assert access == "read"
    def search_count(self, domain):
        self.query_calls.append(("count", tuple(domain)))
        return 4
    def search(self, domain, order=None, limit=None):
        self.query_calls.append(("search", tuple(domain), order, limit))
        return []

class FakeInstance:
    def __init__(self, ident, company):
        self.id = ident
        self.company_id = company
        self.name = "Test Connection"
    def check_access(self, mode):
        assert mode == "read"
    def exists(self):
        return self

class FakeRoot:
    def __init__(self, company_id=1, role="reader", instances=None):
        company = SimpleNamespace(id=company_id, name="Staging Company")
        user = SimpleNamespace(id=44, has_group=lambda group: (
            (role == "admin") or
            (role == "manager" and group.endswith(("manager", "user_readonly"))) or
            (role == "reader" and group.endswith("user_readonly"))
        ))
        self.instances = instances or []
        self.models = {key: FakeModel(key) for key in (
            "shipstation.shipment.sync", "shipstation.fulfillment.batch",
            "shipstation.return.case", "shipstation.order.sync", "shipstation.sync.log")}
        class Env:
            def __init__(self,company,user,models):
                self.company=company; self.user=user; self.su=False; self.models=models
            def __getitem__(self,key):return self.models[key]
        self.env=Env(company,user,self.models)
    def search(self, domain, order, limit):
        self.instance_domain=domain
        return self.instances
    def browse(self, ident):
        return next((i for i in self.instances if i.id==ident), None) or EmptyRecord()

class EmptyRecord:
    def exists(self):return False

class SourceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=ADDON/"models/shipstation_operations_home.py"
        tree=ast.parse(path.read_text())
        model=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=="ShipStationOperationsHome")
        fn=next(x for x in model.body if isinstance(x,ast.FunctionDef) and x.name=="get_operations_overview")
        fn.decorator_list=[]
        module=ast.Module(body=[fn],type_ignores=[])
        ast.fix_missing_locations(module)
        ns={"AccessError":AccessError,"_":lambda s:s}
        exec(compile(module,"s08_runtime_isolated","exec"),ns)
        cls.method=staticmethod(ns["get_operations_overview"])

    def test_reader_allowed_without_logs(self):
        obj=FakeRoot(role="reader")
        result=self.method(obj)
        self.assertFalse(result["can_manage"])
        self.assertEqual(result["metrics"]["failed_syncs"],0)
        self.assertEqual(obj.models["shipstation.sync.log"].query_calls,[])

    def test_nonmember_denied_before_query(self):
        obj=FakeRoot(role="none")
        with self.assertRaises(AccessError):self.method(obj)
        self.assertEqual(obj.models["shipstation.shipment.sync"].query_calls,[])

    def test_wrong_company_instance_denied(self):
        obj=FakeRoot(instances=[FakeInstance(7,SimpleNamespace(id=2))])
        with self.assertRaises(AccessError):self.method(obj,7)

    def test_missing_instance_denied(self):
        obj=FakeRoot()
        with self.assertRaises(AccessError):self.method(obj,99)

    def test_non_integer_instance_denied(self):
        obj=FakeRoot()
        for identifier in ("1", -1, 1.5, True):
            with self.subTest(identifier=identifier):
                with self.assertRaises(AccessError):self.method(obj,identifier)

    def test_selected_company_instance_limits_every_query(self):
        obj=FakeRoot()
        matching=FakeInstance(5,obj.env.company)
        obj.instances=[matching]
        result=self.method(obj,5)
        self.assertEqual(result["instance_id"],5)
        for name,model in obj.models.items():
            if name == "shipstation.sync.log":continue
            for query in model.query_calls:
                self.assertIn(("company_id","=",1),query[1])
                self.assertIn(("instance_id","=",5),query[1])

    def test_manager_logs_scoped_to_company(self):
        obj=FakeRoot(role="manager")
        result=self.method(obj)
        self.assertTrue(result["can_manage"])
        self.assertFalse(result["can_admin"])
        self.assertIn(("instance_id.company_id","=",1),obj.models["shipstation.sync.log"].query_calls[0][1])
        self.assertEqual(result["metrics"]["failed_syncs"],4)

    def test_admin_access(self):
        result=self.method(FakeRoot(role="admin"))
        self.assertTrue(result["can_admin"])

    def test_no_sudo_no_external_or_writes(self):
        code=(ADDON/"models/shipstation_operations_home.py").read_text()
        for forbidden in (".sudo(","_ss_request(","requests.",".write(",".create(","stock.quant", "action_create_label"):
            self.assertNotIn(forbidden,code)

    def test_limits_detail_lists(self):
        code=(ADDON/"models/shipstation_operations_home.py").read_text()
        self.assertIn('limit=8',code)
        self.assertIn('limit=6',code)
        self.assertIn('limit=5',code)

    def test_js_client_action_registered(self):
        js=(ADDON/"static/src/js/shipstation_operations_home.js").read_text()
        self.assertIn('registry.category("actions").add("shipstation_connector.operations_home"',js)
        self.assertIn('useService("orm")',js)
        self.assertIn('useService("action")',js)
        self.assertIn('get_operations_overview',js)

    def test_js_navigation_company_scoped(self):
        js=(ADDON/"static/src/js/shipstation_operations_home.js").read_text()
        self.assertIn('const companyDomain = [["company_id", "=", data.company_id]]',js)
        self.assertIn('instanceDomain',js)
        self.assertIn('target: "current"',js)
        self.assertNotIn('action_create_label(',js)

    def test_templates_valid_and_keyboard_controls(self):
        root=ET.parse(ADDON/"static/src/xml/shipstation_operations_home.xml").getroot()
        button_list=list(root.iter("button"))
        self.assertGreaterEqual(len(button_list),13)
        self.assertTrue(all(b.get('type')=='button' for b in button_list))
        self.assertTrue(any(b.get('aria-label')=='Refresh shipping overview' for b in button_list))
        self.assertTrue(any(b.get('t-foreach')=='state.data.shipments' for b in root.iter('t')))

    def test_kanban_and_actions_present(self):
        xml=ET.parse(ADDON/"views/shipstation_s08_operations_views.xml").getroot()
        ids={r.get('id') for r in xml.iter('record')}
        for name in ('action_shipstation_s08_operations_home','action_shipstation_s08_exception_board','view_shipstation_s08_exception_kanban','view_shipstation_s08_return_search','view_shipstation_s08_batch_search'):
            self.assertIn(name,ids)
        self.assertTrue(list(xml.iter('kanban')))
        self.assertTrue(list(xml.iter('filter')))

    def test_module_assets_and_menu_wired(self):
        manifest=(ADDON/'__manifest__.py').read_text()
        menu=ET.parse(ADDON/'views/menu.xml').getroot()
        self.assertTrue(any(e.get('action')=='action_shipstation_s08_operations_home' for e in menu.iter('menuitem')))
        self.assertTrue(any(e.get('action')=='action_shipstation_s08_exception_board' for e in menu.iter('menuitem')))
        for file in ('shipstation_s08_operations_views.xml','shipstation_operations_home.js','shipstation_operations_home.xml','shipstation_operations_home.css'):
            self.assertIn(file,manifest)

    def test_all_python_xml_parse(self):
        py_count=xml_count=0
        for f in ADDON.rglob('*.py'):
            ast.parse(f.read_text(encoding='utf-8-sig'));py_count+=1
        for f in ADDON.rglob('*.xml'):
            ET.parse(f);xml_count+=1
        self.assertGreaterEqual(py_count,42)
        self.assertGreaterEqual(xml_count,26)

if __name__=='__main__':unittest.main(verbosity=2)
