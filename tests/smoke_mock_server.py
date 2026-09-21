"""
smoke_mock_server.py — Exercise every endpoint of mock_activation_server.py
and assert the responses match contract v1 + clarifications C1..C10.

Runs the mock in a subprocess (so the test really hits a TCP socket, not
an in-process import), then issues curl-equivalent urllib calls against it
and prints each step's request + response. Exits non-zero on any mismatch.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

# Import the module under test by file path so we don't need a `tests/`
# package or a conftest. We don't actually call anything in it from this
# script (the mock runs in a subprocess), but keep the import as a smoke
# check that the module is importable in isolation.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "mock_activation_server",
    os.path.join(THIS_DIR, "fixtures", "mock_activation_server.py"),
)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
mock = _mod  # noqa: F841

KEY = "sk_sensie_" + "a" * 64
OTHER_KEY = "sk_sensie_" + "b" * 64
APP_SECRET = "mock-app-secret"


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.settimeout(0.5)
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.05)
    raise RuntimeError(f"mock did not open {host}:{port} in {timeout}s")


def _hit(method: str, url: str, headers: Optional[Dict[str, str]] = None,
         body: Optional[Dict[str, Any]] = None
         ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
    req_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, dict(resp.headers.items()), json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        return exc.code, dict(exc.headers.items()), json.loads(raw)


def _step(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line)
    if not ok:
        global _failed
        _failed += 1


_failed = 0


def expect_envelope_ok(name: str, status: int, body: Dict[str, Any],
                       expect_status: int, expect_in_data: list) -> None:
    ok = (
        status == expect_status
        and body.get("status") == "success"
        and isinstance(body.get("data"), dict)
        and all(k in body["data"] for k in expect_in_data)
    )
    detail = f"HTTP {status}, body={json.dumps(body)}"
    if isinstance(body.get("data"), dict):
        # Trim very long strings for readability in failure output.
        for k, v in body["data"].items():
            if isinstance(v, str) and len(v) > 80:
                body["data"][k] = v[:77] + "..."
    _step(name, ok, detail)


def expect_error(name: str, status: int, body: Dict[str, Any],
                 expect_status: int, expect_code: str) -> None:
    ok = (
        status == expect_status
        and body.get("status") == "error"
        and body.get("error") == expect_code
        and isinstance(body.get("message"), str)
        and body["message"]
    )
    _step(name, ok, f"HTTP {status}, error={body.get('error')}")


def main() -> int:
    # Pick a free port, launch the mock as a subprocess so the smoke is a
    # real network round-trip (matches what curl would do).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    env = os.environ.copy()
    env["MOCK_ACTIVATION_LOG"] = "1"  # surface stderr access log
    env["MOCK_ACTIVATION_ALLOW_TEST_HOOKS"] = "1"  # allow /__mock/advance
    proc = subprocess.Popen(
        [sys.executable,
         os.path.join(THIS_DIR, "fixtures", "mock_activation_server.py"),
         "--port", str(port),
         "--ttl-seconds", "1800",
         "--app-secret", APP_SECRET],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = proc.stdout.readline()
        if "mock activation server listening on" not in ready_line:
            print(f"mock did not announce readiness: {ready_line!r}")
            return 2
        print(f"# {ready_line.strip()}")
        _wait_for_port("127.0.0.1", port, timeout=5.0)
        base = f"http://127.0.0.1:{port}"
        h = {"x-api-key": KEY}
        ha = {"x-app-secret": APP_SECRET}

        # --- happy path --------------------------------------------------

        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1",
            "scope": "live-gesture",
            "accepted": True,
        })
        expect_envelope_ok("consent created", status, body, 201, ["consent"])
        consent_id = body["data"]["consent"]["id"]
        # C10: install_url is stable.
        assert body["data"]["consent"].get("consented_at"), "missing consented_at"

        status, _, body = _hit("POST", f"{base}/sdk-api/trial/activation-code",
                               h, {"consent_id": consent_id})
        expect_envelope_ok("activation code issued", status, body, 201,
                           ["activation"])
        activation = body["data"]["activation"]
        code = activation["code"]
        assert len(code) == 8 and code.isalnum() and code.isupper(), \
            f"bad code shape: {code!r}"
        assert activation["install_url"] == "https://go.somacheck.com/install", \
            f"bad install_url: {activation['install_url']!r}"

        # Regression: expires_at must be a real wall-clock UTC ISO-8601,
        # roughly now + TTL. The mock used to format time.monotonic() as if
        # it were a Unix epoch, returning dates around 1970-01-14; that
        # broke every CLI consumer that computes remaining time from it.
        # Mirrors the TTL passed to the mock on the subprocess CLI above
        # (tests/fixtures/mock_activation_server.py --ttl-seconds 1800).
        ttl_seconds = 1800
        expires_at_str = activation["expires_at"]
        try:
            expires_at_dt = _dt.datetime.strptime(
                expires_at_str, "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=_dt.timezone.utc)
        except ValueError as exc:
            raise AssertionError(
                f"expires_at is not ISO-8601 UTC: {expires_at_str!r} ({exc})"
            )
        now_wall = _dt.datetime.now(_dt.timezone.utc)
        expected_min = now_wall + _dt.timedelta(seconds=ttl_seconds - 5)
        expected_max = now_wall + _dt.timedelta(seconds=ttl_seconds + 5)
        ok = expected_min <= expires_at_dt <= expected_max
        _step(
            "expires_at parses as UTC and is now+ttl (±5s)",
            ok,
            (f"expires_at={expires_at_str}, "
             f"expected∈[{expected_min.isoformat()}, "
             f"{expected_max.isoformat()}]"),
        )

        status, _, body = _hit("GET", f"{base}/sdk-api/activation/{code}", h)
        expect_envelope_ok("GET pending", status, body, 200, ["activation"])
        assert body["data"]["activation"]["status"] == "pending", \
            f"expected pending, got {body['data']['activation']['status']}"
        assert body["data"]["activation"]["sensie"] is None, \
            "sensie must be null until completed (C5)"

        status, _, body = _hit("POST", f"{base}/sdk-api/activation/{code}/claim",
                               ha, {"device_id": "ios-sim-1"})
        # D4: claim 200 body shape is {status, data: {activation: {status: "claimed", expires_at}}}.
        expect_envelope_ok("claim succeeded", status, body, 200, ["activation"])
        activation_claim = body["data"]["activation"]
        assert activation_claim["status"] == "claimed", \
            f"expected status=claimed, got {activation_claim!r}"
        try:
            _dt.datetime.strptime(
                activation_claim["expires_at"], "%Y-%m-%dT%H:%M:%SZ",
            )
        except (KeyError, ValueError) as exc:
            raise AssertionError(
                f"claim activation.expires_at missing or not ISO-8601 UTC: "
                f"{activation_claim!r} ({exc})"
            )
        # The 'code' field is NOT part of the D4 success body — the contract
        # deliberately mirrors the GET shape so the same parser works on both.
        assert "code" not in activation_claim, \
            f"claim body must not leak the code: {activation_claim!r}"

        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{code}/complete", ha,
            {"whips": 3, "flowing": 1, "agreement": 2},
        )
        # D4: complete 200 body shape is {status, data: {activation: {status: "completed", completed_at}}}.
        expect_envelope_ok("complete succeeded", status, body, 200, ["activation"])
        activation_complete = body["data"]["activation"]
        assert activation_complete["status"] == "completed", \
            f"expected status=completed, got {activation_complete!r}"
        try:
            _dt.datetime.strptime(
                activation_complete["completed_at"], "%Y-%m-%dT%H:%M:%SZ",
            )
        except (KeyError, ValueError) as exc:
            raise AssertionError(
                f"complete activation.completed_at missing or not ISO-8601 UTC: "
                f"{activation_complete!r} ({exc})"
            )
        assert "expires_at" not in activation_complete, (
            "complete body uses completed_at, not expires_at: "
            f"{activation_complete!r}"
        )

        status, _, body = _hit("GET", f"{base}/sdk-api/activation/{code}", h)
        expect_envelope_ok("GET completed", status, body, 200, ["activation"])
        sensie = body["data"]["activation"]["sensie"]
        assert sensie == {"whips": 3, "flowing": 1, "agreement": 2}, \
            f"bad sensie shape: {sensie!r}"

        # --- D9: agreement is OPTIONAL on complete ----------------------
        # The SomaCheck app never sends agreement (it's optional feedback
        # collected AFTER the reveal and must never be invented). Both
        # "omitted" and "JSON null" must succeed and store null. When
        # agreement IS present and non-null it must still be in {-1,1,2};
        # agreement=0 is rejected with 400 invalid_payload.
        #
        # Use a fresh key so we don't collide with the rate-limit setup
        # later in this script (KEY already has 3 outstanding codes).
        d9_key = "sk_sensie_" + "9" * 64
        d9_h = {"x-api-key": d9_key}

        # Issue + claim a code on the fresh key.
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/consent", d9_h,
            {"consent_version": "v1", "scope": "live-gesture",
             "accepted": True},
        )
        d9_cid = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", d9_h,
            {"consent_id": d9_cid},
        )
        d9_code = body["data"]["activation"]["code"]
        _hit("POST", f"{base}/sdk-api/activation/{d9_code}/claim",
             ha, {"device_id": "d9-dev"})

        # D9 case A: agreement OMITTED entirely -> success, stored as null.
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{d9_code}/complete", ha,
            {"whips": 2, "flowing": 1},  # no agreement key
        )
        expect_envelope_ok(
            "D9: complete with agreement omitted -> 200",
            status, body, 200, ["activation"],
        )
        # D4: success body still mirrors the GET shape; agreement is NOT
        # in the success envelope (it's only in the sensie sub-object of
        # GET). The success body must carry status + completed_at.
        activation_d9_omit = body["data"]["activation"]
        assert activation_d9_omit.get("status") == "completed", \
            f"D9 omitted: bad success shape: {activation_d9_omit!r}"
        try:
            _dt.datetime.strptime(
                activation_d9_omit["completed_at"], "%Y-%m-%dT%H:%M:%SZ",
            )
        except (KeyError, ValueError) as exc:
            raise AssertionError(
                "D9 omitted: complete.completed_at missing or not "
                f"ISO-8601 UTC: {activation_d9_omit!r} ({exc})"
            )
        assert "agreement" not in activation_d9_omit, (
            "D9 omitted: success body must not leak agreement: "
            f"{activation_d9_omit!r}"
        )

        # GET after the omitted-agreement complete -> sensie.agreement is null.
        status, _, body = _hit(
            "GET", f"{base}/sdk-api/activation/{d9_code}", d9_h,
        )
        expect_envelope_ok(
            "D9: GET after agreement-omitted complete -> 200",
            status, body, 200, ["activation"],
        )
        sensie_d9_omit = body["data"]["activation"]["sensie"]
        ok = (
            isinstance(sensie_d9_omit, dict)
            and sensie_d9_omit.get("whips") == 2
            and sensie_d9_omit.get("flowing") == 1
            and sensie_d9_omit.get("agreement", "__missing__") is None
        )
        _step(
            "D9: agreement omitted -> GET sensie.agreement is null",
            ok,
            f"sensie={sensie_d9_omit!r}",
        )

        # D9 case B: agreement EXPLICITLY null -> also succeeds, stored null.
        # Use a second code on the same fresh key (still under the 3-cap).
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/consent", d9_h,
            {"consent_version": "v1", "scope": "live-gesture",
             "accepted": True},
        )
        d9_cid2 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", d9_h,
            {"consent_id": d9_cid2},
        )
        d9_code2 = body["data"]["activation"]["code"]
        _hit("POST", f"{base}/sdk-api/activation/{d9_code2}/claim",
             ha, {"device_id": "d9-dev2"})
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{d9_code2}/complete", ha,
            {"whips": 4, "flowing": -1, "agreement": None},
        )
        expect_envelope_ok(
            "D9: complete with agreement=null -> 200",
            status, body, 200, ["activation"],
        )
        status, _, body = _hit(
            "GET", f"{base}/sdk-api/activation/{d9_code2}", d9_h,
        )
        sensie_d9_null = body["data"]["activation"]["sensie"]
        ok = (
            isinstance(sensie_d9_null, dict)
            and sensie_d9_null.get("whips") == 4
            and sensie_d9_null.get("flowing") == -1
            and sensie_d9_null.get("agreement", "__missing__") is None
        )
        _step(
            "D9: agreement=null -> GET sensie.agreement is null",
            ok,
            f"sensie={sensie_d9_null!r}",
        )

        # D9 case C: agreement PRESENT and non-null in {-1,1,2} -> stored
        # verbatim. Re-confirm the value-path still works (mirrors the
        # earlier "agreement: 2" happy-path test, but on a fresh code to
        # keep this section self-contained).
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/consent", d9_h,
            {"consent_version": "v1", "scope": "live-gesture",
             "accepted": True},
        )
        d9_cid3 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", d9_h,
            {"consent_id": d9_cid3},
        )
        d9_code3 = body["data"]["activation"]["code"]
        _hit("POST", f"{base}/sdk-api/activation/{d9_code3}/claim",
             ha, {"device_id": "d9-dev3"})
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{d9_code3}/complete", ha,
            {"whips": 1, "flowing": 1, "agreement": -1},
        )
        expect_envelope_ok(
            "D9: complete with agreement=-1 -> 200, stored verbatim",
            status, body, 200, ["activation"],
        )
        status, _, body = _hit(
            "GET", f"{base}/sdk-api/activation/{d9_code3}", d9_h,
        )
        sensie_d9_neg1 = body["data"]["activation"]["sensie"]
        ok = (
            isinstance(sensie_d9_neg1, dict)
            and sensie_d9_neg1.get("whips") == 1
            and sensie_d9_neg1.get("flowing") == 1
            and sensie_d9_neg1.get("agreement") == -1
        )
        _step(
            "D9: agreement=-1 -> GET sensie.agreement is -1",
            ok,
            f"sensie={sensie_d9_neg1!r}",
        )

        # D9 case D: agreement=0 still rejected. (The earlier "400 invalid_payload
        # (agreement=0)" assertion already covers the existing-code path; here
        # we cover the D7 ordering on an UNKNOWN code — a bad payload must
        # never become an oracle for which codes are real, so 0 on an unknown
        # code must also be 400.)
        status, _, body = _hit(
            "PATCH",
            f"{base}/sdk-api/activation/BBBBBBBB/complete", ha,
            {"whips": 0, "flowing": 1, "agreement": 0},
        )
        expect_error(
            "D9: 400 invalid_payload (agreement=0 on unknown code)",
            status, body, 400, "invalid_payload",
        )

        # --- error taxonomy: one observation per error code --------------

        # 401 unauthorized: bad x-api-key (wrong shape)
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent",
                               {"x-api-key": "sk_sensie_tooshort"}, {})
        expect_error("401 unauthorized (bad key shape)", status, body, 401,
                     "unauthorized")

        # 401 unauthorized: missing x-api-key
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", {},
                               {"accepted": True, "scope": "live-gesture",
                                "consent_version": "v1"})
        expect_error("401 unauthorized (missing key)", status, body, 401,
                     "unauthorized")

        # 401 unauthorized: bad x-app-secret
        status, _, body = _hit("POST", f"{base}/sdk-api/activation/{code}/claim",
                               {"x-app-secret": "wrong"}, {"device_id": "d"})
        expect_error("401 unauthorized (bad app secret)", status, body, 401,
                     "unauthorized")

        # 400 invalid_payload: consent accepted != true
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": False,
        })
        expect_error("400 invalid_payload (consent accepted=false)",
                     status, body, 400, "invalid_payload")

        # 400 invalid_payload: consent scope wrong
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "other", "accepted": True,
        })
        expect_error("400 invalid_payload (consent scope)",
                     status, body, 400, "invalid_payload")

        # --- D5: bounded consent_version (1-64 chars, no control chars) ---
        # Use a fresh key so we don't burn consents on the main one.
        d5_key = "sk_sensie_" + "e" * 64
        d5_h = {"x-api-key": d5_key}

        # 400 invalid_payload: consent_version empty
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", d5_h, {
            "consent_version": "", "scope": "live-gesture", "accepted": True,
        })
        expect_error("D5: 400 invalid_payload (consent_version empty)",
                     status, body, 400, "invalid_payload")

        # 400 invalid_payload: consent_version too long (65 chars)
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", d5_h, {
            "consent_version": "v" * 65, "scope": "live-gesture",
            "accepted": True,
        })
        expect_error("D5: 400 invalid_payload (consent_version 65 chars)",
                     status, body, 400, "invalid_payload")

        # 400 invalid_payload: consent_version contains a control character
        # (newline). Control characters (0x00-0x1F, 0x7F) are forbidden.
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", d5_h, {
            "consent_version": "v1\n", "scope": "live-gesture",
            "accepted": True,
        })
        expect_error("D5: 400 invalid_payload (consent_version control char)",
                     status, body, 400, "invalid_payload")

        # 403 consent_required: activation-code with unknown consent_id
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": "00000000-0000-0000-0000-000000000000"},
        )
        expect_error("403 consent_required (unknown consent)",
                     status, body, 403, "consent_required")

        # 403 consent_required: activation-code with consent_id from a
        # different key.
        h2 = {"x-api-key": OTHER_KEY}
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h2, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        other_consent = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": other_consent},
        )
        expect_error("403 consent_required (other key's consent)",
                     status, body, 403, "consent_required")

        # --- D3: split failure modes for consent_id ----------------------
        # The payload shape is bad -> 400 invalid_payload; the value is a
        # present-but-not-UUID string -> 403 consent_required (the string
        # cannot be an identifier, so it is functionally unknown; 400 would
        # disclose format vs existence).

        # 400 invalid_payload: consent_id missing entirely
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"scope": "live-gesture"},
        )
        expect_error("D3: 400 invalid_payload (consent_id missing)",
                     status, body, 400, "invalid_payload")

        # 400 invalid_payload: consent_id empty string
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": ""},
        )
        expect_error("D3: 400 invalid_payload (consent_id empty string)",
                     status, body, 400, "invalid_payload")

        # 400 invalid_payload: consent_id not a string (integer)
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": 12345},
        )
        expect_error("D3: 400 invalid_payload (consent_id non-string)",
                     status, body, 400, "invalid_payload")

        # 403 consent_required: consent_id present but not a well-formed UUID
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": "not-a-uuid"},
        )
        expect_error("D3: 403 consent_required (consent_id non-UUID)",
                     status, body, 403, "consent_required")

        # 404 code_not_found: claim unknown
        status, _, body = _hit("POST", f"{base}/sdk-api/activation/ZZZZZZZZ/claim",
                               ha, {"device_id": "d"})
        expect_error("404 code_not_found (claim unknown)", status, body, 404,
                     "code_not_found")

        # 404 code_not_found: GET unknown
        status, _, body = _hit("GET", f"{base}/sdk-api/activation/ZZZZZZZZ", h)
        expect_error("404 code_not_found (GET unknown)", status, body, 404,
                     "code_not_found")

        # 404 code_not_found: GET issued to a different key (C3)
        status, _, body = _hit("GET", f"{base}/sdk-api/activation/{code}", h2)
        expect_error("404 code_not_found (GET cross-key)", status, body, 404,
                     "code_not_found")

        # 404 code_not_found: PATCH complete on unknown
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/ZZZZZZZZ/complete", ha,
            {"whips": 0, "flowing": 1, "agreement": -1},
        )
        expect_error("404 code_not_found (complete unknown)", status, body,
                     404, "code_not_found")

        # --- D6: a malformed code path segment is 404 code_not_found ----
        # Spec: {code} must be exactly 8 chars of ABCDEFGHJKMNPQRSTUVWXYZ23456789
        # (case-insensitive). Anything else -> 404 code_not_found on claim,
        # complete, AND GET. Use codes that share length and alphabet
        # boundaries so the test would fail if the matcher only checks length.

        # D6: contains 'I' (excluded from Crockford alphabet)
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/ABCDEFIH/claim", ha,
            {"device_id": "d"},
        )
        expect_error("D6: 404 code_not_found (claim, 'I' in code)",
                     status, body, 404, "code_not_found")

        # D6: contains '0' (excluded from Crockford alphabet)
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/ABCD0FGH/complete", ha,
            {"whips": 0, "flowing": 1, "agreement": 1},
        )
        expect_error("D6: 404 code_not_found (complete, '0' in code)",
                     status, body, 404, "code_not_found")

        # D6: too short (7 chars)
        status, _, body = _hit(
            "GET", f"{base}/sdk-api/activation/ABCDEFG", h,
        )
        expect_error("D6: 404 code_not_found (GET, code 7 chars)",
                     status, body, 404, "code_not_found")

        # D6: too long (9 chars)
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/ABCDEFGHJ/claim", ha,
            {"device_id": "d"},
        )
        expect_error("D6: 404 code_not_found (claim, code 9 chars)",
                     status, body, 404, "code_not_found")

        # D6: contains special character
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/ABCD!FGH/complete", ha,
            {"whips": 0, "flowing": 1, "agreement": 1},
        )
        expect_error("D6: 404 code_not_found (complete, '!' in code)",
                     status, body, 404, "code_not_found")

        # 410 code_already_claimed: claim after claim
        # Issue + claim a fresh code first.
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid2 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": cid2},
        )
        code2 = body["data"]["activation"]["code"]
        _hit("POST", f"{base}/sdk-api/activation/{code2}/claim",
             ha, {"device_id": "d"})
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/{code2}/claim",
            ha, {"device_id": "d2"},
        )
        expect_error("410 code_already_claimed (double claim)", status, body,
                     410, "code_already_claimed")

        # 409 code_not_claimed: PATCH on a pending code
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid3 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": cid3},
        )
        code3 = body["data"]["activation"]["code"]
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{code3}/complete", ha,
            {"whips": 1, "flowing": -1, "agreement": 1},
        )
        expect_error("409 code_not_claimed (complete before claim)",
                     status, body, 409, "code_not_claimed")

        # 410 code_already_claimed: complete an already-completed code
        # (reuse code2 — claim succeeded, now complete it once, then again).
        _hit("PATCH", f"{base}/sdk-api/activation/{code2}/complete", ha,
             {"whips": 0, "flowing": 1, "agreement": -1})
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{code2}/complete", ha,
            {"whips": 0, "flowing": 1, "agreement": -1},
        )
        expect_error("410 code_already_claimed (double complete)",
                     status, body, 410, "code_already_claimed")

        # 400 invalid_payload: complete with bad value domains
        _hit("POST", f"{base}/sdk-api/activation/{code3}/claim",
             ha, {"device_id": "d"})
        for bad_payload, label in [
            ({"whips": -1, "flowing": 1, "agreement": 1}, "whips negative"),
            ({"whips": "x", "flowing": 1, "agreement": 1}, "whips non-int"),
            ({"whips": 1, "flowing": 0, "agreement": 1}, "flowing=0"),
            ({"whips": 1, "flowing": 2, "agreement": 1}, "flowing=2"),
            ({"whips": 1, "flowing": 1, "agreement": 0}, "agreement=0"),
            ({"whips": 1, "flowing": 1, "agreement": 3}, "agreement=3"),
        ]:
            status, _, body = _hit(
                "PATCH", f"{base}/sdk-api/activation/{code3}/complete", ha,
                bad_payload,
            )
            expect_error(f"400 invalid_payload ({label})", status, body, 400,
                         "invalid_payload")

        # --- D7: payload is validated BEFORE the code is looked up ------
        # A bad payload must return 400 invalid_payload whether the code
        # EXISTS or NOT — otherwise bad payloads become an oracle for
        # which codes are real. Test against an unknown code (the one
        # most likely to leak). Pick a valid Crockford-shape code so D6
        # does not intervene.
        for bad_payload, label in [
            ({"whips": -1, "flowing": 1, "agreement": 1},
             "whips negative (unknown code)"),
            ({"whips": 1, "flowing": 0, "agreement": 1},
             "flowing=0 (unknown code)"),
            ({"whips": 1, "flowing": 1, "agreement": 0},
             "agreement=0 (unknown code)"),
        ]:
            status, _, body = _hit(
                "PATCH",
                f"{base}/sdk-api/activation/AAAAAAAA/complete",
                ha,
                bad_payload,
            )
            expect_error(
                f"D7: 400 invalid_payload ({label})", status, body, 400,
                "invalid_payload",
            )

        # --- D5: bounded device_id (1-200 chars, no control chars) ------
        # Issue a fresh code so we can test device_id independently.
        d5d_key = "sk_sensie_" + "f" * 64
        d5d_h = {"x-api-key": d5d_key}
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent",
                               d5d_h, {
            "consent_version": "v1", "scope": "live-gesture",
            "accepted": True,
        })
        d5d_cid = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", d5d_h,
            {"consent_id": d5d_cid},
        )
        d5d_code = body["data"]["activation"]["code"]

        # D5: device_id empty
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/{d5d_code}/claim", ha,
            {"device_id": ""},
        )
        expect_error("D5: 400 invalid_payload (device_id empty)",
                     status, body, 400, "invalid_payload")

        # D5: device_id too long (201 chars)
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/{d5d_code}/claim", ha,
            {"device_id": "d" * 201},
        )
        expect_error("D5: 400 invalid_payload (device_id 201 chars)",
                     status, body, 400, "invalid_payload")

        # D5: device_id contains a control character (newline)
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/{d5d_code}/claim", ha,
            {"device_id": "ios\n"},
        )
        expect_error("D5: 400 invalid_payload (device_id control char)",
                     status, body, 400, "invalid_payload")

        # 429 rate_limited: exceed outstanding-code limit (3). We already
        # have code (completed) and code2 (completed) and code3 (claimed)
        # on KEY. Two more should succeed, then the 4th should 429.
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid4 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": cid4},
        )
        expect_envelope_ok("4th activation code issued (still under cap)",
                           status, body, 201, ["activation"])
        code4 = body["data"]["activation"]["code"]

        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid5 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": cid5},
        )
        expect_envelope_ok("5th activation code issued (at cap)",
                           status, body, 201, ["activation"])
        code5 = body["data"]["activation"]["code"]

        # 6th should be rate_limited. (Note: code is completed so it does
        # NOT count against the outstanding cap. code2 is completed, same.
        # code3 is claimed (outstanding), code4 pending, code5 pending.
        # So outstanding count = 3; one more is over the cap.)
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid6 = body["data"]["consent"]["id"]
        status, hdrs, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", h,
            {"consent_id": cid6},
        )
        expect_error("429 rate_limited (over cap)", status, body, 429,
                     "rate_limited")
        # C9: Retry-After header is present.
        ok = "Retry-After" in hdrs and hdrs["Retry-After"] == "60"
        _step("Retry-After header present (C9)", ok,
              f"Retry-After={hdrs.get('Retry-After')}")

        # C5: expired path returns 200 with status:"expired". Use a fresh
        # key so we don't fight the outstanding-code cap from earlier tests.
        exp_key = "sk_sensie_" + "c" * 64
        exp_h = {"x-api-key": exp_key}
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", exp_h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid7 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", exp_h,
            {"consent_id": cid7},
        )
        code7 = body["data"]["activation"]["code"]
        # Advance the mock clock by 31 minutes via the test-only endpoint.
        # Note: the mock refuses this unless MOCK_ACTIVATION_ALLOW_TEST_HOOKS=1.
        status, _, body = _hit(
            "POST", f"{base}/__mock/advance?seconds={31*60}",
        )
        assert status == 200 and body["data"]["advanced"] == 31 * 60, body

        status, _, body = _hit("GET", f"{base}/sdk-api/activation/{code7}", exp_h)
        expect_envelope_ok("GET lapsed code -> status:expired (200)",
                           status, body, 200, ["activation"])
        ok = body["data"]["activation"]["status"] == "expired"
        _step("expired status surfaces on GET (C5)", ok,
              f"status={body['data']['activation']['status']}")
        # Regression pair: after /__mock/advance the GET must reflect
        # status:expired AND the expires_at field the GET returns must
        # still be a real wall-clock UTC ISO-8601 (just in the past now).
        post_advance_status = body["data"]["activation"]["status"]
        post_advance_expires_at = body["data"]["activation"]["expires_at"]
        ok_status = post_advance_status == "expired"
        ok_iso = False
        try:
            _dt.datetime.strptime(
                post_advance_expires_at, "%Y-%m-%dT%H:%M:%SZ",
            )
            ok_iso = True
        except ValueError:
            ok_iso = False
        _step(
            "after /__mock/advance, GET reflects status:expired",
            ok_status,
            f"status={post_advance_status}, expires_at={post_advance_expires_at}",
        )
        _step(
            "after /__mock/advance, GET expires_at is still ISO-8601 UTC",
            ok_iso,
            f"expires_at={post_advance_expires_at}",
        )

        # Claim on a now-expired code -> 410 code_expired.
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/activation/{code7}/claim", ha,
            {"device_id": "d"},
        )
        expect_error("410 code_expired (claim after expiry)", status, body,
                     410, "code_expired")

        # Complete on the expired code -> 410 code_expired.
        status, _, body = _hit(
            "PATCH", f"{base}/sdk-api/activation/{code7}/complete", ha,
            {"whips": 0, "flowing": 1, "agreement": 1},
        )
        expect_error("410 code_expired (complete after expiry)", status, body,
                     410, "code_expired")

        # Case-insensitive: lowercase input should resolve to the uppercase
        # code (C4). Use a fresh key to avoid the outstanding-code cap.
        ci_key = "sk_sensie_" + "d" * 64
        ci_h = {"x-api-key": ci_key}
        status, _, body = _hit("POST", f"{base}/sdk-api/trial/consent", ci_h, {
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        })
        cid8 = body["data"]["consent"]["id"]
        status, _, body = _hit(
            "POST", f"{base}/sdk-api/trial/activation-code", ci_h,
            {"consent_id": cid8},
        )
        code8 = body["data"]["activation"]["code"]
        status, _, body = _hit(
            "GET", f"{base}/sdk-api/activation/{code8.lower()}", ci_h,
        )
        if isinstance(body, dict) and "data" in body:
            inner = body["data"].get("activation", {})
            inner_status = inner.get("status") if isinstance(inner, dict) else None
        else:
            inner_status = None
        ok = status == 200 and inner_status == "pending"
        _step("case-insensitive lookup (C4)", ok,
              f"HTTP {status}, status={inner_status}")

        # Final tally.
        if _failed == 0:
            print(f"\nALL CHECKS PASSED")
            return 0
        print(f"\n{_failed} CHECK(S) FAILED")
        return 1
    finally:
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
