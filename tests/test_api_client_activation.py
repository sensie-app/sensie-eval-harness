"""
test_api_client_activation.py — Unit tests for the Sensie live-API activation
client methods (post_consent, request_activation_code, get_activation) and the
four new exceptions (SensieConsentRequiredError, SensieRateLimitedError,
SensieActivationNotFoundError, SensieActivationGoneError).

All network calls are mocked — no test here touches the network. Patterned
after tests/test_api_client.py (stdlib urllib + unittest.mock).

Contract references (from COMMON.md v1, clarifications C1-C10):
  - C1: error envelope  {"status":"error","error":"<code>","message":"..."}
  - C2: 401 unauthorized for bad x-api-key / x-app-secret
  - C4: 8-char code, CSPRNG-generated, case-insensitive (server uppercases)
  - C5: lapsed code -> 200 + status:"expired" (NOT an HTTP error)
  - C8: accepted must be literal true; scope must equal "live-gesture"
  - C9: >3 outstanding codes -> 429 error="rate_limited" + Retry-After.
        Existing quota 429 uses error="quota_exceeded"; clients switch on
        the body `error` field, not the status alone.
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
    SensieActivationGoneError,
    SensieActivationNotFoundError,
    SensieAuthError,
    SensieConsentRequiredError,
    SensieQuotaError,
    SensieRateLimitedError,
)

BASE = "https://example.test/functions/v1"
KEY = "sk_sensie_" + "a" * 64
CONSENT_PATH = "/sdk-api/trial/consent"
CODE_PATH = "/sdk-api/trial/activation-code"
ACTIVATION_PATH_PREFIX = "/sdk-api/activation/"


def fake_response(payload, status=200):
    """Build a context-manager response like urlopen returns."""
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    return resp


def http_error(url, code, payload, headers=None):
    """Build a urllib.error.HTTPError matching the contract envelope."""
    import email.message
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return HTTPError(url, code, "error", hdrs,
                     io.BytesIO(json.dumps(payload).encode("utf-8")))


class TestPostConsent(unittest.TestCase):
    """post_consent(consent_version) -> data.consent {id, consented_at}."""

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_post_consent_request_shape(self, urlopen):
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"consent": {"id": "c-abc", "consented_at": "2026-09-21T14:00:00Z"}},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        consent = client.post_consent("2026-09-21")

        # Returns the consent dict (not the envelope).
        self.assertEqual(consent["id"], "c-abc")
        self.assertEqual(consent["consented_at"], "2026-09-21T14:00:00Z")

        # Request shape: POST {base}/sdk-api/trial/consent with the right body.
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}{CONSENT_PATH}")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), KEY)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body, {
            "consent_version": "2026-09-21",
            "scope": "live-gesture",
            "accepted": True,
        })

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_post_consent_hardcoded_scope_and_accepted(self, urlopen):
        """C8 — accepted must be literal true; scope must be 'live-gesture'."""
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"consent": {"id": "c-1", "consented_at": "x"}},
        })
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        client.post_consent("v1")
        body = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertIs(body["accepted"], True)
        self.assertEqual(body["scope"], "live-gesture")
        self.assertEqual(body["consent_version"], "v1")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_post_consent_no_raw_imu_keys(self, urlopen):
        """Trial contract — no raw IMU arrays ever leave the device."""
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"consent": {"id": "c-2", "consented_at": "x"}},
        })
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        client.post_consent("v1")
        body = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
        forbidden = {"accelerometer", "gyroscope", "imu", "samples", "raw"}
        self.assertEqual(forbidden & set(body.keys()), set())

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_post_consent_invalid_payload_400(self, urlopen):
        """C8 — invalid payload on consent -> generic SensieApiError (400)."""
        urlopen.side_effect = http_error(
            f"{BASE}{CONSENT_PATH}", 400,
            {"status": "error", "error": "invalid_payload",
             "message": "accepted must be true"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieApiError) as ctx:
            client.post_consent("v1")
        self.assertEqual(ctx.exception.status, 400)
        # Not one of the activation-specific exceptions.
        self.assertNotIsInstance(ctx.exception, SensieConsentRequiredError)


class TestRequestActivationCode(unittest.TestCase):
    """request_activation_code(consent_id) -> data.activation {code, expires_at, install_url}."""

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_request_activation_code_request_shape(self, urlopen):
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"activation": {
                "code": "ABC234XY",
                "expires_at": "2026-09-21T14:30:00Z",
                "install_url": "https://go.somacheck.com/install",
            }},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        activation = client.request_activation_code("c-abc")

        self.assertEqual(activation["code"], "ABC234XY")
        self.assertEqual(activation["expires_at"], "2026-09-21T14:30:00Z")
        # C10 — install_url is the stable go.somacheck.com/install link.
        self.assertEqual(activation["install_url"], "https://go.somacheck.com/install")

        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}{CODE_PATH}")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), KEY)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body, {"consent_id": "c-abc"})

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_request_activation_code_consent_required_403(self, urlopen):
        """C8 — bad/missing consent_id -> 403 consent_required."""
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 403,
            {"status": "error", "error": "consent_required",
             "message": "consent id belongs to a different key"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieConsentRequiredError) as ctx:
            client.request_activation_code("c-other")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.body["error"], "consent_required")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_request_activation_code_other_403_falls_through(self, urlopen):
        """Non-consent-required 403 is NOT a SensieConsentRequiredError."""
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 403,
            {"status": "error", "error": "forbidden",
             "message": "some other reason"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieApiError) as ctx:
            client.request_activation_code("c-1")
        self.assertEqual(ctx.exception.status, 403)
        self.assertNotIsInstance(ctx.exception, SensieConsentRequiredError)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_request_activation_code_auth_401(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 401,
            {"status": "error", "error": "unauthorized",
             "message": "invalid x-api-key"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieAuthError) as ctx:
            client.request_activation_code("c-1")
        self.assertEqual(ctx.exception.status, 401)


class TestRateLimitedVsQuotaExceeded(unittest.TestCase):
    """C9 — 429 has TWO distinct bodies: rate_limited vs quota_exceeded.
    Clients must switch on the body `error` field, not the status alone."""

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_429_rate_limited_raises_SensieRateLimitedError(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 429,
            {"status": "error", "error": "rate_limited",
             "message": "too many outstanding activation codes"},
            headers={"Retry-After": "60"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieRateLimitedError) as ctx:
            client.request_activation_code("c-1")
        exc = ctx.exception
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.body["error"], "rate_limited")
        # SensieRateLimitedError exposes Retry-After via the `retry_after` prop.
        self.assertEqual(exc.retry_after, "60")
        # Distinct from the quota exception (subclass relationship must NOT
        # collapse them — a caller catching SensieQuotaError must NOT
        # accidentally catch the activation rate-limit signal).
        self.assertNotIsInstance(exc, SensieQuotaError)
        self.assertIsInstance(exc, SensieApiError)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_429_quota_exceeded_still_SensieQuotaError(self, urlopen):
        """Existing trial-tier quota 429 — not a code of the activation path."""
        urlopen.side_effect = http_error(
            f"{BASE}/sdk-api/session", 429,
            {"error": "quota_exceeded", "used": 100, "limit": 100,
             "window_reset_at": "2026-09-22T00:00:00Z"},
            headers={"Retry-After": "3600"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieQuotaError) as ctx:
            client.create_session("u", "0.1.0")
        exc = ctx.exception
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.used, 100)
        self.assertEqual(exc.limit, 100)
        self.assertEqual(exc.window_reset_at, "2026-09-22T00:00:00Z")
        self.assertEqual(exc.retry_after, "3600")
        # And it is NOT a SensieRateLimitedError.
        self.assertNotIsInstance(exc, SensieRateLimitedError)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_429_unknown_error_field_treated_as_quota(self, urlopen):
        """If the 429 body has no `error` key, fall through to SensieQuotaError
        (preserve existing behavior — don't accidentally invent a 3rd exception)."""
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 429,
            {"status": "error"},  # no `error` field
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieQuotaError):
            client.request_activation_code("c-1")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_rate_limited_retry_after_missing(self, urlopen):
        """SensieRateLimitedError.retry_after is None when the header is absent."""
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 429,
            {"error": "rate_limited"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieRateLimitedError) as ctx:
            client.request_activation_code("c-1")
        self.assertIsNone(ctx.exception.retry_after)


class TestGetActivation(unittest.TestCase):
    """get_activation(code) -> data.activation {status, sensie|None, expires_at}.
    A lapsed code is 200 + status:'expired' (not an exception)."""

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_request_shape(self, urlopen):
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"activation": {
                "status": "pending",
                "sensie": None,
                "expires_at": "2026-09-21T14:30:00Z",
            }},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        activation = client.get_activation("abc234xy")

        self.assertEqual(activation["status"], "pending")
        self.assertIsNone(activation["sensie"])
        self.assertEqual(activation["expires_at"], "2026-09-21T14:30:00Z")

        request = urlopen.call_args[0][0]
        # Client uppercases + URL-quotes the code (C4 — server uppercases too).
        self.assertEqual(request.full_url, f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-api-key"), KEY)
        self.assertIsNone(request.data)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_case_insensitive(self, urlopen):
        """C4 — input is case-insensitive; the client uppercases locally."""
        urlopen.return_value = fake_response({
            "data": {"activation": {"status": "claimed", "sensie": None,
                                     "expires_at": "x"}},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        client.get_activation("abc234xy")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_url_encodes_special_chars(self, urlopen):
        """Even if a caller passes whitespace, the client trims + uppercases."""
        urlopen.return_value = fake_response({
            "data": {"activation": {"status": "pending", "sensie": None,
                                     "expires_at": "x"}},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        client.get_activation("  abc234xy\n")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_200_expired_not_an_exception(self, urlopen):
        """C5 — lapsed code is HTTP 200 with status:'expired', NOT an error.
        The client must return the activation dict, not raise."""
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"activation": {
                "status": "expired",
                "sensie": None,
                "expires_at": "2026-09-21T14:00:00Z",
            }},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        activation = client.get_activation("ABC234XY")
        self.assertEqual(activation["status"], "expired")
        self.assertIsNone(activation["sensie"])
        # Defensive: the client's `_request` does treat HTTP 200 with
        # body.status=="error" as an error, but "success" must pass through.
        self.assertNotIsInstance(activation, Exception)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_200_completed_includes_sensie(self, urlopen):
        urlopen.return_value = fake_response({
            "status": "success",
            "data": {"activation": {
                "status": "completed",
                "sensie": {"whips": 3, "flowing": 1, "agreement": 2},
                "expires_at": "2026-09-21T14:30:00Z",
            }},
        }, status=200)
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        activation = client.get_activation("ABC234XY")
        self.assertEqual(activation["status"], "completed")
        self.assertEqual(activation["sensie"]["whips"], 3)
        self.assertEqual(activation["sensie"]["flowing"], 1)
        self.assertEqual(activation["sensie"]["agreement"], 2)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_404_not_found(self, urlopen):
        """C3 — unknown code or cross-key code -> 404 code_not_found."""
        urlopen.side_effect = http_error(
            f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY", 404,
            {"status": "error", "error": "code_not_found",
             "message": "no such code"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieActivationNotFoundError) as ctx:
            client.get_activation("ABC234XY")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.body["error"], "code_not_found")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_410_code_expired(self, urlopen):
        """C6/C7 — claim/complete on expired code -> 410 code_expired."""
        urlopen.side_effect = http_error(
            f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY", 410,
            {"status": "error", "error": "code_expired",
             "message": "TTL elapsed"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieActivationGoneError) as ctx:
            client.get_activation("ABC234XY")
        exc = ctx.exception
        self.assertEqual(exc.status, 410)
        # .reason mirrors body.error so callers can branch.
        self.assertEqual(exc.reason, "code_expired")
        # SensieActivationGoneError is a SensieApiError (not a quota/auth).
        self.assertIsInstance(exc, SensieApiError)
        self.assertNotIsInstance(exc, SensieQuotaError)
        self.assertNotIsInstance(exc, SensieAuthError)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_410_code_already_claimed(self, urlopen):
        """C6 — already claimed/completed -> 410 code_already_claimed."""
        urlopen.side_effect = http_error(
            f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY", 410,
            {"status": "error", "error": "code_already_claimed",
             "message": "already used"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieActivationGoneError) as ctx:
            client.get_activation("ABC234XY")
        self.assertEqual(ctx.exception.reason, "code_already_claimed")

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_get_activation_410_unusual_reason_still_none_safe(self, urlopen):
        """`.reason` returns whatever the body `error` is (None if missing)."""
        urlopen.side_effect = http_error(
            f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY", 410,
            {"status": "error"},  # no `error` field
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieActivationGoneError) as ctx:
            client.get_activation("ABC234XY")
        self.assertIsNone(ctx.exception.reason)


class TestExceptionHierarchy(unittest.TestCase):
    """All four new exceptions must subclass SensieApiError and remain
    distinct from each other (no cross-catch leaks)."""

    def test_all_new_exceptions_subclass_SensieApiError(self):
        for cls in (
            SensieConsentRequiredError,
            SensieRateLimitedError,
            SensieActivationNotFoundError,
            SensieActivationGoneError,
        ):
            self.assertTrue(issubclass(cls, SensieApiError),
                            f"{cls.__name__} must subclass SensieApiError")

    def test_rate_limited_not_subclass_of_quota(self):
        """A caller that catches SensieQuotaError must NOT accidentally catch
        the activation rate-limit (and vice versa). The classes are siblings
        under SensieApiError, not a chain."""
        self.assertFalse(
            issubclass(SensieRateLimitedError, SensieQuotaError),
            "SensieRateLimitedError must NOT be a SensieQuotaError subclass",
        )
        self.assertFalse(
            issubclass(SensieQuotaError, SensieRateLimitedError),
            "SensieQuotaError must NOT be a SensieRateLimitedError subclass",
        )

    def test_gone_not_subclass_of_not_found(self):
        self.assertFalse(
            issubclass(SensieActivationGoneError, SensieActivationNotFoundError),
        )
        self.assertFalse(
            issubclass(SensieActivationNotFoundError, SensieActivationGoneError),
        )

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_status_body_headers_exposed_by_all(self, urlopen):
        """Every SensieApiError carries status/body/headers."""
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 403,
            {"error": "consent_required"},
            headers={"X-Trace": "abc"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieConsentRequiredError) as ctx:
            client.request_activation_code("c-1")
        exc = ctx.exception
        self.assertEqual(exc.status, 403)
        self.assertEqual(exc.body["error"], "consent_required")
        self.assertEqual(exc.headers.get("X-Trace"), "abc")


class TestErrorEnvelopeParsing(unittest.TestCase):
    """Defensive: malformed/non-JSON bodies still raise, never crash."""

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_404_non_json_body(self, urlopen):
        urlopen.side_effect = HTTPError(
            url=f"{BASE}{ACTIVATION_PATH_PREFIX}ABC234XY",
            code=404, msg="Not Found", hdrs=None,
            fp=io.BytesIO(b"<html>not json</html>"),
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieActivationNotFoundError) as ctx:
            client.get_activation("ABC234XY")
        self.assertEqual(ctx.exception.status, 404)

    @mock.patch("sensie_eval.api_client.urllib.request.urlopen")
    def test_500_unmapped_error(self, urlopen):
        urlopen.side_effect = http_error(
            f"{BASE}{CODE_PATH}", 500,
            {"status": "error", "error": "internal", "message": "boom"},
        )
        client = SensieApiClient(api_key=KEY, base_url=BASE)
        with self.assertRaises(SensieApiError) as ctx:
            client.request_activation_code("c-1")
        self.assertEqual(ctx.exception.status, 500)
        # Not one of the activation-specific exceptions.
        self.assertNotIsInstance(ctx.exception, SensieConsentRequiredError)
        self.assertNotIsInstance(ctx.exception, SensieRateLimitedError)
        self.assertNotIsInstance(ctx.exception, SensieActivationNotFoundError)
        self.assertNotIsInstance(ctx.exception, SensieActivationGoneError)


if __name__ == "__main__":
    unittest.main()
