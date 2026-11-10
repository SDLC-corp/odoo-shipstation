"""Explicit Void Label (refund request): guard rails, every outcome, idempotency and recovery."""
import ast
import pathlib

import requests
from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .common import ShipStationCommon
from .test_label_flow import LABEL_OK

VOID_OK = {"approved": True, "message": "Label voided successfully"}
MODULE_DIR = pathlib.Path(__file__).resolve().parents[1]


@tagged("post_install", "-at_install", "shipstation")
class TestVoidLabel(ShipStationCommon):

    def _bought(self, with_delivery=False):
        """An order for which this connector bought a (mocked) label."""
        if with_delivery:
            self.link_sale_to_shipstation(self.make_confirmed_sale())
        order = self.make_order_sync(carrier_code="ups", service_code="ups_ground", package_code="package",
                                     rate_weight=16, rate_weight_units="ounces")
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        order.action_create_label()
        self.assertEqual(order.label_state, "created")
        if with_delivery:
            self.instance._process_shipment_record(self.shipment_payload(71001, tracking="1ZTEST000"))
        self.http.reset()
        return order

    def _enable(self):
        self.instance.write({"allow_label_void": True})

    def _void(self, order, user=None):
        return (order.with_user(user) if user else order).action_void_label()

    # ---- guard rails ------------------------------------------------------------------------
    def test_off_by_default_and_nothing_is_sent(self):
        order = self._bought()
        self.assertFalse(self.instance.allow_label_void)
        error = self.attempt(lambda: self._void(order, self.user_admin), UserError)
        self.assertIn("switched off", str(error))
        self.assertEqual(order.label_state, "created")
        self.assertFalse(self.http.calls)

    def test_only_administrators_even_when_enabled(self):
        order = self._bought()
        self._enable()
        for user in (self.user_manager, self.user_reader, self.user_plain, self.user_portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self._void(order, user)
        self.assertFalse(self.http.calls)

    def test_the_switch_is_invisible_to_non_administrators(self):
        for user in (self.user_manager, self.user_reader):
            self.assertNotIn("allow_label_void", self.instance.with_user(user).fields_get())
        self.assertIn("allow_label_void", self.instance.with_user(self.user_admin).fields_get())

    def test_only_a_label_bought_here_and_still_active_can_be_voided(self):
        self._enable()
        fresh = self.make_order_sync(shipstation_order_id="9100", shipstation_order_number="SO-9100")
        self.attempt(lambda: self._void(fresh), UserError)
        self.assertFalse(self.http.calls)

    def test_exactly_one_code_path_can_reach_the_void_endpoint(self):
        """No bulk action, cron job, webhook or sync may ever be able to request a refund."""
        owners = []
        for path in (MODULE_DIR / "models", MODULE_DIR / "controllers", MODULE_DIR / "services"):
            for file in path.glob("*.py"):
                tree = ast.parse(file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if any(isinstance(c, ast.Constant) and isinstance(c.value, str) and "voidlabel" in c.value
                               for c in ast.walk(node)):
                            owners.append("%s:%s" % (file.name, node.name))
        self.assertEqual(owners, ["shipstation_order_sync.py:action_void_label"])

    def test_bulk_and_background_work_never_requests_a_void(self):
        self._enable()
        order = self._bought()
        self.http.add("GET", "/orders", {"orders": []})
        self.http.add("GET", "/shipments", {"shipments": []})
        batch_action = order.with_user(self.user_manager).action_create_fulfillment_batch()
        self.env["shipstation.fulfillment.batch"].browse(batch_action["res_id"]).with_user(
            self.user_manager).action_refresh_readiness()
        self.env["shipstation.instance"].run_cron_sync_orders()
        self.env["shipstation.instance"].run_cron_sync_shipments()
        self.assertEqual(order.label_state, "created")
        self.assertFalse(self.http.calls_to("POST", "/"))

    # ---- outcomes -----------------------------------------------------------------------------
    def test_approved_void_frees_the_order_the_delivery_and_allows_a_replacement(self):
        self._enable()
        order = self._bought(with_delivery=True)
        picking = order.instance_id._find_order("9001", "SO-9001", "111").picking_ids
        self.assertEqual(picking.carrier_tracking_ref, "1ZTEST000")
        self.http.add("POST", "/shipments/voidlabel", VOID_OK)

        result = self._void(order, self.user_admin)

        self.assertEqual(result["params"]["type"], "success")
        call = self.http.calls_to("POST", "/shipments/voidlabel")
        self.assertEqual(len(call), 1)
        self.assertEqual((call[0]["host"], call[0]["json"]), ("ssapi.shipstation.com", {"shipmentId": 71001}))
        self.assertEqual(order.label_state, "voided")
        self.assertFalse(order.shipstation_shipment_id or order.tracking_number or order.label_url)
        self.assertEqual((order.last_void_shipment_id, order.last_void_tracking_number), ("71001", "1ZTEST000"))
        self.assertEqual(order.last_void_by_id, self.user_admin)
        self.assertTrue(order.last_void_at)
        shipment = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "71001")])
        self.assertTrue(shipment.voided)
        self.assertEqual(shipment.link_state, "review", "a linked label that is voided still needs a person")
        self.assertFalse(picking.carrier_tracking_ref, "tracking the connector wrote is removed")
        # the refunded label can be replaced, and only then
        self.http.add("POST", "/orders/createlabelfororder", dict(LABEL_OK, shipmentId=71002, trackingNumber="1ZNEW"))
        order.action_create_label()
        self.assertEqual((order.label_state, order.shipstation_shipment_id, order.tracking_number),
                         ("created", "71002", "1ZNEW"))
        self.assertEqual((order.last_void_shipment_id, order.last_void_tracking_number), ("71001", "1ZTEST000"),
                         "the audit trail of the first label is kept")

    def test_a_second_click_is_harmless_and_sends_nothing(self):
        self._enable()
        order = self._bought()
        self.http.add("POST", "/shipments/voidlabel", VOID_OK)
        self._void(order)
        self.attempt(lambda: self._void(order), UserError)
        self.assertEqual(len(self.http.calls_to("POST", "/shipments/voidlabel")), 1)

    def test_a_refusal_restores_the_label(self):
        self._enable()
        order = self._bought()
        self.http.add("POST", "/shipments/voidlabel", {"approved": False, "message": "Label already used"})
        error = self.attempt(lambda: self._void(order), UserError)
        self.assertIn("Label already used", str(error))
        self.assertEqual((order.label_state, order.shipstation_shipment_id), ("created", "71001"))

    def test_a_definite_http_error_restores_the_label(self):
        self._enable()
        order = self._bought()
        self.http.add("POST", "/shipments/voidlabel", {"Message": "not found"}, status=404)
        self.attempt(lambda: self._void(order), UserError)
        self.assertEqual((order.label_state, order.shipstation_shipment_id), ("created", "71001"))

    def test_an_unclear_outcome_locks_the_order_until_an_admin_resets_it(self):
        self._enable()
        order = self._bought()
        self.http.add("POST", "/shipments/voidlabel", exc=requests.Timeout("no answer"))
        error = self.attempt(lambda: self._void(order), UserError)
        self.assertIn("Do NOT retry", str(error))
        self.assertEqual(order.label_state, "void_requested")
        before = len(self.http.calls)
        self.attempt(lambda: self._void(order), UserError)                     # no second request
        self.attempt(order.action_create_label, UserError)                     # and no new purchase either
        self.assertEqual(len(self.http.calls), before)
        with self.assertRaises(AccessError):
            order.with_user(self.user_manager).action_reset_label_request()
        order.with_user(self.user_admin).action_reset_label_request()
        self.assertEqual((order.label_state, order.shipstation_shipment_id), ("created", "71001"))

    def test_a_reply_without_a_verdict_is_treated_as_unclear(self):
        self._enable()
        order = self._bought()
        self.http.add("POST", "/shipments/voidlabel", {"something": "else"})
        self.attempt(lambda: self._void(order), UserError)
        self.assertEqual(order.label_state, "void_requested")

    # ---- voids made elsewhere ---------------------------------------------------------------------
    def test_a_void_done_in_shipstation_frees_the_order_through_the_sync(self):
        order = self._bought(with_delivery=True)
        self.instance._process_shipment_record(self.shipment_payload(71001, tracking="1ZTEST000", voided=True))
        self.assertEqual(order.label_state, "voided")
        self.assertFalse(order.shipstation_shipment_id or order.tracking_number)
        self.assertFalse(order.last_void_by_id, "nobody in Odoo requested it")
        self.assertFalse(self.http.calls)

    def test_a_void_found_by_the_sync_resolves_an_unclear_request(self):
        self._enable()
        order = self._bought()
        self.http.add("POST", "/shipments/voidlabel", exc=requests.Timeout("no answer"))
        self.attempt(lambda: self._void(order), UserError)
        self.assertEqual(order.label_state, "void_requested")
        self.instance._process_shipment_record(self.shipment_payload(71001, tracking="1ZTEST000", voided=True))
        self.assertEqual(order.label_state, "voided")

    # ---- views -----------------------------------------------------------------------------------------
    def test_the_button_is_for_administrators_when_the_switch_is_on(self):
        view = self.env.ref("shipstation_connector.view_shipstation_order_sync_form")
        names = lambda user: [b.get("name") for b in etree.fromstring(  # noqa: E731
            self.env["shipstation.order.sync"].with_user(user).get_view(view.id, "form")["arch"]).iter("button")]
        self.assertIn("action_void_label", names(self.user_admin))
        self.assertNotIn("action_void_label", names(self.user_manager))
        self.assertNotIn("action_void_label", names(self.user_reader))
