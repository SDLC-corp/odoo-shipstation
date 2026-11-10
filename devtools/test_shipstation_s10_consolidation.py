"""S10 source checks for the two concrete integration-consistency defects."""
import ast
import pathlib
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT=pathlib.Path(__file__).resolve().parents[1]/"shipstation_connector"

def load_rate_action():
    tree=ast.parse((ROOT/"models/shipstation_rate.py").read_text(encoding="utf-8-sig"))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="ShipStationRate")
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="action_apply_rate")
    method.decorator_list=[]
    module=ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[]))
    env={"UserError":ValueError,"_":lambda message:message}
    exec(compile(module,"shipstation_rate.py","exec"),env)
    return env["action_apply_rate"]

class S10Consolidation(unittest.TestCase):
    def test_connections_filter_selected_uses_id_not_instance_id(self):
        js=(ROOT/"static/src/js/shipstation_operations_home.js").read_text()
        match=re.search(r'case "connections":(.*?)case "carriers":',js,re.S)
        self.assertIsNotNone(match)
        chunk=match.group(1)
        self.assertIn('domain = [...companyDomain]',chunk)
        self.assertIn('domain.push(["id", "=", data.instance_id])',chunk)
        self.assertNotIn('domain = scoped',chunk)

    def test_other_queues_keep_instance_domain(self):
        js=(ROOT/"static/src/js/shipstation_operations_home.js").read_text()
        self.assertIn('const scoped = [...companyDomain, ...instanceDomain];',js)
        self.assertIn('domain = [...scoped, ["link_state", "=", "pending"]]',js)

    def test_rate_selection_keeps_quote_current(self):
        action=load_rate_action()
        class Order:
            rate_ids=[]
            def __init__(self):
                self.current={"carrierCode":"ups","toCountry":"US","weight":{"value":10,"units":"ounces"}}
                self.saved=[]
            def _check_rate_operator(self): pass
            def _ensure_fresh_rate_quote(self): pass
            def _build_v1_rate_request(self): return dict(self.current)
            def _rate_fingerprint(self,params):return repr(sorted(params.items()))
            def write(self,vals):
                self.saved.append(vals)
                self.current.update({"carrierCode":vals["carrier_code"],"serviceCode":vals["service_code"],"packageCode":vals["package_code"]})
            def _rate_notice(self,title,message):return title
        order=Order()
        class Rate:
            carrier_code="ups";service_code="ups_ground";package_code="package";confirmation=False
            order_id=order
            def ensure_one(self): pass
        rate=Rate();order.rate_ids=[rate]
        result=action(rate)
        self.assertEqual(result,"Rate selected")
        self.assertEqual(len(order.saved),1)
        self.assertEqual(order.saved[0]["rate_request_hash"],order._rate_fingerprint(order._build_v1_rate_request()))
        self.assertEqual(order.saved[0]["service_code"],"ups_ground")

    def test_rate_method_checks_quote_before_write(self):
        src=(ROOT/"models/shipstation_rate.py").read_text()
        self.assertLess(src.index('order._ensure_fresh_rate_quote()'),src.index('order.write({'))

    def test_manifest_inventory_claims_match_safe_readonly_behavior(self):
        src=(ROOT/"__manifest__.py").read_text()
        self.assertNotIn('Export inventory quantities',src)
        self.assertIn('read-only reconciliation',src)

    def test_syntax(self):
        for p in ROOT.rglob('*.py'):
            ast.parse(p.read_text(encoding="utf-8-sig"))

if __name__=='__main__':unittest.main(verbosity=2)
