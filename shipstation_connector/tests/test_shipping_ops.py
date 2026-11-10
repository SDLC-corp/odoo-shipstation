"""Rates, quote freshness, carrier catalog, bulk fulfillment preparation and return cases."""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from .common import ShipStationCommon

RATES = [
    {"serviceName": "UPS Ground", "serviceCode": "ups_ground", "shipmentCost": 9.5, "otherCost": 0.0},
    {"serviceName": "UPS 2nd Day", "serviceCode": "ups_2nd_day_air", "shipmentCost": 21.0, "otherCost": 1.5},
    {"serviceName": "UPS Saver", "serviceCode": "ups_saver", "shipmentCost": 9.5, "otherCost": 0.0},
]


@tagged("post_install", "-at_install", "shipstation")
class TestRates(ShipStationCommon):

    def _quotable(self, **extra):
        vals = dict(carrier_code="ups", rate_from_postal="78701", rate_to_country="us",
                    rate_to_postal="10001", rate_weight=16, rate_weight_units="ounces",
                    package_code="package")
        vals.update(extra)
        return self.make_order_sync(**vals).with_user(self.user_manager)

    def test_rates_are_quotes_only_and_stored(self):
        order = self._quotable()
        self.http.add("POST", "/shipments/getrates", RATES)
        result = order.action_get_rates()
        order.invalidate_cache(["rate_ids"], order.ids)  # as a UI reload would: the cache keeps insertion order
        self.assertEqual(len(order.rate_ids), 3)
        self.assertEqual(order.rate_ids.mapped("service_code"), ["ups_ground", "ups_saver", "ups_2nd_day_air"],
                         "cheapest first; equal prices keep a stable order")
        self.assertIn("No label has been purchased", result["params"]["message"])
        request = self.http.calls[0]["json"]
        self.assertEqual((request["carrierCode"], request["toCountry"], request["fromPostalCode"]),
                         ("ups", "US", "78701"))
        self.assertEqual(self.http.calls[0]["path"], "/shipments/getrates")
        self.assertFalse(self.http.calls_to("POST", "/orders/createlabelfororder"))

    def test_invalid_responses_preserve_previous_quotes(self):
        order = self._quotable()
        self.http.add("POST", "/shipments/getrates", RATES)
        order.action_get_rates()
        previous = order.rate_ids.ids
        for bad in ({"nope": 1}, [{"serviceName": "x"}], [{"serviceCode": "a", "shipmentCost": "abc"}],
                    [{"serviceCode": "a", "shipmentCost": -3}]):
            with self.subTest(bad=bad):
                self.http.reset()
                self.http.add("POST", "/shipments/getrates", bad)
                with self.assertRaises(UserError):
                    order.action_get_rates()
                self.assertCountEqual(order.rate_ids.ids, previous)

    def test_required_inputs_are_validated_before_any_request(self):
        for vals in (dict(rate_weight=0), dict(rate_from_postal=False), dict(rate_to_country="USA"),
                     dict(carrier_code=False), dict(rate_length=5, rate_width=0, rate_height=0)):
            with self.subTest(vals=vals):
                order = self._quotable(**vals)
                with self.assertRaises(UserError):
                    order.action_get_rates()
        self.assertFalse(self.http.calls)

    def test_best_rate_is_cheapest_total_and_keeps_quote_current(self):
        order = self._quotable()
        self.http.add("POST", "/shipments/getrates", RATES)
        order.action_get_rates()
        order.action_select_best_rate()
        self.assertEqual(order.service_code, "ups_ground")        # ties resolved by id: first returned
        self.assertEqual(order.rate_request_hash, order._rate_fingerprint(order._build_v1_rate_request()))
        order._ensure_fresh_rate_quote()                           # still valid after selection

    def test_manager_can_refresh_quotes_repeatedly(self):
        """Regression: managers lack delete rights on rates, so the second Get Rates used to crash."""
        order = self._quotable()
        self.http.add("POST", "/shipments/getrates", RATES)
        order.action_get_rates()
        first = order.rate_ids.ids
        order.action_get_rates()
        self.assertEqual(len(order.rate_ids), 3)
        self.assertFalse(set(first) & set(order.rate_ids.ids), "old quotes are replaced, not duplicated")

    def test_quote_goes_stale_after_30_minutes_or_when_details_change(self):
        order = self._quotable()
        self.http.add("POST", "/shipments/getrates", RATES)
        order.action_get_rates()
        order.sudo().rate_to_postal = "90210"
        with self.assertRaises(UserError):
            order.action_select_best_rate()
        order.sudo().rate_to_postal = "10001"
        order.action_get_rates()
        order.sudo().rate_checked_at = fields.Datetime.now() - timedelta(minutes=31)
        with self.assertRaises(UserError):
            order.action_select_best_rate()

    def test_readers_cannot_quote(self):
        order = self.make_order_sync().with_user(self.user_reader)
        with self.assertRaises(AccessError):
            order.action_get_rates()

    def test_package_presets_validate_and_load(self):
        Preset = self.env["shipstation.package.preset"]
        with self.assertRaises(ValidationError):
            Preset.create({"name": "bad", "instance_id": self.instance.id, "package_code": "package",
                           "weight_value": 0})
        with self.assertRaises(ValidationError):
            Preset.create({"name": "half", "instance_id": self.instance.id, "package_code": "package",
                           "weight_value": 5, "length": 4})
        preset = Preset.create({"name": "Small", "instance_id": self.instance.id, "package_code": "package",
                                "weight_value": 12, "weight_units": "ounces",
                                "length": 6, "width": 5, "height": 3})
        order = self.make_order_sync(package_preset_id=preset.id).with_user(self.user_manager)
        order.action_apply_package_preset()
        self.assertEqual((order.rate_weight, order.rate_length), (12.0, 6.0))
        other = Preset.create({"name": "Other", "instance_id": self.instance_b.id, "package_code": "package",
                               "weight_value": 1})
        order.sudo().package_preset_id = other
        with self.assertRaises(UserError):
            order.action_apply_package_preset()

    def test_carrier_discovery_is_get_only_and_validated(self):
        self.http.add("GET", "/carriers", [{"code": "ups", "name": "UPS"}])
        self.http.add("GET", "/carriers/listservices", [{"code": "ups_ground", "name": "UPS Ground"}])
        self.http.add("GET", "/carriers/listpackages", [{"code": "package", "name": "Package"}])
        self.instance.with_user(self.user_admin).action_discover_carrier_catalog()
        catalog = self.env["shipstation.carrier.catalog"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(sorted(catalog.mapped("kind")), ["carrier", "package", "service"])
        self.assertTrue(all(c["method"] == "GET" for c in self.http.calls))
        # re-running updates in place
        self.instance.with_user(self.user_admin).action_discover_carrier_catalog()
        self.assertEqual(self.env["shipstation.carrier.catalog"].search_count(
            [("instance_id", "=", self.instance.id)]), 3)
        self.http.reset()
        self.http.add("GET", "/carriers", {"unexpected": True})
        with self.assertRaises(UserError):
            self.instance.with_user(self.user_admin).action_discover_carrier_catalog()


@tagged("post_install", "-at_install", "shipstation")
class TestFulfillmentBatches(ShipStationCommon):

    def _prepared_order(self, number="SO-1", ss_id="9001"):
        order = self.make_order_sync(
            shipstation_order_id=ss_id, shipstation_order_number=number, carrier_code="ups",
            rate_from_postal="78701", rate_to_country="US", rate_to_postal="10001", rate_weight=16,
            package_code="package")
        self.http.reset()
        self.http.add("POST", "/shipments/getrates", RATES)
        order.with_user(self.user_manager).action_get_rates()
        order.with_user(self.user_manager).action_select_best_rate()
        return order

    def test_unprepared_orders_are_blocked_and_prepared_ones_ready(self):
        ready = self._prepared_order()
        blocked = self.make_order_sync(shipstation_order_id="9002", shipstation_order_number="SO-2")
        before = len(self.http.calls)
        action = (ready | blocked).with_user(self.user_manager).action_create_fulfillment_batch()
        batch = self.env["shipstation.fulfillment.batch"].browse(action["res_id"])
        states = {line.order_id: line.state for line in batch.line_ids}
        self.assertEqual(states, {ready: "ready", blocked: "blocked"})
        self.assertEqual(batch.state, "review", "any blocked order keeps the batch in review")
        self.assertEqual(len(self.http.calls), before, "batch preparation must make no ShipStation calls")
        self.assertEqual(ready.label_state, "none")

    def test_all_ready_batch_is_ready_but_never_buys_anything(self):
        ready = self._prepared_order()
        batch = self.env["shipstation.fulfillment.batch"].browse(
            ready.with_user(self.user_manager).action_create_fulfillment_batch()["res_id"])
        self.assertEqual(batch.state, "ready")
        self.assertFalse(self.http.calls_to("POST", "/orders/createlabelfororder"))

    def test_expired_quote_blocks_readiness(self):
        order = self._prepared_order()
        order.sudo().rate_checked_at = fields.Datetime.now() - timedelta(minutes=45)
        batch = self.env["shipstation.fulfillment.batch"].browse(
            order.with_user(self.user_manager).action_create_fulfillment_batch()["res_id"])
        self.assertEqual(batch.line_ids.state, "blocked")

    def test_order_cannot_be_in_two_open_batches_or_cross_instances(self):
        order = self._prepared_order()
        order.with_user(self.user_manager).action_create_fulfillment_batch()
        with self.assertRaises(UserError):
            order.with_user(self.user_manager).action_create_fulfillment_batch()
        other = self.make_order_sync(self.instance_b, shipstation_order_id="8001")
        self.attempt(lambda: (order | other).with_user(self.user_admin).action_create_fulfillment_batch(),
                     UserError, AccessError)

    def test_batch_size_is_bounded_and_readers_are_refused(self):
        orders = self.env["shipstation.order.sync"]
        with self.assertRaises(UserError):
            orders.with_user(self.user_manager).action_create_fulfillment_batch()
        order = self._prepared_order()
        with self.assertRaises(AccessError):
            order.with_user(self.user_reader).action_create_fulfillment_batch()

    def test_batch_close_requires_every_order_to_be_shipped(self):
        order = self._prepared_order()
        batch = self.env["shipstation.fulfillment.batch"].browse(
            order.with_user(self.user_manager).action_create_fulfillment_batch()["res_id"])
        manager_batch = batch.with_user(self.user_manager)
        with self.assertRaises(UserError):
            manager_batch.action_close_batch()
        order.sudo().write({"shipstation_shipment_id": "55", "tracking_number": "T"})
        manager_batch.action_refresh_readiness()
        self.assertEqual(batch.line_ids.state, "shipped")
        manager_batch.action_close_batch()
        self.assertEqual(batch.state, "closed")
        with self.assertRaises(UserError):
            manager_batch.action_refresh_readiness()
        manager_batch.action_reopen_batch()
        self.assertEqual(batch.state, "review")
        with self.assertRaises(AccessError):
            batch.line_ids.with_user(self.user_manager).write({"state": "ready"})


@tagged("post_install", "-at_install", "shipstation")
class TestReturnCases(ShipStationCommon):

    def _case(self, user=None, **extra):
        order = self.make_order_sync(
            payload=str({"orderId": 9001, "items": [{"sku": "SKU-A", "quantity": 3}, {"sku": "SKU-B", "quantity": 1}]}))
        vals = {"instance_id": self.instance.id, "order_id": order.id}
        vals.update(extra)
        return self.env["shipstation.return.case"].with_user(user or self.user_manager).create(vals)

    def _line(self, case, sku="SKU-A", qty=1.0):
        return self.env["shipstation.return.case.line"].with_user(self.user_manager).create(
            {"case_id": case.id, "sku": sku, "quantity": qty})

    def test_full_lifecycle_with_audit_trail(self):
        case = self._case()
        self.assertTrue(case.name.startswith("SHIP-RETURN-"))
        with self.assertRaises(UserError):
            case.action_submit_review()                    # no items yet
        self._line(case)
        case.action_submit_review()
        self.assertEqual(case.state, "review")
        admin_case = case.with_user(self.user_admin)
        with self.assertRaises(UserError):
            admin_case.action_close_manual_case()          # must be approved first
        admin_case.action_approve_manual_handling()
        self.assertEqual((case.state, case.approved_by_id), ("approved", self.user_admin))
        with self.assertRaises(UserError):
            admin_case.action_close_manual_case()          # needs a resolution note
        case.sudo().resolution_note = "Refunded outside Odoo"
        admin_case.action_close_manual_case()
        self.assertEqual(case.state, "closed")
        with self.assertRaises(UserError):
            case.with_user(self.user_manager).write({"customer_note": "late edit"})
        self.assertFalse(self.http.calls, "return cases are local tracking only")

    def test_rejection_requires_a_reason_and_is_final(self):
        case = self._case()
        self._line(case)
        case.action_submit_review()
        admin_case = case.with_user(self.user_admin)
        with self.assertRaises(UserError):
            admin_case.action_reject_case()
        case.sudo().resolution_note = "Outside return window"
        admin_case.action_reject_case()
        self.assertEqual(case.state, "rejected")
        with self.assertRaises(UserError):
            admin_case.action_approve_manual_handling()

    def test_items_are_frozen_after_approval(self):
        case = self._case()
        line = self._line(case)
        case.action_submit_review()
        case.with_user(self.user_admin).action_approve_manual_handling()
        with self.assertRaises(UserError):
            line.write({"quantity": 2})
        with self.assertRaises(UserError):
            line.unlink()
        with self.assertRaises(UserError):
            self._line(case, "SKU-B")

    def test_quantities_must_be_positive_and_cannot_exceed_what_was_ordered(self):
        case = self._case()
        with self.assertRaises(ValidationError):
            self._line(case, qty=0)
        with self.assertRaises(ValidationError):
            self._line(case, qty=-1)
        self._line(case, "SKU-A", 2.0)
        with self.assertRaises(ValidationError):
            self._line(case, "SKU-A", 2.0)                 # 2 + 2 > 3 ordered
        with self.assertRaises(ValidationError):
            self._line(case, "SKU-NOPE", 1.0)              # never ordered
        self._line(case, "SKU-A", 1.0)                     # exactly the remaining unit is fine

    def test_cases_must_reference_an_order_or_shipment_of_the_same_instance(self):
        with self.assertRaises(ValidationError):
            self.env["shipstation.return.case"].with_user(self.user_manager).create(
                {"instance_id": self.instance.id})
        other = self.make_order_sync(self.instance_b, shipstation_order_id="8002")
        with self.assertRaises(ValidationError):
            self.env["shipstation.return.case"].with_user(self.user_admin).create(
                {"instance_id": self.instance.id, "order_id": other.id})

    def test_approval_actions_are_admin_only_and_company_scoped(self):
        case = self._case()
        self._line(case)
        case.action_submit_review()
        for user in (self.user_manager, self.user_reader, self.user_plain):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                case.with_user(user).action_approve_manual_handling()
        with self.assertRaises(AccessError):
            case.with_user(self.user_manager_b).action_submit_review()

    def test_shipment_can_start_a_return_for_managers_only(self):
        shipment = self.make_shipment_sync()
        action = shipment.with_user(self.user_manager).action_start_return_case()
        self.assertEqual(action["context"]["default_shipment_id"], shipment.id)
        with self.assertRaises(AccessError):
            shipment.with_user(self.user_reader).action_start_return_case()
