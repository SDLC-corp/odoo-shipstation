"""Order import, incremental sync, safe push, and copy semantics against the real ORM."""
import re
from datetime import datetime
from unittest import mock

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .common import FakeResponse, ShipStationCommon

PT_QUERY_FORMAT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")


@tagged("post_install", "-at_install", "shipstation")
class TestOrderImport(ShipStationCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.make_product(cls, "SS-SKU-1")

    def _sale(self, ss_id="9001"):
        return self.env["sale.order"].search([
            ("shipstation_instance_id", "=", self.instance.id), ("shipstation_order_id", "=", ss_id)])

    def test_import_creates_one_draft_quotation_with_converted_dates(self):
        self.assertEqual(self.instance._import_order_record(self.order_payload()), "processed")
        sale = self._sale()
        self.assertEqual(len(sale), 1)
        self.assertEqual(sale.state, "draft", "imported orders must never be auto-confirmed")
        self.assertEqual(sale.client_order_ref, "SO-9001")
        self.assertEqual(sale.order_line.product_uom_qty, 2.0)
        self.assertEqual(sale.partner_id.email, "import@example.test")
        self.assertEqual(sale.date_order, datetime(2026, 11, 5, 16, 0, 0))     # 08:00 Pacific
        mirror = self.env["shipstation.order.sync"].search([("shipstation_order_id", "=", "9001")])
        self.assertEqual(mirror.last_modified_date, datetime(2026, 11, 5, 17, 30, 0))  # 09:30 Pacific
        self.assertEqual(mirror.status, "awaiting_shipment")

    def test_import_is_idempotent_and_ignores_stale_updates(self):
        self.instance._import_order_record(self.order_payload())
        self.assertEqual(self.instance._import_order_record(self.order_payload()), "processed")
        self.assertEqual(len(self._sale()), 1)
        self.assertEqual(self.env["shipstation.order.sync"].search_count(
            [("shipstation_order_id", "=", "9001")]), 1)
        older = self.order_payload(modify="2026-11-05T09:00:00.0000000", qty=99)
        self.assertEqual(self.instance._import_order_record(older), "stale")
        self.assertEqual(self._sale().order_line.product_uom_qty, 2.0)

    def test_update_replaces_lines_only_while_draft(self):
        self.instance._import_order_record(self.order_payload())
        self.instance._import_order_record(self.order_payload(modify="2026-11-05T10:00:00.0000000", qty=7))
        self.assertEqual(self._sale().order_line.product_uom_qty, 7.0)
        sale = self._sale()
        sale.action_confirm()
        self.instance._import_order_record(self.order_payload(modify="2026-11-05T11:00:00.0000000", qty=1))
        self.assertEqual(sale.state, "sale")
        self.assertEqual(sale.order_line.product_uom_qty, 7.0, "confirmed orders must not be rewritten")
        self.assertEqual(sale.shipstation_last_modified_date, datetime(2026, 11, 5, 19, 0, 0))

    def test_cancelled_remote_order_cancels_draft_quotation(self):
        self.instance._import_order_record(self.order_payload())
        self.instance._import_order_record(
            self.order_payload(status="cancelled", modify="2026-11-05T10:30:00.0000000"))
        self.assertEqual(self._sale().state, "cancel")

    def test_import_rejects_other_store_and_missing_identity(self):
        with self.assertRaises(UserError):
            self.instance._import_order_record(self.order_payload(store_id=999))
        with self.assertRaises(UserError):
            self.instance._import_order_record(self.order_payload(number=""))
        with self.assertRaises(UserError):
            self.instance._import_order_record("not a dict")
        self.assertFalse(self._sale())

    def test_two_instances_may_share_a_remote_order_number_without_collision(self):
        self.instance._import_order_record(self.order_payload())
        self.instance_b._import_order_record(self.order_payload(store_id=222))
        self.assertEqual(self.env["sale.order"].search_count(
            [("shipstation_order_id", "=", "9001")]), 2)


@tagged("post_install", "-at_install", "shipstation")
class TestOrderSync(ShipStationCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.make_product(cls, "SS-SKU-1")

    def test_sync_uses_pacific_query_window_and_advances_checkpoint(self):
        self.http.add("GET", "/orders", {"orders": [
            self.order_payload(9001, "SO-1"), self.order_payload(9002, "SO-2")]})
        result = self.instance._sync_orders(mode="cron")
        self.assertEqual((result["processed"], result["failed"]), (2, 0))
        self.assertTrue(result["checkpoint_advanced"])
        self.assertTrue(self.instance.last_order_sync_at)
        params = self.http.calls[0]["params"]
        self.assertRegex(params["modifyDateStart"], PT_QUERY_FORMAT)
        self.assertRegex(params["modifyDateEnd"], PT_QUERY_FORMAT)
        self.assertEqual((params["storeId"], params["orderStatus"], params["sortBy"]),
                         ("111", "awaiting_shipment", "ModifyDate"))
        report = self.env["shipstation.report"].search([("instance_id", "=", self.instance.id)], limit=1)
        self.assertEqual(report.status, "success")

    def test_one_bad_record_holds_checkpoint_but_good_records_import(self):
        self.http.add("GET", "/orders", {"orders": [
            self.order_payload(9001, "SO-1"), self.order_payload(9002, "")]})
        result = self.instance._sync_orders(mode="cron")
        self.assertEqual((result["processed"], result["failed"]), (1, 1))
        self.assertFalse(result["checkpoint_advanced"])
        self.assertFalse(self.instance.last_order_sync_at)
        self.assertEqual(self.env["sale.order"].search_count([("shipstation_order_id", "=", "9001")]), 1)
        failed = self.env["shipstation.report"].search(
            [("instance_id", "=", self.instance.id), ("status", "=", "failed")])
        self.assertTrue(failed)

    def test_api_failure_holds_checkpoint_and_is_reported(self):
        self.http.add("GET", "/orders", {"Message": "down"}, status=503)
        result = self.instance._sync_orders(mode="cron")
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["checkpoint_advanced"])
        self.assertFalse(self.instance.last_order_sync_at)

    def test_malformed_response_is_rejected_not_trusted(self):
        self.http.add("GET", "/orders", {"unexpected": []})
        result = self.instance._sync_orders(mode="cron")
        self.assertFalse(result["checkpoint_advanced"])

    def test_cron_isolates_a_broken_instance_from_healthy_ones(self):
        self.instance_b.write({"cron_sync_orders": True})
        self.instance.write({"cron_sync_orders": True})

        def by_store(record):
            if record["params"].get("storeId") == "111":
                return FakeResponse({"Message": "broken"}, 500)
            return FakeResponse({"orders": [self.order_payload(8001, "SO-B", store_id=222)]})

        self.http.add("GET", "/orders", callback=by_store)
        self.env["shipstation.instance"].run_cron_sync_orders()
        self.assertTrue(self.instance_b.last_order_sync_at, "healthy instance must still be synced")
        self.assertFalse(self.instance.last_order_sync_at)


@tagged("post_install", "-at_install", "shipstation")
class TestOrderPush(ShipStationCommon):

    def test_push_is_idempotent_keyed_and_limited_to_open_deliveries(self):
        open_order = self.make_confirmed_sale()
        draft = self.env["sale.order"].create({
            "partner_id": self.env["res.partner"].create({"name": "Draft Buyer"}).id,
            "order_line": [(0, 0, {"product_id": self.make_product("DRAFT-1").id, "product_uom_qty": 1})],
        })
        finished = self.make_confirmed_sale(product=self.make_product("DONE-1"))
        finished.picking_ids.action_cancel()   # nothing left to ship
        foreign = self.make_confirmed_sale(product=self.make_product("FOREIGN-1"))
        foreign.write({"shipstation_instance_id": self.instance_b.id})

        self.http.add("POST", "/orders/createorder", {"orderId": 4242, "orderNumber": open_order.name})
        result = self.instance._push_orders(mode="manual")

        failures = [(o.name, o.shipstation_push_error) for o in open_order if o.shipstation_push_error]
        self.assertEqual((result["pushed"], result["failed"]), (1, 0), failures)
        pushes = self.http.calls_to("POST", "/orders/createorder")
        self.assertEqual(len(pushes), 1, "only the order with an open delivery may be pushed")
        body = pushes[0]["json"]
        self.assertEqual(body["orderKey"], "odoo-%s-%s" % (self.instance.id, open_order.name))
        self.assertEqual(body["orderStatus"], "awaiting_shipment")
        self.assertEqual(body["storeId"], 111)
        self.assertNotIn("orderId", body)
        self.assertEqual(open_order.shipstation_order_id, "4242")
        self.assertEqual(open_order.shipstation_push_state, "success")
        for untouched in (draft, finished, foreign):
            self.assertEqual(untouched.shipstation_push_state, "draft")
        # running again must not send the same order twice
        self.instance._push_orders(mode="manual")
        self.assertEqual(len(self.http.calls_to("POST", "/orders/createorder")), 1)

    def test_failing_orders_cannot_starve_new_ones(self):
        failing = self.make_confirmed_sale(product=self.make_product("P-FAIL"))
        fresh = self.make_confirmed_sale(product=self.make_product("P-NEW"))
        failing.write({"shipstation_push_state": "failed"})   # lower id, tried before and rejected
        self.assertLess(failing.id, fresh.id)
        self.http.add("POST", "/orders/createorder", {"orderId": 31})
        with mock.patch.object(type(self.instance), "PUSH_BATCH_LIMIT", 1):
            result = self.instance._push_orders(mode="manual")
        self.assertEqual((result["pushed"], result["remaining"]), (1, 1))
        self.assertEqual(fresh.shipstation_order_id, "31")
        self.assertFalse(failing.shipstation_order_id)

    def test_instance_totals_use_grouped_queries(self):
        self.make_order_sync(total_amount=30.0)
        self.make_order_sync(shipstation_order_id="9002", shipstation_order_number="SO-2", total_amount=12.5)
        self.make_shipment_sync()
        instance = self.instance.with_user(self.user_manager)
        self.assertEqual((instance.total_orders, instance.total_shipments, instance.total_revenue), (2, 1, 42.5))
        self.assertEqual(self.instance_b.with_user(self.user_manager_b).total_orders, 0)

    def test_shipping_charge_is_a_shipping_amount_not_an_item(self):
        """Regression: sale.order.amount_delivery exists only with website_sale, so reading it crashed."""
        item, fee = self.make_product("ITEM-1"), self.make_product("SHIP-FEE")
        sale = self.make_confirmed_sale(order_line=[
            (0, 0, {"product_id": item.id, "product_uom_qty": 1, "price_unit": 10.0}),
            (0, 0, {"product_id": fee.id, "product_uom_qty": 1, "price_unit": 7.0, "is_delivery": True}),
        ])
        payload = self.instance._build_order_payload(sale)
        self.assertEqual(payload["shippingAmount"], 7.0)
        self.assertEqual([line["sku"] for line in payload["items"]], ["ITEM-1"])

    def test_known_remote_id_is_sent_so_updates_never_duplicate(self):
        order = self.link_sale_to_shipstation(self.make_confirmed_sale(), ss_order_id="4242")
        body = self.instance._build_order_payload(order)
        self.assertEqual(body["orderId"], 4242)
        self.assertTrue(body["orderKey"].startswith("odoo-"))

    def test_failed_push_is_recorded_and_does_not_block_other_orders(self):
        first = self.make_confirmed_sale(product=self.make_product("P-A"))
        second = self.make_confirmed_sale(product=self.make_product("P-B"))
        replies = iter([FakeResponse({"Message": "invalid"}, 400), FakeResponse({"orderId": 77})])
        self.http.add("POST", "/orders/createorder", callback=lambda r: next(replies))
        result = self.instance._push_orders(mode="manual")
        self.assertEqual((result["pushed"], result["failed"]), (1, 1))
        states = {first.shipstation_push_state, second.shipstation_push_state}
        self.assertEqual(states, {"failed", "success"})
        failed = first if first.shipstation_push_state == "failed" else second
        self.assertIn("invalid", failed.shipstation_push_error)
        self.assertEqual(len(self.http.calls_to("POST", "/orders/createorder")), 2, "no automatic retry of a POST")

    def test_push_reports_remaining_when_over_batch_limit(self):
        order = self.make_confirmed_sale()
        self.http.add("POST", "/orders/createorder", {"orderId": 1})
        result = self.instance._push_orders(mode="manual")
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(order.shipstation_order_id, "1")

    def test_manual_push_action_requires_admin(self):
        self.make_confirmed_sale()
        with self.assertRaises(AccessError):
            self.instance.with_user(self.user_manager).action_push_orders()
        self.assertFalse(self.http.calls)


@tagged("post_install", "-at_install", "shipstation")
class TestConnectorRolesNeedNoOtherApps(ShipStationCommon):
    """A ShipStation Admin/Manager is not necessarily a Sales or Inventory user."""

    def test_admin_without_sales_rights_can_run_a_manual_order_sync(self):
        self.make_product("SS-SKU-1")
        self.assertFalse(self.user_admin.has_group("sales_team.group_sale_salesman"))
        self.http.add("GET", "/orders", {"orders": [self.order_payload()]})
        result = self.instance.with_user(self.user_admin).action_sync_orders()
        self.assertEqual(result["params"]["type"], "success", result["params"]["message"])
        self.assertEqual(self.env["sale.order"].search_count([("shipstation_order_id", "=", "9001")]), 1)

    def test_admin_without_sales_rights_can_sync_shipments_and_push_orders(self):
        order = self.make_confirmed_sale()
        self.http.add("GET", "/shipments", {"shipments": []})
        self.http.add("POST", "/orders/createorder", {"orderId": 55})
        admin = self.instance.with_user(self.user_admin)
        self.assertEqual(admin.action_sync_shipments()["params"]["type"], "success")
        result = admin.action_push_orders()
        self.assertEqual(result["params"]["type"], "success", result["params"]["message"])
        self.assertEqual(order.shipstation_order_id, "55")

    def test_manager_without_sales_rights_still_cannot_run_admin_syncs(self):
        with self.assertRaises(AccessError):
            self.instance.with_user(self.user_manager).action_sync_orders()

    def test_pull_requires_the_manager_role(self):
        order = self.make_order_sync().with_user(self.user_reader)
        with self.assertRaises(AccessError):
            order.action_pull_from_shipstation()
        self.assertFalse(self.http.calls)


@tagged("post_install", "-at_install", "shipstation")
class TestCopyAndIdentity(ShipStationCommon):

    def test_duplicating_records_never_clones_shipstation_identity(self):
        order = self.link_sale_to_shipstation(self.make_confirmed_sale())
        order.write({"shipstation_pushed": True, "shipstation_push_state": "success"})
        clone = order.copy()
        for name in ("shipstation_instance_id", "shipstation_order_id", "shipstation_order_number",
                     "shipstation_store_id", "shipstation_status"):
            self.assertFalse(clone[name], name)
        self.assertEqual((clone.shipstation_pushed, clone.shipstation_push_state), (False, "draft"))

        partner = self.env["res.partner"].create({
            "name": "P", "shipstation_customer_id": "C1", "shipstation_pushed": True})
        self.assertFalse(partner.copy().shipstation_customer_id)
        self.assertFalse(partner.copy().shipstation_pushed)

        picking = order.picking_ids[:1]
        picking.write({"shipstation_shipment_id": "S1", "shipstation_tracking_number": "T1"})
        copied = picking.copy()
        self.assertFalse(copied.shipstation_shipment_id)
        self.assertFalse(copied.shipstation_tracking_number)

    def test_copy_of_an_instance_gets_a_fresh_webhook_secret(self):
        clone = self.instance.copy({"name": "Clone"})
        self.assertNotEqual(clone.webhook_token, self.instance.webhook_token)
        self.assertFalse(clone.v2_api_key, "the V2 key must not be silently copied")
