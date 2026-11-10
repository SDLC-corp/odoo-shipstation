"""The data repairs run when upgrading a database installed from the published 19.0.1.0.0 build."""
import importlib.util

from odoo.tests.common import tagged
from odoo.tools import file_path

from .common import ShipStationCommon


def load_migration():
    path = file_path("shipstation_connector/migrations/19.0.1.1.0/post-migration.py")
    spec = importlib.util.spec_from_file_location("shipstation_post_migration_1_1_0", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("post_install", "-at_install", "shipstation")
class TestUpgradeFromPublishedBuild(ShipStationCommon):

    def _tokens(self, *instances):
        self.env.invalidate_all()
        return [self.env["shipstation.instance"].browse(i.id).sudo().webhook_token for i in instances]

    def test_connections_that_share_a_webhook_secret_each_get_their_own(self):
        """Odoo gives every existing row the same value when it adds a column with a callable default."""
        Instance = self.env["shipstation.instance"].with_context(skip_store_autofill=True)
        third = Instance.create({"name": "Third", "api_key": "k", "api_secret": "s", "company_id": self.company.id})
        unique = Instance.create({"name": "Unique", "api_key": "k", "api_secret": "s", "company_id": self.company.id})
        self.env.cr.execute("UPDATE shipstation_instance SET webhook_token = 'SHARED' WHERE id IN %s",
                            ((self.instance.id, self.instance_b.id, third.id),))
        keep = self._tokens(unique)[0]

        load_migration().migrate(self.env.cr, "19.0.1.0.0")

        shared = self._tokens(self.instance, self.instance_b, third)
        self.assertEqual(len(set(shared)), 3, "each connection must have a different secret")
        self.assertNotIn("SHARED", shared)
        self.assertTrue(all(len(token) >= 40 for token in shared))
        self.assertEqual(self._tokens(unique)[0], keep, "a secret that was already unique is never rotated")

    def test_missing_secret_is_filled_and_rerunning_changes_nothing(self):
        self.env.cr.execute("UPDATE shipstation_instance SET webhook_token = NULL WHERE id = %s", (self.instance.id,))
        migration = load_migration()
        migration.migrate(self.env.cr, "19.0.1.0.0")
        first = self._tokens(self.instance, self.instance_b)
        self.assertTrue(first[0])
        migration.migrate(self.env.cr, "19.0.1.0.0")
        self.assertEqual(self._tokens(self.instance, self.instance_b), first, "idempotent")

    def test_blank_order_currencies_are_filled_from_the_connection(self):
        """V1 orders carry no currency, so the published build stored mirrors without one."""
        eur = self.env.ref("base.EUR")
        eur.active = True
        self.instance_b.order_currency_id = eur
        blank_a = self.make_order_sync(currency=False)
        blank_b = self.make_order_sync(instance=self.instance_b, shipstation_order_id="9002", currency="")
        kept = self.make_order_sync(shipstation_order_id="9003", currency="CAD")
        self.env.flush_all()
        load_migration().migrate(self.env.cr, "19.0.1.0.0")
        self.env.invalidate_all()
        self.assertEqual(blank_a.currency, self.company.currency_id.name)
        self.assertEqual(blank_b.currency, "EUR", "a configured order currency wins over the company's")
        self.assertEqual(kept.currency, "CAD", "a stated currency is never overwritten")

    def test_a_fresh_install_runs_nothing(self):
        before = self._tokens(self.instance, self.instance_b)
        self.env.cr.execute("UPDATE shipstation_instance SET webhook_token = 'SAME' WHERE id IN %s",
                            ((self.instance.id, self.instance_b.id),))
        load_migration().migrate(self.env.cr, False)
        self.assertEqual(self._tokens(self.instance, self.instance_b), ["SAME", "SAME"])
        self.assertNotEqual(before, ["SAME", "SAME"])

    def test_only_provably_linked_historical_shipments_are_marked_linked(self):
        sale = self.link_sale_to_shipstation(self.make_confirmed_sale())
        picking = sale.picking_ids
        picking.write({"shipstation_shipment_id": "MIG-900", "shipstation_tracking_number": "1ZOLD"})
        provable = self.make_shipment_sync(shipstation_shipment_id="MIG-900")                # one delivery has it
        orphan = self.make_shipment_sync(shipstation_shipment_id="MIG-999")                  # nothing has it
        # ambiguous: two deliveries carry the same shipment id -> a person must decide
        twin_sale = self.link_sale_to_shipstation(self.make_confirmed_sale(), ss_order_id="9002", number="SO-2")
        for p in (twin_sale.picking_ids | picking.copy()):
            p.write({"shipstation_shipment_id": "MIG-950"})
        ambiguous = self.make_shipment_sync(shipstation_shipment_id="MIG-950", shipstation_order_id="9002")
        # another company's delivery must never be attached to this company's shipment
        foreign_sale = self.env["sale.order"].with_company(self.company_b).create({
            "partner_id": self.env["res.partner"].create({"name": "B customer"}).id, "company_id": self.company_b.id,
            "order_line": [(0, 0, {"product_id": self.make_product("B-1").id, "product_uom_qty": 1})]})
        foreign_sale.action_confirm()
        foreign_sale.picking_ids.write({"shipstation_shipment_id": "MIG-960"})
        cross_company = self.make_shipment_sync(shipstation_shipment_id="MIG-960")
        already = self.make_shipment_sync(shipstation_shipment_id="MIG-970", link_state="review")

        self.env.flush_all()   # the migration is raw SQL: pending ORM writes must reach the database first
        self.env.cr.execute("SELECT count(*) FROM stock_picking WHERE shipstation_shipment_id = 'MIG-950'")
        self.assertEqual(self.env.cr.fetchone()[0], 2, "fixture: two deliveries really carry the same shipment id")

        count = load_migration()._link_shipments_already_on_a_delivery(self.env.cr)

        self.env.invalidate_all()
        self.assertGreaterEqual(count, 1)     # other, real historical shipments in the database may qualify too
        self.assertEqual((provable.link_state, provable.picking_id), ("linked", picking))
        for untouched in (orphan, ambiguous, cross_company):
            with self.subTest(shipment=untouched.shipstation_shipment_id):
                self.assertEqual((untouched.link_state, bool(untouched.picking_id)), ("pending", False))
        self.assertEqual(already.link_state, "review", "work already triaged by a person is not overwritten")
