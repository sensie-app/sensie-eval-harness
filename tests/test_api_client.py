"""
test_api_client.py — Unit tests for the Sensie live-API client.

All network calls are mocked — no test here touches the network.

Tests cover:
  1. Request shape (URL, method, headers, JSON body)
  2. No raw IMU arrays in any payload (trial contract)
  3. 429 quota_exceeded parsing (body schema + Retry-After header)
  4. 401 auth failure parsing
  5. Payload validation (flowing / agreement contract values)
"""

import io
import json
import os
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sensie_eval.api_client import (
    SensieApiClient,
    SensieApiError,
    SensieAuthError,
    SensieQuotaError,
)

BASE = "https://example.test/functions/v1"
KEY = "sk_sensie_" + "a" * 64


def fake_response(payload):
    """Build a context-manager response like urlopen returns."""
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def http_error(url, code, payload, headers=None):
    import email.message
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return HTTPError(url, code, "error", hdrs,
                     io.BytesIO(json.dumps(payload).encode("utf-8")))


class TestRequestShape(unittest.TestCase):

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_create_session_request(self, urlopen):
        urlopen.return_value = fake_response({
            "status": "success",
            "message": "Session created.",
            "data": {"session": {"id": 123}},
        })
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        session = client.create_session("eval-abc", sdk_version="0.1.0")

        self.assertEqual(session["id"], 123)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}/sdk-api/session")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), KEY)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body, {
            "userId": "eval-abc",
            "type": "evaluation",
            "sdkVersion": "0.1.0",
        })

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_post_sensie_request(self, urlopen):
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"sensie": {"id": 7, "whips": 2}},
        })
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        sensie = client.post_sensie(123, whips=2, flowing=1, agreement=2)

        self.assertEqual(sensie["id"], 7)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}/sdk-api/session/123/sensie")
        body = json.loads(request.data.decode("utf-8"))
        # Exactly the three scalar fields — never raw IMU arrays.
        self.assertEqual(set(body.keys()), {"whips", "flowing", "agreement"})
        self.assertEqual(body["whips"], 2)
        self.assertEqual(body["flowing"], 1)
        self.assertEqual(body["agreement"], 2)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_list_sensies_request(self, urlopen):
        urlopen.return_value = fake_response({
            "data": {"sensies": [{"id": 1}, {"id": 2}]},
        })
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        sensies = client.list_sensies(123)

        self.assertEqual(len(sensies), 2)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}/sdk-api/session/123/sensie")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)


class TestPayloadValidation(unittest.TestCase):

    def test_invalid_flowing_rejected(self):
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(ValueError):
            client.post_sensie(1, whips=1, flowing=0, agreement=1)

    def test_invalid_agreement_rejected(self):
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(ValueError):
            client.post_sensie(1, whips=1, flowing=1, agreement=0)


class TestErrorHandling(unittest.TestCase):

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_quota_exceeded_429(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}/sdk-api/session/1/sensie", 429,
            {
                "error": "quota_exceeded",
                "used": 100,
                "limit": 100,
                "window_reset_at": "2026-07-09T14:00:00Z",
            },
            headers={"Retry-After": "3600"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieQuotaError) as ctx:
            client.post_sensie(1, whips=1, flowing=1, agreement=1)

        exc = ctx.exception
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.used, 100)
        self.assertEqual(exc.limit, 100)
        self.assertEqual(exc.window_reset_at, "2026-07-09T14:00:00Z")
        self.assertEqual(exc.retry_after, "3600")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_quota_null_reset(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}/sdk-api/session", 429,
            {"error": "quota_exceeded", "used": 100, "limit": 100,
             "window_reset_at": None},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieQuotaError) as ctx:
            client.create_session("u", "0.1.0")
        self.assertIsNone(ctx.exception.window_reset_at)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_auth_failure_401(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}/sdk-api/session", 401,
            {"status": "fail", "message": "Invalid API key."},
        )
        client = SensieApiClient(api_key="sk_sensie_bad", base_url=BASE)
        with self.assertRaises(SensieAuthError) as ctx:
            client.create_session("u", "0.1.0")
        self.assertEqual(ctx.exception.status, 401)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_other_http_error(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}/sdk-api/session", 500, {"status": "error"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieApiError) as ctx:
            client.create_session("u", "0.1.0")
        self.assertEqual(ctx.exception.status, 500)
        self.assertNotIsInstance(ctx.exception, SensieQuotaError)
        self.assertNotIsInstance(ctx.exception, SensieAuthError)


if __name__ == "__main__":
    unittest.main()


class TestFailEnvelope(unittest.TestCase):
    """The API reports validation failures as HTTP 200 + status:'fail'."""

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_fail_envelope_raises(self, urlopen):
        resp = fake_response({
            "status": "fail",
            "message": "Invalid typeof for: whips",
            "data": [],
        })
        resp.status = 200
        urlopen.return_value = resp
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieApiError) as ctx:
            client.post_sensie(1, whips=1, flowing=1, agreement=1)
        self.assertNotIsInstance(ctx.exception, SensieQuotaError)
        self.assertIn("Invalid typeof", ctx.exception.body.get("message", ""))
