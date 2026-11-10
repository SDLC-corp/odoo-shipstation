"""The V1 webhook exercised over real HTTP on Odoo 15 (application/json posts on a type="http" route)."""
import json
from unittest import mock

from odoo import http
from odoo.tests.common import tagged

from ..controllers import shipstation_webhook
from .common import ShipStationHttpCommon

BODY = {"resource_url": "https://ssapi.shipstation.com/orders?storeID=111&importBatch=HTTP1",
        "resource_type": "ORDER_NOTIFY"}


@tagged("post_install", "-at_install", "shipstation")
class TestWebhookHttp(ShipStationHttpCommon):

    def setUp(self):
        super().setUp()
        self.instance.write({"webhook_order_create": True, "webhook_shipment_update": False})
        # Anonymous session bound to the test database (the test config's dbfilter matches several).
        self.authenticate(None, None)

    def _post(self, body, token=None, content_type="application/json"):
        url = "%s/shipstation/webhook/v1/%s/%s" % (
            self.base_url(), self.instance.id, token or self.instance.webhook_token)
        data = body if isinstance(body, str) else json.dumps(body)
        return self.url_open(url, data=data, headers={"Content-Type": content_type}, timeout=30)

    def _rows(self):
        return self.env["shipstation.webhook.inbox"].search([("instance_id", "=", self.instance.id)])

    def test_notification_is_queued_once_and_answered_with_plain_http_codes(self):
        response = self._post(BODY)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"status": "accepted"})
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.state, "pending")
        self.assertEqual(self._post(BODY).json(), {"status": "duplicate"})
        self.assertEqual(self._post(BODY, token="wrong-token").status_code, 404)
        self.assertEqual(self._post("not json").status_code, 422)
        evil = dict(BODY, resource_url="https://evil.example.com/orders?storeID=111")
        self.assertEqual(self._post(evil).status_code, 422)
        self.assertEqual(len(self._rows()), 1)
        self.assertFalse(self.http.calls, "receiving a notification must not call ShipStation")

    def test_notification_in_the_request_that_loads_the_registry_is_not_lost(self):
        # The request-type override exists only once this module's code is imported. Simulate the
        # first request after a restart, built by Odoo's own get_request (a JSON-RPC request): it
        # must still be queued and answered 202, never with a JSON-RPC error and HTTP 200.
        with mock.patch.object(http.Root, "get_request", shipstation_webhook._root_get_request):
            response = self._post(BODY)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"status": "accepted"})
        self.assertEqual(len(self._rows()), 1)
