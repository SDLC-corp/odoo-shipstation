"""Runtime security: ACLs, record rules, secret fields, RPC entry points and multi-company."""
import logging

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from ..controllers.chatbot_controller import active_company_ids
from ..controllers.shipstation_webhook import WebhookTokenLogFilter, redact_webhook_token
from ..services.chatbot_service import ShipStationSimpleChatbotService
from .common import ShipStationCommon


@tagged("post_install", "-at_install", "shipstation")
class TestSecurityRuntime(ShipStationCommon):

    # ---- secrets -------------------------------------------------------------
    def test_credentials_are_hidden_from_non_admins(self):
        secret_fields = ("api_key", "api_secret", "v2_api_key", "webhook_token", "webhook_target_url")
        for user in (self.user_manager, self.user_reader):
            instance = self.instance.with_user(user)
            visible = instance.fields_get()
            for name in secret_fields:
                self.assertNotIn(name, visible, "%s can see field %s" % (user.login, name))
                with self.assertRaises(AccessError):
                    instance.read([name])

    def test_admin_can_read_credentials(self):
        self.assertEqual(self.instance.with_user(self.user_admin).api_key, "TEST-KEY-A")

    def test_secret_values_never_appear_in_list_or_search_results(self):
        found = self.instance.with_user(self.user_manager).search_read([], ["name"])
        self.assertTrue(found)
        self.assertNotIn("TEST-KEY", str(found))
        with self.assertRaises(AccessError):
            self.instance.with_user(self.user_manager).search([("api_key", "=", "TEST-KEY-A")])

    def test_secret_fields_cannot_be_probed_by_grouping_sorting_or_related_paths(self):
        """A secret must never work as a search oracle for roles below Admin: not in a domain, a sort
        order, a grouping or a dotted path from another model. (Odoo 15-17 do not enforce field groups
        in all of these; their ports guard the instance model explicitly. This test keeps every version honest.)"""
        Instance = self.instance.with_user(self.user_manager)
        # Odoo 16 read_group API: _read_group(domain, fields, groupby) (19: groupby, aggregates).
        probes = (
            lambda: Instance._read_group([], [], ["api_key"]),
            lambda: Instance._read_group([("webhook_token", "!=", False)], [], ["name"]),
            lambda: Instance.search([], order="api_secret desc"),
            lambda: self.env["shipstation.order.sync"].with_user(self.user_manager).search(
                [("instance_id.api_key", "=", "TEST-KEY-A")]),
        )
        for index, probe in enumerate(probes):
            with self.subTest(probe=index), self.assertRaises(AccessError):
                probe()
        # the admin keeps full use of the fields
        self.assertTrue(self.instance.with_user(self.user_admin).search([("api_key", "=", "TEST-KEY-A")]))

    # ---- privileged instance actions -------------------------------------------
    def test_non_admin_cannot_run_instance_operations_and_no_http_leaves(self):
        before = len(self.http.calls)
        actions = ("action_test_v1_connection", "action_test_v2_connection", "action_sync_orders",
                   "action_sync_shipments", "action_sync_products", "action_sync_customers",
                   "action_sync_inventory", "action_push_orders", "action_push_products",
                   "action_push_customers", "action_discover_carrier_catalog",
                   "action_rotate_v1_webhook_token")
        for user in (self.user_manager, self.user_reader, self.user_plain):
            instance = self.instance.with_user(user)
            for action in actions:
                with self.subTest(user=user.login, action=action), self.assertRaises(AccessError):
                    getattr(instance, action)()
        self.assertEqual(len(self.http.calls), before, "A denied action still reached the network")

    def test_api_helpers_refuse_readers(self):
        reader = self.instance.with_user(self.user_reader)
        with self.assertRaises(AccessError):
            reader._ss_request("GET", "/stores")
        with self.assertRaises(AccessError):
            reader._ss_v2_request("GET", "/carriers")
        self.assertFalse(self.http.calls)

    # ---- shipping operations -----------------------------------------------------
    def test_reader_and_plain_user_cannot_quote_or_buy_labels(self):
        order = self.make_order_sync(
            carrier_code="ups", service_code="ups_ground", package_code="package")
        for user in (self.user_reader, self.user_plain):
            rec = order.with_user(user)
            for action in ("action_get_rates", "action_create_label", "action_select_best_rate"):
                with self.subTest(user=user.login, action=action), self.assertRaises(AccessError):
                    getattr(rec, action)()
        self.assertFalse(self.http.calls)

    def test_batch_and_return_state_cannot_be_forged_by_direct_write(self):
        order = self.make_order_sync()
        batch = self.env["shipstation.fulfillment.batch"].with_user(self.user_manager).create(
            {"instance_id": self.instance.id})
        with self.assertRaises(AccessError):
            batch.write({"state": "ready"})
        case = self.env["shipstation.return.case"].with_user(self.user_manager).create(
            {"instance_id": self.instance.id, "order_id": order.id})
        for vals in ({"state": "approved"}, {"approved_by_id": self.user_manager.id}):
            with self.assertRaises(AccessError):
                case.write(vals)
        # a manager may submit but only an admin may approve
        case.env["shipstation.return.case.line"].create({"case_id": case.id, "sku": "X", "quantity": 1})
        case.action_submit_review()
        with self.assertRaises(AccessError):
            case.action_approve_manual_handling()
        case.with_user(self.user_admin).action_approve_manual_handling()
        self.assertEqual(case.state, "approved")
        self.assertEqual(case.approved_by_id, self.user_admin)

    # ---- multi-company ---------------------------------------------------------------
    def test_company_isolation_for_every_company_scoped_model(self):
        order_b = self.make_order_sync(self.instance_b, shipstation_order_id="7001",
                                       shipstation_order_number="SO-B-7001")
        ship_b = self.make_shipment_sync(self.instance_b, shipstation_order_id="7001")
        for user, own, foreign in ((self.user_manager, self.instance, self.instance_b),
                                   (self.user_manager_b, self.instance_b, self.instance)):
            Instance = self.env["shipstation.instance"].with_user(user)
            visible = Instance.search([])
            self.assertIn(own, visible, user.login)
            self.assertNotIn(foreign, visible, user.login)
            self.assertEqual(visible.mapped("company_id"), user.company_id, "only the user's own company")
            with self.assertRaises(AccessError):
                foreign.with_user(user).read(["name"])
        for record in (order_b, ship_b):
            with self.assertRaises(AccessError):
                record.with_user(self.user_manager).read(["id"])
            self.assertFalse(
                self.env[record._name].with_user(self.user_manager).search([("id", "=", record.id)]))

    def test_cross_company_batch_and_return_creation_is_rejected(self):
        order_b = self.make_order_sync(self.instance_b, shipstation_order_id="7002")
        with self.assertRaises(AccessError):
            self.env["shipstation.fulfillment.batch"].with_user(self.user_manager).create(
                {"instance_id": self.instance_b.id})
        # the company rule hides the foreign order from the manager ...
        with self.assertRaises(AccessError):
            order_b.with_user(self.user_manager).read(["id"])
        # ... and a return that mixes instances is rejected by the model constraint
        with self.assertRaises(ValidationError):
            self.env["shipstation.return.case"].with_user(self.user_admin).create(
                {"instance_id": self.instance.id, "order_id": order_b.id})

    # ---- AI assistant (chatbot) ----------------------------------------------------------
    def test_chatbot_denies_users_without_shipstation_access(self):
        self.make_order_sync(shipstation_order_number="SO-SECRET-1", customer_name="Secret Customer")
        for user in (self.user_plain, self.user_portal):
            service = ShipStationSimpleChatbotService(self.env(user=user))
            for prompt in ("recent orders", "recent shipments", "low stock", "sync status",
                           "pending tracking", "today shipments"):
                with self.subTest(user=user.login, prompt=prompt), self.assertRaises(AccessError):
                    service.get_reply(prompt)

    def test_chatbot_tolerates_malformed_input(self):
        service = ShipStationSimpleChatbotService(self.env(user=self.user_reader))
        for junk in (None, 12345, {"a": 1}, ["x"], "", "x" * 100000, "\u0000\ud83d"):
            with self.subTest(junk=str(junk)[:20]):
                self.assertTrue(service.get_reply(junk)["reply"])

    def test_chatbot_respects_company_isolation(self):
        self.make_order_sync(self.instance, shipstation_order_id="1", shipstation_order_number="SO-A-1")
        self.make_order_sync(self.instance_b, shipstation_order_id="2",
                             shipstation_order_number="SO-LEAK-B", customer_name="Leaky B")
        reply = ShipStationSimpleChatbotService(self.env(user=self.user_manager)).get_reply(
            "recent orders")["reply"]
        self.assertIn("SO-A-1", reply)
        self.assertNotIn("SO-LEAK-B", reply)
        self.assertNotIn("Leaky B", reply)

    def test_chatbot_follows_the_company_switcher(self):
        """Found in the browser pass: the JSON-RPC call carried no company context, so a user with two
        companies saw both companies' orders while only one was active in the switcher."""
        self.make_order_sync(self.instance, shipstation_order_id="1", shipstation_order_number="SO-ACTIVE-A")
        self.make_order_sync(self.instance_b, shipstation_order_id="2", shipstation_order_number="SO-OTHER-B")
        admin = self.user_admin
        a, b = self.company.id, self.company_b.id
        self.assertEqual(active_company_ids(admin, [a]), [a])
        self.assertEqual(active_company_ids(admin, [b, a, b]), [b, a])
        foreign = self.env["res.company"].create({"name": "Not Mine"}).id
        for junk in (None, "1", {"a": 1}, [True], ["1"], [foreign], [1.0]):
            with self.subTest(junk=junk):
                self.assertEqual(active_company_ids(admin, junk), [], "never a company the user lacks")
        env = self.env(user=admin, context={"allowed_company_ids": active_company_ids(admin, [a])})
        reply = ShipStationSimpleChatbotService(env).get_reply("recent orders")["reply"]
        self.assertIn("SO-ACTIVE-A", reply)
        self.assertNotIn("SO-OTHER-B", reply)

    def test_webhook_secret_is_redacted_from_the_access_log(self):
        """Found in the HTTP pass: Odoo's access log wrote the full callback URL, secret included."""
        token = "S3cr3t-Token_value"
        line = "POST /shipstation/webhook/v1/7/%s HTTP/1.1" % token
        access_logger = logging.getLogger("werkzeug")
        self.assertTrue(any(isinstance(f, WebhookTokenLogFilter) for f in access_logger.filters))
        record = access_logger.makeRecord("werkzeug", logging.INFO, __file__, 1, '"%s" %s %s', (line, "202", "-"), None)
        for log_filter in access_logger.filters:
            log_filter.filter(record)
        message = record.getMessage()
        self.assertNotIn(token, message)
        self.assertIn("/shipstation/webhook/v1/7/[redacted] HTTP/1.1", message)
        self.assertEqual(redact_webhook_token("GET /web HTTP/1.1"), "GET /web HTTP/1.1")
        self.assertEqual(redact_webhook_token(None), None)

    def test_rate_quotes_cannot_be_typed_in_on_the_order_form(self):
        arch = self.env["shipstation.order.sync"].with_user(self.user_manager).get_views([(False, "form")])
        arch = arch["views"]["form"]["arch"]
        # Odoo 16 postprocesses readonly="1" into the modifiers JSON attribute
        self.assertRegex(arch, r'<field name="rate_ids"[^>]*modifiers="[^"]*&quot;readonly&quot;: true')
        self.assertRegex(arch, r'<tree[^>]*create="0"')  # Odoo 16 list tag

    def test_chatbot_reader_gets_answers_without_privileged_data(self):
        self.make_shipment_sync(tracking_number="TRK-READER")
        reply = ShipStationSimpleChatbotService(self.env(user=self.user_reader)).get_reply(
            "recent shipments")["reply"]
        self.assertIn("TRK-READER", reply)
