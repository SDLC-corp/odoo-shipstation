"""Shared fixtures for the ShipStation connector Odoo test-suite.

Safety rules enforced here for every test:

* No real HTTP request can leave the process. ``requests.request/get/post/put/delete`` are
  replaced by a router that raises ``AssertionError`` for any URL that a test did not
  explicitly mock. A live ShipStation call (and therefore a label purchase, charge or
  refund) is impossible from this suite.
* Credentials are obviously fake (``TEST-...``).
* ``time.sleep`` used by retry/back-off loops is stubbed so failure paths are fast.
"""
import base64
import json
from unittest import mock
from urllib.parse import urlsplit

from odoo.tests.common import HttpCase, TransactionCase, new_test_user

MODULE = "odoo.addons.shipstation_connector"
GROUP_ADMIN = "shipstation_connector.group_shipstation_admin"
GROUP_MANAGER = "shipstation_connector.group_shipstation_manager"
GROUP_READER = "shipstation_connector.group_shipstation_user_readonly"

# A tiny but structurally valid-looking PDF; ShipStation returns labels as base64 PDFs.
FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
FAKE_LABEL_B64 = base64.b64encode(FAKE_PDF).decode()


class FakeResponse:
    """Minimal ``requests.Response`` stand-in."""

    def __init__(self, payload=None, status=200, headers=None, text=None):
        self.status_code = status
        self.headers = headers or {}
        if text is not None:
            self.text = text
            self.content = text.encode()
        elif payload is None:
            self.text = ""
            self.content = b""
        else:
            self.text = json.dumps(payload)
            self.content = self.text.encode()
        self._payload = payload

    def json(self):
        if self._payload is None and not self.text:
            raise ValueError("no body")
        return self._payload if self._payload is not None else json.loads(self.text)


class HttpRouter:
    """Route mocked HTTP calls by ``(METHOD, path-prefix)`` and remember every call."""

    def __init__(self):
        self.routes = []
        self.calls = []
        self.defaults = []

    def reset(self):
        """Forget calls and test routes; standing defaults (see ``add_default``) stay registered."""
        self.routes = [dict(route) for route in self.defaults]
        self.calls = []

    def add_default(self, method, path, response=None, **kwargs):
        """A route that survives ``reset()``; a test can still override it with ``add``."""
        self.add(method, path, response, **kwargs)
        self.defaults.append(self.routes[-1])

    def add(self, method, path, response=None, status=200, headers=None, callback=None, exc=None,
            sequence=None):
        """Register a route.

        ``response`` is returned verbatim (a list payload is a list payload). ``sequence`` is a list
        of payloads consumed in order, the last one repeating. A later route for the same path
        replaces an earlier one; a longer path always beats a shorter prefix.
        """
        self.routes.append({
            "method": method.upper(), "path": path, "response": response, "sequence": sequence,
            "status": status, "headers": headers, "callback": callback, "exc": exc,
        })

    def calls_to(self, method, path):
        return [c for c in self.calls if c["method"] == method.upper() and c["path"].startswith(path)]

    def __call__(self, method, url=None, **kwargs):
        method = method.upper()
        parts = urlsplit(url)
        record = {"method": method, "url": url, "host": parts.netloc, "path": parts.path,
                  "params": kwargs.get("params"), "json": kwargs.get("json"),
                  "headers": kwargs.get("headers"), "auth": kwargs.get("auth"), "timeout": kwargs.get("timeout")}
        self.calls.append(record)
        matches = [(len(r["path"]), i, r) for i, r in enumerate(self.routes)
                   if r["method"] == method and parts.path.startswith(r["path"])]
        if not matches:
            raise AssertionError("Unmocked ShipStation HTTP call: %s %s" % (method, url))
        route = max(matches, key=lambda m: (m[0], m[1]))[2]
        if route["exc"]:
            raise route["exc"]
        if route["callback"]:
            return route["callback"](record)
        payload = route["response"]
        if route["sequence"] is not None:
            seq = route["sequence"]
            payload = seq.pop(0) if len(seq) > 1 else seq[0]
        return FakeResponse(payload, route["status"], route["headers"])


class ShipStationFixtures:
    """Mixin: network guard, companies, one user per role, two instances and data helpers."""

    @classmethod
    def setup_shipstation_fixtures(cls):
        # registry.cursor() now yields cursors on the test transaction, so code that writes on
        # a separate cursor (durable logs, label claims) is exercised without real commits.
        # Odoo 20 replaced registry_enter_test_mode_cls() with the registry_test_mode() context
        # manager (HttpCase already enters it in its own setUpClass).
        if not cls._registry_patched:
            cls.enterClassContext(cls.registry_test_mode())
        # ---- network guard -------------------------------------------------
        cls.http = HttpRouter()
        patcher = mock.patch("requests.request", side_effect=cls.http)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        for name in ("get", "post", "put", "delete"):
            patcher = mock.patch(
                "requests." + name,
                side_effect=lambda url, _n=name, **kw: cls.http(_n, url, **kw),
            )
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        sleeper = mock.patch(MODULE + ".models.shipstation_sync_base.time")
        cls.sleep = sleeper.start()
        cls.addClassCleanup(sleeper.stop)

        # ---- companies and people --------------------------------------
        cls.company = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "ShipStation Test Company B"})
        both = [(6, 0, [cls.company.id, cls.company_b.id])]
        only_a = [(6, 0, [cls.company.id])]
        cls.user_admin = new_test_user(
            cls.env, login="ss_admin_user", groups="base.group_user," + GROUP_ADMIN,
            company_ids=both, company_id=cls.company.id)
        cls.user_manager = new_test_user(
            cls.env, login="ss_manager_user", groups="base.group_user," + GROUP_MANAGER,
            company_ids=only_a, company_id=cls.company.id)
        cls.user_manager_b = new_test_user(
            cls.env, login="ss_manager_b_user", groups="base.group_user," + GROUP_MANAGER,
            company_ids=[(6, 0, [cls.company_b.id])], company_id=cls.company_b.id)
        cls.user_reader = new_test_user(
            cls.env, login="ss_reader_user", groups="base.group_user," + GROUP_READER,
            company_ids=only_a, company_id=cls.company.id)
        cls.user_plain = new_test_user(
            cls.env, login="ss_plain_user", groups="base.group_user",
            company_ids=only_a, company_id=cls.company.id)
        cls.user_portal = new_test_user(
            cls.env, login="ss_portal_user", groups="base.group_portal",
            company_id=cls.company.id)

        # ---- instances ---------------------------------------------------
        Instance = cls.env["shipstation.instance"].with_context(skip_store_autofill=True)
        cls.instance = Instance.create({
            "name": "Test Instance A", "api_key": "TEST-KEY-A", "api_secret": "TEST-SECRET-A",
            "v2_api_key": "TEST-V2-A", "store_id": "111", "company_id": cls.company.id,
        })
        cls.instance_b = Instance.create({
            "name": "Test Instance B", "api_key": "TEST-KEY-B", "api_secret": "TEST-SECRET-B",
            "store_id": "222", "company_id": cls.company_b.id,
        })

        # A database with Odoo demo data already holds confirmed orders with open deliveries. Mark them
        # as already in ShipStation so push tests see only the orders they create (rolled back after).
        for order in cls.env["sale.order"].search([("state", "=", "sale"), ("shipstation_order_id", "=", False)]):
            order.shipstation_order_id = "pre-test-%s" % order.id

    # ---- helpers -----------------------------------------------------------
    def make_order_sync(self, instance=None, **extra):
        instance = instance or self.instance
        vals = {
            "instance_id": instance.id, "shipstation_order_id": "9001",
            "shipstation_order_number": "SO-9001", "shipstation_store_id": instance.store_id,
            "status": "awaiting_shipment", "customer_name": "Test Customer",
            "total_amount": 50.0, "currency": "USD",
        }
        vals.update(extra)
        return self.env["shipstation.order.sync"].create(vals)

    def make_shipment_sync(self, instance=None, **extra):
        instance = instance or self.instance
        vals = {
            "instance_id": instance.id, "shipstation_shipment_id": "5001",
            "shipstation_order_id": "9001", "shipstation_order_number": "SO-9001",
            "shipstation_store_id": instance.store_id, "tracking_number": "TRK123",
            "carrier_code": "ups",
        }
        vals.update(extra)
        return self.env["shipstation.shipment.sync"].create(vals)

    def make_product(self, sku="SS-SKU-1", name="Test Widget", price=10.0):
        return self.env["product.product"].create({
            "name": name, "default_code": sku, "type": "consu", "is_storable": True,
            "list_price": price,
        })

    def make_confirmed_sale(self, qty=5.0, product=None, instance=None, **order_vals):
        """Confirmed sale order owned by ``instance`` with a real outgoing delivery."""
        instance = instance or self.instance
        product = product or self.make_product()
        partner = self.env["res.partner"].create({"name": "Sale Customer", "email": "buyer@example.test"})
        vals = {
            "partner_id": partner.id, "company_id": instance.company_id.id,
            "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": qty, "price_unit": 10.0})],
        }
        vals.update(order_vals)
        order = self.env["sale.order"].with_company(instance.company_id).create(vals)
        order.action_confirm()
        return order

    def attempt(self, func, *exc_types):
        """Run ``func`` expecting one of ``exc_types`` WITHOUT Odoo's rollback-on-raise savepoint.

        ``assertRaises`` rolls the test cursor back to a savepoint, which would also undo writes the
        code under test deliberately commits on a separate cursor (durable label claims, request
        logs). In production those survive the failure, so the tests must observe them.
        """
        try:
            func()
        except exc_types as exc:
            return exc
        self.fail("%s was not raised" % " / ".join(t.__name__ for t in exc_types))

    def link_sale_to_shipstation(self, order, ss_order_id="9001", number="SO-9001", instance=None):
        instance = instance or self.instance
        order.write({
            "shipstation_instance_id": instance.id, "shipstation_order_id": ss_order_id,
            "shipstation_order_number": number, "shipstation_store_id": instance.store_id,
        })
        return order

    def order_payload(self, order_id=9001, number="SO-9001", store_id=111, status="awaiting_shipment",
                      modify="2026-11-05T09:30:00.0000000", sku="SS-SKU-1", qty=2, **extra):
        """A V1 order exactly as ShipStation documents it: the store is ONLY at advancedOptions.storeId
        (``store_id=None`` omits it), and there is no top-level storeId, customerName or currencyCode."""
        advanced = {"warehouseId": 1, "source": "Shopify"}
        if store_id is not None:
            advanced["storeId"] = store_id
        payload = {
            "orderId": order_id, "orderNumber": number, "advancedOptions": advanced,
            "orderStatus": status, "orderDate": "2026-11-05T08:00:00.0000000",
            "createDate": "2026-11-05T07:59:00.0000000", "customerUsername": "import_buyer",
            "modifyDate": modify,
            "customerEmail": "import@example.test", "orderTotal": 25.0,
            "billTo": {"name": "Import Customer", "street1": "1 Main St", "city": "Austin",
                       "state": "TX", "postalCode": "78701", "country": "US"},
            "shipTo": {"name": "Import Customer", "street1": "1 Main St", "city": "Austin",
                       "state": "TX", "postalCode": "78701", "country": "US"},
            "items": [{"sku": sku, "name": "Test Widget", "quantity": qty, "unitPrice": 12.5}],
        }
        payload.update(extra)
        return payload

    def shipment_payload(self, shipment_id=5001, order_id=9001, number="SO-9001",
                         tracking="TRK123", voided=False, **extra):
        """A V1 shipment as ShipStation documents it: it names NO store (advancedOptions is null);
        the only store scoping is the storeId query filter of the request."""
        payload = {
            "shipmentId": shipment_id, "orderId": order_id, "orderNumber": number, "advancedOptions": None,
            "trackingNumber": tracking, "carrierCode": "ups", "serviceCode": "ups_ground",
            "shipDate": "2026-11-05", "createDate": "2026-11-05T08:00:00.0000000",
            "voided": voided, "voidDate": "2026-11-05T10:00:00.0000000" if voided else None,
        }
        payload.update(extra)
        return payload


class ShipStationCommon(ShipStationFixtures, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_shipstation_fixtures()

    def setUp(self):
        super().setUp()
        self.http.reset()
        self.sleep.reset_mock()
        # Label purchases first check the account has a Ship From location (tests may override this route).
        if not self.http.defaults:
            self.http.add_default("GET", "/warehouses", [{"warehouseId": 1, "warehouseName": "Main", "isDefault": True}])
            self.http.reset()


class ShipStationHttpCommon(ShipStationFixtures, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_shipstation_fixtures()

    def setUp(self):
        super().setUp()
        self.http.reset()
