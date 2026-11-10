"""HTTP transport, error semantics, rate limiting, logging, URL safety and Pacific-time handling."""
from datetime import datetime, timedelta

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import tagged

from ..models.shipstation_sync_base import ShipStationAPIError
from .common import ShipStationCommon


@tagged("post_install", "-at_install", "shipstation")
class TestApiTransport(ShipStationCommon):

    # ---- routing and auth -----------------------------------------------------
    def test_v1_uses_v1_host_with_basic_auth_only(self):
        self.http.add("GET", "/orders", {"orders": []})
        self.instance._ss_request("GET", "/orders", params={"page": 1})
        call = self.http.calls[0]
        self.assertEqual(call["url"], "https://ssapi.shipstation.com/orders")
        self.assertEqual(call["auth"], ("TEST-KEY-A", "TEST-SECRET-A"))
        self.assertIsNone(call["headers"])

    def test_v2_uses_v2_host_with_header_only(self):
        self.http.add("GET", "/v2/carriers", {"carriers": []})
        self.instance._ss_v2_request("GET", "/carriers")
        call = self.http.calls[0]
        self.assertEqual(call["url"], "https://api.shipstation.com/v2/carriers")
        self.assertEqual(call["headers"], {"API-Key": "TEST-V2-A"})
        self.assertIsNone(call["auth"])

    def test_v2_connection_test_hits_documented_carriers_endpoint(self):
        self.http.add("GET", "/v2/carriers", {"carriers": []})
        result = self.instance.with_user(self.user_admin).action_test_v2_connection()
        self.assertEqual(result["params"]["type"], "success")
        call = self.http.calls[0]
        self.assertEqual(call["path"], "/v2/carriers")
        self.assertEqual(call["params"]["page_size"], 1)

    def test_missing_credentials_never_reach_the_network(self):
        no_v2 = self.instance.copy({"name": "No V2", "v2_api_key": False})
        with self.assertRaises(UserError):
            no_v2._ss_v2_request("GET", "/carriers")
        self.assertFalse(self.http.calls)

    # ---- error semantics -------------------------------------------------------
    def test_client_error_is_definitive_and_not_ambiguous(self):
        self.http.add("POST", "/orders/createorder", {"Message": "bad order"}, status=400)
        with self.assertRaises(ShipStationAPIError) as ctx:
            self.instance._ss_request("POST", "/orders/createorder", data={"x": 1})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(ctx.exception.ambiguous)
        self.assertIn("bad order", str(ctx.exception))

    def test_server_error_on_post_is_ambiguous_and_never_retried(self):
        self.http.add("POST", "/orders/createlabelfororder", {"Message": "boom"}, status=500)
        with self.assertRaises(ShipStationAPIError) as ctx:
            self.instance._ss_request("POST", "/orders/createlabelfororder", data={"x": 1})
        self.assertTrue(ctx.exception.ambiguous)
        self.assertEqual(len(self.http.calls), 1, "a POST must never be retried")

    def test_timeout_on_post_is_ambiguous_and_never_retried(self):
        self.http.add("POST", "/orders/createorder", exc=requests.Timeout("slow"))
        with self.assertRaises(ShipStationAPIError) as ctx:
            self.instance._ss_request("POST", "/orders/createorder", data={})
        self.assertTrue(ctx.exception.ambiguous)
        self.assertEqual(len(self.http.calls), 1)

    def test_get_is_retried_on_transient_errors_then_succeeds(self):
        responses = iter([503, 502, 200])

        def flaky(record):
            from .common import FakeResponse
            code = next(responses)
            return FakeResponse([{"storeId": 1}] if code == 200 else {"Message": "later"}, code)

        self.http.routes = []
        self.http.add("GET", "/stores", callback=flaky)
        self.assertEqual(self.instance._ss_request("GET", "/stores"), [{"storeId": 1}])
        self.assertEqual(len(self.http.calls), 3)

    def test_rate_limit_waits_for_server_provided_reset_capped(self):
        self.http.add("GET", "/orders", {"message": "Too Many Requests"}, status=429,
                      headers={"X-Rate-Limit-Reset": "7"})
        with self.assertRaises(ShipStationAPIError) as ctx:
            self.instance._ss_request("GET", "/orders")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertFalse(ctx.exception.ambiguous, "a 429 means the request was NOT processed")
        waits = [c.args[0] for c in self.sleep.sleep.call_args_list]
        self.assertTrue(waits and all(w == 7.0 for w in waits), waits)
        self.assertIn("retry in about", str(ctx.exception))

    def test_total_sleep_per_call_is_bounded_below_odoos_worker_limit(self):
        """A server asking for a 60 s pause three times would exceed Odoo's 120 s request limit."""
        self.http.add("GET", "/orders", {"message": "Too Many Requests"}, status=429,
                      headers={"X-Rate-Limit-Reset": "99999"})
        with self.assertRaises(ShipStationAPIError) as ctx:
            self.instance._ss_request("GET", "/orders")
        waits = [c.args[0] for c in self.sleep.sleep.call_args_list]
        self.assertLessEqual(sum(waits), 45.0, waits)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(len(self.http.calls), 1, "no point retrying when the pause alone exceeds the budget")

    def test_repeated_short_rate_limits_still_retry_within_the_budget(self):
        self.http.add("GET", "/orders", {"message": "Too Many Requests"}, status=429,
                      headers={"X-Rate-Limit-Reset": "20"})
        with self.assertRaises(ShipStationAPIError):
            self.instance._ss_request("GET", "/orders")
        waits = [c.args[0] for c in self.sleep.sleep.call_args_list]
        self.assertEqual(waits, [20.0, 20.0], "two 20 s pauses fit in 45 s, a third would not")
        self.assertEqual(len(self.http.calls), 3)

    def test_unreadable_success_body_is_ambiguous(self):
        from .common import FakeResponse
        self.http.add("POST", "/orders/createorder", callback=lambda r: FakeResponse(text="<html>oops"))
        with self.assertRaises(ShipStationAPIError) as ctx:
            self.instance._ss_request("POST", "/orders/createorder", data={})
        self.assertTrue(ctx.exception.ambiguous)

    # ---- logging ---------------------------------------------------------------
    def test_failures_and_successes_are_logged_without_credentials(self):
        self.http.add("GET", "/orders", {"orders": []})
        self.http.add("GET", "/shipments", {"Message": "denied"}, status=403)
        self.instance._ss_request("GET", "/orders", params={"page": 1})
        self.attempt(lambda: self.instance._ss_request("GET", "/shipments"), UserError)
        logs = self.env["shipstation.sync.log"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(sorted(logs.mapped("status")), ["failed", "success"])
        failed = logs.filtered(lambda r: r.status == "failed")
        self.assertIn("403", failed.error_message)
        blob = " ".join(str(v) for r in logs for v in (r.request_payload, r.response_payload, r.error_message))
        for secret in ("TEST-KEY-A", "TEST-SECRET-A", "TEST-V2-A"):
            self.assertNotIn(secret, blob)

    def test_v2_failures_are_logged_too(self):
        self.http.add("GET", "/v2/carriers", {"errors": [{"message": "bad key"}]}, status=401)
        self.attempt(lambda: self.instance._ss_v2_request("GET", "/carriers"), UserError)
        log = self.env["shipstation.sync.log"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(log.status, "failed")
        self.assertIn("bad key", log.error_message)

    # ---- URL safety --------------------------------------------------------------
    def test_base_url_must_be_https_shipstation(self):
        for good in ("https://api.shipstation.com", "https://api.shipstation.com/", "https://eu.api.shipstation.com"):
            self.instance.base_url = good
        for bad in ("http://api.shipstation.com", "https://evil.example.com",
                    "https://api.shipstation.com.evil.test", "https://api.shipstation.com@evil.test",
                    "https://shipstation.com.evil.test", "ftp://api.shipstation.com"):
            with self.subTest(url=bad), self.assertRaises(ValidationError):
                self.instance.base_url = bad

    def test_custom_base_url_requires_explicit_developer_switch(self):
        self.env["ir.config_parameter"].sudo().set_str("shipstation_connector.allow_custom_base_url", "1")
        self.instance.base_url = "http://127.0.0.1:9999"
        self.assertEqual(self.instance.base_url, "http://127.0.0.1:9999")

    # ---- Pacific time ----------------------------------------------------------------
    def test_utc_to_pacific_in_standard_and_daylight_time(self):
        fmt = self.instance._format_ss_datetime
        self.assertEqual(fmt(datetime(2026, 11, 5, 17, 30, 0)), "2026-11-05T09:30:00")   # PST, UTC-8
        self.assertEqual(fmt(datetime(2026, 7, 1, 17, 30, 0)), "2026-07-01T10:30:00")    # PDT, UTC-7
        self.assertEqual(fmt(datetime(2026, 11, 5, 17, 30, 0), query=True), "2026-11-05 09:30:00")
        self.assertFalse(fmt(False))

    def test_pacific_to_utc_parsing_matches_documented_formats(self):
        parse = self.instance._parse_ss_datetime
        self.assertEqual(parse("2026-11-05T09:30:00.0000000"), datetime(2026, 11, 5, 17, 30, 0))
        self.assertEqual(parse("2026-07-01 10:30:00"), datetime(2026, 7, 1, 17, 30, 0))
        self.assertEqual(parse("2026-11-05"), datetime(2026, 11, 5, 8, 0, 0))   # date only = midnight Pacific
        self.assertEqual(parse("2026-11-05T17:30:00Z"), datetime(2026, 11, 5, 17, 30, 0))  # explicit UTC kept
        self.assertFalse(parse("not a date"))
        self.assertFalse(parse(None))

    def test_round_trip_for_unambiguous_instants_around_dst_changes(self):
        for naive in (datetime(2026, 3, 8, 9, 30, 0), datetime(2026, 3, 8, 10, 30, 0),   # spring forward
                      datetime(2026, 11, 1, 7, 30, 0), datetime(2026, 11, 1, 10, 30, 0)):  # fall back
            self.assertFalse(self.instance._pacific_time_is_ambiguous(naive))
            formatted = self.instance._format_ss_datetime(naive)
            self.assertEqual(self.instance._parse_ss_datetime(formatted), naive)
        # a wall-clock time that does not exist (02:30 on spring-forward day) must not crash
        self.assertTrue(self.instance._parse_ss_datetime("2026-03-08T02:30:00"))

    def test_repeated_dst_hour_widens_the_sync_window(self):
        """When DST ends, 01:30 happens twice and ShipStation's wall-clock label is ambiguous."""
        first, second = datetime(2026, 11, 1, 8, 30, 0), datetime(2026, 11, 1, 9, 30, 0)   # 01:30 PDT / PST
        self.assertEqual(self.instance._format_ss_datetime(first), self.instance._format_ss_datetime(second))
        for ambiguous in (first, second):
            self.assertTrue(self.instance._pacific_time_is_ambiguous(ambiguous))
            start, _end = self.instance._get_sync_window(ambiguous)
            self.assertFalse(self.instance._pacific_time_is_ambiguous(start))
            self.assertLessEqual(start, first - timedelta(hours=1), "must start before the repeated hour")
        quiet = datetime(2026, 11, 1, 12, 0, 0)
        self.assertEqual(self.instance._get_sync_window(quiet)[0], quiet)

    def test_label_ship_date_is_a_pacific_calendar_date(self):
        # 03:00 UTC on Nov 6 is still Nov 5 in Los Angeles
        self.assertEqual(self.instance._format_ss_date(datetime(2026, 11, 6, 3, 0, 0)), "2026-11-05")
