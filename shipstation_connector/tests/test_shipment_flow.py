"""Shipment import, delivery linking, partial/multi-package shipments, voided labels, recovery."""
import re
from unittest import mock

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .common import ShipStationCommon

PT_QUERY_FORMAT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")


@tagged("post_install", "-at_install", "shipstation")
class TestShipmentSync(ShipStationCommon):

    def test_sync_uses_only_parameters_documented_for_list_shipments(self):
        self.http.add("GET", "/shipments", {"shipments": []})
        result = self.instance._sync_shipments(mode="cron")
        self.assertTrue(result["checkpoint_advanced"])
        passes = [c["params"] for c in self.http.calls_to("GET", "/shipments")]
        self.assertEqual(len(passes), 2, "one pass for new labels, one for voided labels")
        created, voided = passes
        self.assertRegex(created["createDateStart"], PT_QUERY_FORMAT)
        self.assertRegex(voided["voidDateStart"], PT_QUERY_FORMAT)
        for params in passes:
            self.assertEqual(params["sortBy"], "CreateDate")
            self.assertEqual(params["storeId"], "111")
            self.assertFalse([k for k in params if k.lower().startswith("modifydate")],
                             "GET /shipments has no modifyDate filter")

    def test_voided_labels_are_not_counted_as_shipments(self):
        dashboard = self.env["shipstation.dashboard"].with_user(self.user_admin)
        base_total = dashboard.get_dashboard_data()["summary"]["shipments"]   # the database may hold other data
        self.make_shipment_sync()
        self.make_shipment_sync(shipstation_shipment_id="5002", voided=True)
        self.assertEqual(self.instance.total_shipments, 1)
        data = dashboard.get_dashboard_data()
        self.assertEqual(data["summary"]["shipments"], base_total + 1)
        row = next(i for i in data["instances"] if i["id"] == self.instance.id)
        self.assertEqual(row["total_shipments"], 1)

    def test_records_have_readable_names_not_model_comma_id(self):
        shipment = self.make_shipment_sync(tracking_number="1ZABC")
        self.assertEqual(shipment.display_name, "SO-9001 · 1ZABC")
        self.assertEqual(self.make_shipment_sync(shipstation_shipment_id=False, tracking_number="T9",
                                                 shipstation_order_number=False,
                                                 shipstation_order_id="55").display_name, "55 · T9")
        order = self.make_order_sync()
        self.assertEqual(order.display_name, "SO-9001 · Test Customer")
        log = self.env["shipstation.sync.log"].create(
            {"instance_id": self.instance.id, "status": "failed", "method": "GET", "endpoint": "/orders"})
        self.assertEqual(log.display_name, "GET /orders (failed)")

    def test_import_derives_status_and_handles_voids(self):
        good = self.shipment_payload(5001, tracking="T-GOOD")
        bad = self.shipment_payload(5002, tracking="T-VOID", voided=True)
        self.http.add("GET", "/shipments", {"shipments": [good, bad]})
        result = self.instance._sync_shipments(mode="cron")
        self.assertEqual(result["failed"], 0)
        records = self.env["shipstation.shipment.sync"].search([("instance_id", "=", self.instance.id)])
        by_id = {r.shipstation_shipment_id: r for r in records}
        self.assertEqual(by_id["5001"].status, "label_created")
        self.assertFalse(by_id["5001"].voided)
        self.assertEqual(by_id["5001"].link_state, "unmatched", "no Odoo order exists for it in this test")
        self.assertEqual((by_id["5002"].status, by_id["5002"].voided), ("voided", True))
        self.assertEqual(by_id["5002"].link_state, "voided")
        self.assertTrue(by_id["5002"].void_date)
        self.assertEqual(result["voided"], 1)

    def test_a_shipment_returned_by_both_passes_is_processed_once(self):
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001)]})
        self.instance._sync_shipments(mode="cron")
        self.assertEqual(self.env["shipstation.shipment.sync"].search_count(
            [("shipstation_shipment_id", "=", "5001")]), 1)

    def test_failures_hold_the_checkpoint(self):
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001, order_id=None)]})   # no order id
        result = self.instance._sync_shipments(mode="cron")
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["checkpoint_advanced"])
        self.assertFalse(self.instance.last_shipment_sync_at)

    def test_resync_does_not_duplicate_shipments(self):
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001)]})
        self.instance._sync_shipments(mode="cron")
        self.instance._sync_shipments(mode="cron")
        self.assertEqual(self.env["shipstation.shipment.sync"].search_count(
            [("shipstation_shipment_id", "=", "5001")]), 1)


@tagged("post_install", "-at_install", "shipstation")
class TestDeliveryLinking(ShipStationCommon):

    def _link(self, payload):
        return self.instance._process_shipment_record(payload)

    def _setup_order(self, qty=4.0):
        sale = self.link_sale_to_shipstation(self.make_confirmed_sale(qty=qty))
        self.assertEqual(len(sale.picking_ids), 1)
        return sale, sale.picking_ids

    def test_single_delivery_is_linked_with_tracking(self):
        sale, picking = self._setup_order()
        self.assertEqual(self._link(self.shipment_payload(5001, tracking="T1")), "linked")
        self.assertEqual(picking.carrier_tracking_ref, "T1")
        self.assertEqual(picking.shipstation_shipment_id, "5001")
        shipment = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")])
        self.assertEqual((shipment.link_state, shipment.picking_id), ("linked", picking))
        # idempotent
        self.assertEqual(self._link(self.shipment_payload(5001, tracking="T1")), "linked")

    def test_order_without_a_delivery_stays_pending(self):
        draft = self.env["sale.order"].create({
            "partner_id": self.env["res.partner"].create({"name": "Q"}).id,
            "order_line": [(0, 0, {"product_id": self.make_product("Q-1").id, "product_uom_qty": 1})],
        })
        self.link_sale_to_shipstation(draft)
        self.assertEqual(self._link(self.shipment_payload(5001)), "pending")

    def test_a_different_package_never_overwrites_existing_tracking(self):
        sale, picking = self._setup_order()
        self._link(self.shipment_payload(5001, tracking="T1"))
        self.assertEqual(self._link(self.shipment_payload(5002, tracking="T2")), "review")
        self.assertEqual((picking.carrier_tracking_ref, picking.shipstation_shipment_id), ("T1", "5001"))

    def test_two_unclaimed_deliveries_are_ambiguous(self):
        sale, picking = self._setup_order()
        picking.copy()
        self.assertEqual(len(sale.picking_ids), 2)
        self.assertEqual(self._link(self.shipment_payload(5001)), "review")
        self.assertFalse(picking.carrier_tracking_ref, "an ambiguous match must not write tracking anywhere")

    def test_partial_shipment_links_second_package_to_the_backorder(self):
        sale, first = self._setup_order(qty=4.0)
        self.assertEqual(self._link(self.shipment_payload(5001, tracking="T-PKG1")), "linked")
        first.move_lines.quantity_done = 2.0  # Odoo 15: move_lines / quantity_done (19: move_ids / quantity)
        first.with_context(skip_backorder=True).button_validate()
        self.assertEqual(first.state, "done")
        backorder = sale.picking_ids - first
        self.assertEqual(len(backorder), 1, "a backorder for the remaining 2 units must exist")
        self.assertFalse(backorder.shipstation_shipment_id, "backorders must not inherit shipment identity")
        self.assertFalse(backorder.shipstation_tracking_number)
        self.assertEqual(self._link(self.shipment_payload(5002, tracking="T-PKG2")), "linked")
        self.assertEqual(backorder.carrier_tracking_ref, "T-PKG2")
        self.assertEqual(first.carrier_tracking_ref, "T-PKG1", "package 1 keeps its own tracking")

    def test_carrier_mapping_is_applied_only_when_explicit(self):
        sale, picking = self._setup_order()
        self._link(self.shipment_payload(5001))
        self.assertFalse(picking.carrier_id, "carriers are never guessed from a name")
        carrier = self.env["delivery.carrier"].create({
            "name": "Mapped UPS", "delivery_type": "fixed",
            "product_id": self.make_product("DELIVERY-1").id})
        self.env["shipstation.carrier.mapping"].create({
            "instance_id": self.instance.id, "shipstation_carrier_code": "ups",
            "odoo_carrier_id": carrier.id})
        shipment = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")])
        self.instance._reconcile_single_shipment(shipment)
        self.assertEqual(picking.carrier_id, carrier)

    def test_voided_label_removes_only_the_tracking_it_wrote_and_escalates(self):
        sale, picking = self._setup_order()
        self._link(self.shipment_payload(5001, tracking="T1"))
        self.assertEqual(picking.carrier_tracking_ref, "T1")
        outcome = self._link(self.shipment_payload(5001, tracking="T1", voided=True))
        self.assertEqual(outcome, "review")
        self.assertFalse(picking.carrier_tracking_ref)
        self.assertFalse(picking.shipstation_shipment_id)
        shipment = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")])
        self.assertEqual((shipment.link_state, shipment.picking_id.id, shipment.voided), ("review", False, True))
        self.assertIn(picking.display_name, shipment.link_message)
        # a repeat sync must not silently clear the escalation
        self.assertEqual(self._link(self.shipment_payload(5001, tracking="T1", voided=True)), "review")
        self.assertEqual(shipment.link_state, "review")
        # only a manager can acknowledge it
        with self.assertRaises(AccessError):
            shipment.with_user(self.user_reader).action_acknowledge_void()
        shipment.with_user(self.user_manager).action_acknowledge_void()
        self.assertEqual(shipment.link_state, "voided")

    def test_replacement_label_is_linked_automatically_once_the_void_is_processed(self):
        sale, picking = self._setup_order()
        self._link(self.shipment_payload(5001, tracking="T1"))
        # the replacement is bought before the void reaches Odoo, so it is parked for review
        self.assertEqual(self._link(self.shipment_payload(5002, tracking="T2")), "review")
        replacement = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5002")])
        self.assertEqual(replacement.link_state, "review")
        self._link(self.shipment_payload(5001, tracking="T1", voided=True))
        self.assertEqual((replacement.link_state, replacement.picking_id), ("linked", picking))
        self.assertEqual((picking.carrier_tracking_ref, picking.shipstation_shipment_id), ("T2", "5002"))
        voided = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")])
        self.assertEqual(voided.link_state, "review", "the void itself still needs an operator acknowledgement")
        self.assertFalse(self.http.calls, "recovery is local only")

    def test_void_recovery_leaves_other_orders_alone_and_survives_a_broken_sibling(self):
        sale, picking = self._setup_order()
        self._link(self.shipment_payload(5001, tracking="T1"))
        other = self.make_shipment_sync(shipstation_shipment_id="6001", shipstation_order_id="8888",
                                        shipstation_order_number="OTHER", link_state="review")
        broken = self.make_shipment_sync(shipstation_shipment_id="6002", link_state="review", tracking_number=False)
        with mock.patch.object(type(self.instance), "_reconcile_single_shipment",
                               autospec=True, side_effect=[RuntimeError("boom")]) as reconcile:
            result = self.instance._reconcile_voided_shipment(
                self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")]))
        self.assertEqual(result, "review")
        self.assertEqual(other.link_state, "review", "a different order must never be touched")
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(broken.link_state, "review", "a failed re-link leaves the sibling for manual recheck")

    def test_unlinked_void_needs_no_action_and_cannot_be_acknowledged_when_valid(self):
        self.assertEqual(self._link(self.shipment_payload(5003, voided=True)), "voided")
        valid = self.make_shipment_sync(shipstation_shipment_id="5009", voided=False)
        with self.assertRaises(UserError):
            valid.with_user(self.user_manager).action_acknowledge_void()

    def test_admin_without_sales_rights_can_recheck_a_single_link(self):
        sale, picking = self._setup_order()
        shipment = self.make_shipment_sync()
        shipment.with_user(self.user_admin).action_retry_delivery_link()
        self.assertEqual((shipment.link_state, picking.carrier_tracking_ref), ("linked", "TRK123"))

    def test_shipment_without_an_odoo_order_is_kept_visible_and_never_blocks_the_checkpoint(self):
        self.http.add("GET", "/shipments", {"shipments": [
            self.shipment_payload(5100, order_id=424242, number="FOREIGN-1", tracking="T-FOREIGN")]})
        result = self.instance._sync_shipments(mode="cron")
        self.assertEqual((result["unmatched"], result["failed"]), (1, 0))
        self.assertTrue(result["checkpoint_advanced"])
        record = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5100")])
        self.assertEqual(record.link_state, "unmatched")
        self.assertIn("FOREIGN-1", record.link_message)
        # once the order arrives, a recheck links it
        sale = self.make_confirmed_sale()
        self.link_sale_to_shipstation(sale, ss_order_id="424242", number="FOREIGN-1")
        record.with_user(self.user_admin).action_retry_delivery_link()
        self.assertEqual(record.link_state, "linked")

    def test_tracking_for_a_different_company_delivery_is_never_written(self):
        sale, picking = self._setup_order()
        foreign = self.shipment_payload(5001, storeId=222)   # a payload that NAMES another store is refused
        with self.assertRaises(UserError):
            self._link(foreign)
        self.assertFalse(picking.carrier_tracking_ref)

    def test_workbench_actions_do_not_touch_the_network(self):
        sale, picking = self._setup_order()
        shipment = self.make_shipment_sync()
        manager = shipment.with_user(self.user_manager)
        manager.action_claim_review()
        self.assertEqual(shipment.review_owner_id, self.user_manager)
        with self.assertRaises(AccessError):
            manager.action_bulk_recheck_delivery_links()      # admin only
        shipment.with_user(self.user_admin).action_bulk_recheck_delivery_links()
        self.assertEqual(shipment.link_state, "linked")
        self.assertFalse(self.http.calls)
