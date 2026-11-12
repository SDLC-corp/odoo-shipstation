"""Paid-label safety: correct endpoint, explicit single purchase, idempotency, ambiguity, permissions."""
import base64

import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .common import FAKE_LABEL_B64, FAKE_PDF, FakeResponse, ShipStationCommon

LABEL_OK = {
    "shipmentId": 71001, "shipmentCost": 8.5, "insuranceCost": 0.0,
    "trackingNumber": "1ZTEST000", "labelData": FAKE_LABEL_B64, "formData": None,
}


@tagged("post_install", "-at_install", "shipstation")
class TestLabelPurchase(ShipStationCommon):

    def _ready_order(self, **extra):
        vals = dict(carrier_code="ups", service_code="ups_ground", package_code="package",
                    rate_weight=16.0, rate_weight_units="ounces",
                    rate_length=10, rate_width=8, rate_height=4)
        vals.update(extra)
        return self.make_order_sync(**vals)

    def _buy(self, order, user=None):
        return (order.with_user(user) if user else order).action_create_label()

    # ---- the happy path ---------------------------------------------------------
    def test_label_uses_the_order_endpoint_and_documented_body(self):
        order = self._ready_order(confirmation="delivery")
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        result = self._buy(order, self.user_manager)
        self.assertEqual(result["params"]["type"], "success")

        calls = self.http.calls_to("POST", "/")
        self.assertEqual([c["path"] for c in calls], ["/orders/createlabelfororder"])
        self.assertEqual(calls[0]["host"], "ssapi.shipstation.com")
        body = calls[0]["json"]
        self.assertEqual(body["orderId"], 9001)
        self.assertIsInstance(body["orderId"], int)
        self.assertEqual((body["carrierCode"], body["serviceCode"], body["packageCode"]),
                         ("ups", "ups_ground", "package"))
        self.assertEqual(body["confirmation"], "delivery")
        self.assertRegex(body["shipDate"], r"^\d{4}-\d\d-\d\d$")
        self.assertIs(body["testLabel"], False, "a test label must never be requested implicitly")
        self.assertEqual(body["weight"], {"value": 16.0, "units": "ounces"})
        self.assertEqual(body["dimensions"]["units"], "inches")

    def test_confirmation_defaults_to_none_because_the_api_requires_it(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        self._buy(order)
        self.assertEqual(self.http.calls_to("POST", "/orders/createlabelfororder")[0]["json"]["confirmation"], "none")

    def test_result_is_recorded_and_pdf_attached_to_order_and_delivery(self):
        sale = self.link_sale_to_shipstation(self.make_confirmed_sale())
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        self._buy(order)
        self.assertEqual((order.shipstation_shipment_id, order.tracking_number, order.label_state),
                         ("71001", "1ZTEST000", "created"))
        self.assertNotIn("labelData", order.payload, "the bulky base64 label must not be stored in the payload")
        attachments = self.env["ir.attachment"].search([("name", "like", "ShipStation_Label_")])
        self.assertEqual(sorted(attachments.mapped("res_model")), ["shipstation.order.sync", "stock.picking"])
        for attachment in attachments:
            self.assertEqual(base64.b64decode(attachment.datas), FAKE_PDF)
        self.assertEqual(attachments.filtered(lambda a: a.res_model == "stock.picking").res_id,
                         sale.picking_ids.id)
        mirror = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "71001")])
        self.assertEqual(mirror.tracking_number, "1ZTEST000")
        self.assertEqual(order.action_open_label()["url"].split("?")[0].rsplit("/", 1)[0], "/web/content")

    def test_non_pdf_label_data_is_ignored_not_stored(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder",
                      dict(LABEL_OK, labelData=base64.b64encode(b"<script>x</script>").decode()))
        self._buy(order)
        self.assertEqual(order.label_state, "created")
        self.assertFalse(self.env["ir.attachment"].search([("name", "like", "ShipStation_Label_")]))

    # ---- idempotency ------------------------------------------------------------------
    def test_second_click_never_buys_a_second_label(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        self._buy(order)
        with self.assertRaises(UserError):
            self._buy(order)
        self.assertEqual(len(self.http.calls_to("POST", "/orders/createlabelfororder")), 1)

    def test_concurrent_claim_blocks_a_parallel_request(self):
        order = self._ready_order()
        order.invalidate_cache(ids=order.ids)
        order._claim_label_request()                       # first clicker wins the claim
        error = self.attempt(order._claim_label_request, UserError)   # second click is refused
        self.assertIn("outcome is unknown", str(error))
        self.assertFalse(self.http.calls)

    # ---- failure semantics -------------------------------------------------------------
    def test_definitive_rejection_is_marked_failed_and_may_be_retried(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", {"Message": "invalid service"}, status=400)
        self.attempt(lambda: self._buy(order), UserError)
        self.assertEqual(order.label_state, "failed")
        self.http.reset()
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        self._buy(order)
        self.assertEqual(order.label_state, "created")

    def test_ambiguous_failure_locks_the_order_until_an_admin_resets_it(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", exc=requests.Timeout("no answer"))
        error = self.attempt(lambda: self._buy(order), UserError)
        self.assertIn("Do NOT retry", str(error))
        self.assertEqual(order.label_state, "requested")
        before = len(self.http.calls)
        self.attempt(lambda: self._buy(order), UserError)  # blocked without touching the network
        self.assertEqual(len(self.http.calls), before)
        with self.assertRaises(AccessError):
            order.with_user(self.user_manager).action_reset_label_request()
        order.with_user(self.user_admin).action_reset_label_request()
        self.assertEqual(order.label_state, "none")

    def test_a_server_error_is_treated_as_ambiguous(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", {"Message": "oops"}, status=502)
        self.attempt(lambda: self._buy(order), UserError)
        self.assertEqual(order.label_state, "requested")
        self.assertEqual(len(self.http.calls_to("POST", "/orders/createlabelfororder")), 1, "no retry of a paid POST")

    def test_response_without_a_shipment_is_not_trusted(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", {"unexpected": True})
        self.attempt(lambda: self._buy(order), UserError)
        self.assertEqual(order.label_state, "requested")
        self.assertFalse(order.shipstation_shipment_id)

    def test_a_non_positive_shipment_id_is_not_recorded_as_a_shipment(self):
        """Seen on the real API: a test label returns shipmentId -1. No shipment exists, so nothing may be
        recorded under that id or later sent to the void endpoint."""
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", dict(LABEL_OK, shipmentId=-1))
        self._buy(order)
        self.assertEqual(order.label_state, "created")
        self.assertEqual(order.tracking_number, "1ZTEST000")
        self.assertFalse(order.shipstation_shipment_id)
        self.assertFalse(self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "in", ["-1", "0"])]))

    def test_reset_is_refused_once_a_shipment_exists(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        self._buy(order)
        with self.assertRaises(UserError):
            order.with_user(self.user_admin).action_reset_label_request()

    # ---- preconditions and permissions ----------------------------------------------------
    def test_preconditions_block_the_purchase_before_any_request(self):
        cases = {
            "no service": dict(service_code=False),
            "no package": dict(package_code=False),
            "not awaiting shipment": dict(status="shipped"),
            "non-numeric remote id": dict(shipstation_order_id="abc"),
        }
        for name, vals in cases.items():
            with self.subTest(name):
                order = self._ready_order(shipstation_order_number="SO-" + name, **vals)
                with self.assertRaises(UserError):
                    self._buy(order)
        self.assertFalse(self.http.calls)

    def test_only_manager_or_admin_can_buy_and_company_is_enforced(self):
        order = self._ready_order()
        for user in (self.user_reader, self.user_plain, self.user_portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self._buy(order, user)
        foreign = self.make_order_sync(self.instance_b, carrier_code="ups", service_code="s", package_code="p")
        with self.assertRaises(AccessError):
            self._buy(foreign, self.user_manager)
        self.assertFalse(self.http.calls)

    def test_no_bulk_or_background_path_can_buy_a_label(self):
        order = self._ready_order(shipstation_order_number="SO-BULK")
        batch_action = order.with_user(self.user_manager).action_create_fulfillment_batch()
        batch = self.env["shipstation.fulfillment.batch"].browse(batch_action["res_id"])
        batch.with_user(self.user_manager).action_refresh_readiness()
        self.http.add("GET", "/orders", {"orders": []})
        self.http.add("GET", "/shipments", {"shipments": []})
        self.env["shipstation.instance"].run_cron_sync_orders()
        self.env["shipstation.instance"].run_cron_sync_shipments()
        self.assertEqual(order.label_state, "none")
        self.assertFalse(self.http.calls_to("POST", "/"), "bulk and background actions may never POST")

    def test_v1_label_endpoint_for_non_order_shipments_is_never_used(self):
        order = self._ready_order()
        self.http.add("POST", "/orders/createlabelfororder", LABEL_OK)
        self._buy(order)
        self.assertFalse(self.http.calls_to("POST", "/shipments/createlabel"))


@tagged("post_install", "-at_install", "shipstation")
class TestLabelShipFromPreflight(ShipStationCommon):
    """Found against the real API: with no Ship From location ShipStation answers createlabelfororder with HTTP 500
    "No warehouse has been selected". The connector now checks first, so the order is not locked as ambiguous."""

    def test_no_ship_from_location_stops_before_the_paid_request_without_locking(self):
        order = self.make_order_sync(carrier_code="stamps_com", service_code="usps_ground_advantage",
                                     package_code="package", status="awaiting_shipment")
        self.http.add("GET", "/warehouses", [])
        self.http.add("POST", "/orders/createlabelfororder", {"shipmentId": 1, "trackingNumber": "X"})
        with self.assertRaisesRegex(UserError, "no Ship From location"):
            order.with_user(self.user_manager).action_create_label()
        self.assertFalse(self.http.calls_to("POST", "/orders/createlabelfororder"), "the paid request is never sent")
        self.assertIn(order.label_state, (False, "none"), "nothing was claimed, so nothing is locked")
