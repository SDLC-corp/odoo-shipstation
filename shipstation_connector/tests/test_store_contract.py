"""The store-id contract with ShipStation V1, using the payload shapes ShipStation documents.

Documented facts (ShipStation V1 API reference):
* an order carries its store ONLY at ``advancedOptions.storeId``; there is no top-level ``storeId``;
* ``advancedOptions.storeId`` can only be set when an order is created;
* a shipment carries NO store identifier at all; list calls are scoped by the ``storeId`` query filter.

An earlier version required a top-level ``storeId`` to equal the configured store, which would have
rejected every order and shipment of any connection with a store id. The old fixtures put that field in,
so nothing noticed.
"""
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import FakeResponse, ShipStationCommon


@tagged("post_install", "-at_install", "shipstation")
class TestStoreContract(ShipStationCommon):

    def _mirror(self, order_id="9001"):
        return self.env["shipstation.order.sync"].search([
            ("instance_id", "=", self.instance.id), ("shipstation_order_id", "=", order_id)])

    # ---- the fixtures themselves must stay faithful -----------------------------------------------------
    def test_fixtures_have_the_documented_shapes(self):
        order = self.order_payload()
        self.assertNotIn("storeId", order, "V1 orders have no top-level storeId")
        self.assertEqual(order["advancedOptions"]["storeId"], 111)
        for absent in ("customerName", "currencyCode"):
            self.assertNotIn(absent, order, "not a field of the V1 Order model")
        self.assertNotIn("advancedOptions.storeId", self.order_payload(store_id=None)["advancedOptions"])
        shipment = self.shipment_payload()
        self.assertNotIn("storeId", shipment)
        self.assertIsNone(shipment["advancedOptions"])

    def test_store_extraction_helper(self):
        remote = self.instance._remote_store_id
        self.assertEqual(remote({"advancedOptions": {"storeId": 111}}), "111")
        self.assertEqual(remote({"storeId": 222}), "222")                    # tolerated, never required
        self.assertEqual(remote({"storeId": 222, "advancedOptions": {"storeId": 111}}), "222")
        for none in ({}, {"advancedOptions": None}, {"advancedOptions": {}}, {"advancedOptions": {"storeId": None}},
                     None, "text", []):
            self.assertEqual(remote(none), "", none)

    # ---- orders ----------------------------------------------------------------------------------------------
    def test_a_documented_order_is_imported_with_its_store(self):
        self.make_product("SS-SKU-1")
        self.assertEqual(self.instance._import_order_record(self.order_payload()), "processed")
        sale = self.env["sale.order"].search([("shipstation_order_id", "=", "9001")])
        self.assertEqual((sale.shipstation_store_id, sale.shipstation_instance_id), ("111", self.instance))
        self.assertEqual(self._mirror().shipstation_store_id, "111")
        self.assertEqual(self._mirror().customer_name, "Import Customer", "taken from billTo.name")

    def test_an_order_naming_another_store_is_refused(self):
        self.make_product("SS-SKU-1")
        self.assertRaises(UserError, self.instance._import_order_record, self.order_payload(store_id=999))
        self.assertFalse(self.env["sale.order"].search([("shipstation_order_id", "=", "9001")]))

    def test_an_order_naming_no_store_is_trusted_to_the_request_filter(self):
        self.make_product("SS-SKU-1")
        self.instance._import_order_record(self.order_payload(store_id=None))
        self.assertEqual(self.env["sale.order"].search([("shipstation_order_id", "=", "9001")]).shipstation_store_id, "111")

    def test_order_sync_requests_are_filtered_by_the_configured_store(self):
        self.make_product("SS-SKU-1")
        self.http.add("GET", "/orders", {"orders": [self.order_payload()]})
        result = self.instance._sync_orders(mode="cron")
        self.assertEqual((result["processed"], result["failed"]), (1, 0))
        self.assertEqual(self.http.calls[0]["params"]["storeId"], "111")

    # ---- shipments --------------------------------------------------------------------------------------------
    def test_documented_shipments_without_any_store_sync_and_link(self):
        sale = self.link_sale_to_shipstation(self.make_confirmed_sale())
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001, tracking="T-REAL")]})
        result = self.instance._sync_shipments(mode="cron")
        self.assertEqual((result["linked"], result["failed"]), (1, 0), result)
        self.assertTrue(result["checkpoint_advanced"])
        for call in self.http.calls_to("GET", "/shipments"):
            self.assertEqual(call["params"]["storeId"], "111", "the filter is the only store scoping shipments get")
        record = self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")])
        self.assertEqual((record.link_state, record.shipstation_store_id), ("linked", "111"))
        self.assertEqual(sale.picking_ids.carrier_tracking_ref, "T-REAL")

    def test_a_shipment_that_names_another_store_is_still_refused(self):
        self.assertRaises(UserError, self.instance._process_shipment_record, self.shipment_payload(storeId=999))

    def test_shipment_lookup_for_one_order_keeps_unlabelled_shipments(self):
        self.link_sale_to_shipstation(self.make_confirmed_sale())
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001), self.shipment_payload(5002, tracking="T2")]})
        records = self.env["shipstation.shipment.sync"].sync_for_order(
            self.instance, {"orderId": 9001, "orderNumber": "SO-9001"}, create_placeholder=False)
        self.assertEqual(len(records), 2, "shipments name no store, so none may be filtered out locally")

    # ---- webhook items -----------------------------------------------------------------------------------------
    def _inbox(self, event, resource):
        return self.env["shipstation.webhook.inbox"].create({
            "instance_id": self.instance.id, "event_type": event, "fingerprint": event + resource,
            "resource_url": "https://ssapi.shipstation.com/%s?storeID=111&importBatch=7" % resource})

    def test_webhook_shipments_without_a_store_are_processed_not_skipped(self):
        self.instance.write({"webhook_shipment_update": True})
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001)], "pages": 1})
        self._inbox("SHIP_NOTIFY", "shipments")._process_v1_notification()
        self.assertTrue(self.env["shipstation.shipment.sync"].search([("shipstation_shipment_id", "=", "5001")]))

    def test_webhook_orders_are_filtered_by_the_store_they_name(self):
        self.instance.write({"webhook_order_create": True})
        self.http.add("GET", "/orders", {"orders": [self.order_payload(9001), self.order_payload(9002, "SO-2", store_id=999),
                                                     self.order_payload(9003, "SO-3", store_id=None)], "pages": 1})
        self._inbox("ORDER_NOTIFY", "orders")._process_v1_notification()
        self.assertTrue(self._mirror("9001"))
        self.assertFalse(self._mirror("9002"), "an order naming another store is skipped")
        self.assertTrue(self._mirror("9003"), "an order naming no store is accepted")

    # ---- pushing orders ------------------------------------------------------------------------------------------
    def test_push_assigns_the_store_via_advanced_options_and_only_when_creating(self):
        order = self.make_confirmed_sale()
        created = self.instance._build_order_payload(order)
        self.assertNotIn("storeId", created, "a top-level storeId is ignored: the order would land in the wrong store")
        self.assertEqual(created["advancedOptions"], {"storeId": 111})
        self.assertNotIn("orderId", created)
        self.link_sale_to_shipstation(order, ss_order_id="4242")
        updated = self.instance._build_order_payload(order)
        self.assertEqual(updated["orderId"], 4242)
        self.assertNotIn("storeId", updated)
        self.assertNotIn("storeId", updated.get("advancedOptions", {}), "the store can only be set when creating")

    def test_mirror_push_follows_the_same_rule(self):
        source = str({"orderNumber": "SO-NEW", "orderDate": "2026-11-05T08:00:00", "customerEmail": "a@example.test",
                      "billTo": {"name": "A", "street1": "1 St", "city": "X", "postalCode": "1", "country": "US"},
                      "shipTo": {"name": "A", "street1": "1 St", "city": "X", "postalCode": "1", "country": "US"},
                      "items": [{"sku": "S", "name": "S", "quantity": 1, "unitPrice": 1.0}]})
        local = self.make_order_sync(shipstation_order_id=False, shipstation_order_number="SO-NEW",
                                     shipstation_store_id=False, payload=source)
        payload = local._prepare_createorder_payload()
        self.assertEqual(payload["advancedOptions"], {"storeId": 111})
        self.assertNotIn("storeId", payload)
        known = self.make_order_sync(shipstation_order_id="55", shipstation_order_number="SO-OLD", payload=source)
        self.assertNotIn("advancedOptions", known._prepare_createorder_payload())

    # ---- upgrade: sales imported by the published build --------------------------------------------------------------
    def _legacy_sale(self, store=False, company=None):
        company = company or self.company
        partner = self.env["res.partner"].create({"name": "Legacy Buyer"})
        return self.env["sale.order"].with_company(company).create({
            "partner_id": partner.id, "company_id": company.id,
            "shipstation_order_id": "9001", "shipstation_order_number": "SO-9001", "shipstation_store_id": store,
            "order_line": [(0, 0, {"product_id": self.make_product("LEG-1").id, "product_uom_qty": 1})]})

    def test_published_build_orders_with_a_blank_store_are_adopted_not_duplicated(self):
        """The published build recorded a blank store (real orders have none at the top level).

        Runs in a company of its own so "the company's only connection" holds in any database,
        including one that already carries other connections in the main company.
        """
        solo_company = self.env["res.company"].create({"name": "Legacy Solo Company"})
        solo = self.env["shipstation.instance"].with_context(skip_store_autofill=True).create({
            "name": "Solo connection", "api_key": "TEST-KEY-S", "api_secret": "TEST-SECRET-S",
            "store_id": "444", "company_id": solo_company.id})
        legacy = self._legacy_sale(store=False, company=solo_company)
        self.assertEqual(solo._find_order("9001", "SO-9001", "444"), legacy)
        self.make_product("SS-SKU-1")
        solo._import_order_record(self.order_payload(store_id=444))
        sales = self.env["sale.order"].with_company(solo_company).search([("shipstation_order_id", "=", "9001")])
        self.assertEqual(sales, legacy, "the existing order was updated, a second one was not created")
        self.assertEqual((legacy.shipstation_instance_id, legacy.shipstation_store_id), (solo, "444"))

    def test_published_build_placeholder_with_a_blank_store_is_adopted_by_its_real_shipment(self):
        """Pending placeholders written by the published build have no store; they must not be orphaned."""
        self.link_sale_to_shipstation(self.make_confirmed_sale())
        placeholder = self.make_shipment_sync(
            shipstation_shipment_id=False, tracking_number=False, shipstation_store_id=False,
            status="shipped_pending_tracking")
        self.http.add("GET", "/shipments", {"shipments": [self.shipment_payload(5001, tracking="T-ADOPT")]})
        self.instance._sync_shipments(mode="cron")
        rows = self.env["shipstation.shipment.sync"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(rows, placeholder, "the placeholder became the real shipment; no second row appeared")
        self.assertEqual((placeholder.shipstation_shipment_id, placeholder.tracking_number, placeholder.shipstation_store_id),
                         ("5001", "T-ADOPT", "111"))

    def test_a_placeholder_of_another_instance_is_never_adopted(self):
        other = self.make_shipment_sync(instance=self.instance_b, shipstation_shipment_id=False,
                                        tracking_number=False, shipstation_store_id=False)
        self.assertFalse(self.env["shipstation.shipment.sync"]._find_existing_shipment(
            self.instance, order_id="9001", store_id="111"))
        self.assertTrue(other)

    def test_a_blank_store_is_not_adopted_when_the_company_has_several_connections(self):
        legacy = self._legacy_sale(store=False)
        self.env["shipstation.instance"].with_context(skip_store_autofill=True).create({
            "name": "Second in company A", "api_key": "k", "api_secret": "s", "store_id": "333",
            "company_id": self.company.id})
        self.assertFalse(self.instance._find_order("9001", "SO-9001", "111"),
                         "with two connections the owner of a blank-store order is ambiguous")
        self.assertFalse(legacy.shipstation_instance_id)

    def test_an_order_recorded_under_another_store_is_never_adopted(self):
        self._legacy_sale(store="222")
        self.assertFalse(self.instance._find_order("9001", "SO-9001", "111"))
