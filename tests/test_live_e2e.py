"""
test_live_e2e.py — End-to-end smoke for the live-gesture CLI path.

This file drives the REAL `sensie_eval.cli.main()` entrypoint against the
in-memory mock (tests/fixtures/mock_activation_server.py), with the mock
running in the same process. No subprocess: we call `main()` with the
expected argv and capture stdout/stderr the same way `tests/test_cli.py`
does. The simulated SomaCheck side runs in a thread that calls the mock
over HTTP via urllib (this also exercises the real wire shapes the CLI
ships with: `x-api-key`, the success envelope, claim->complete).

Contract references (from briefs/COMMON.md v1, clarifications C1-C10):
  C1  Envelope: success {"status":"success","data":{...}}; error
      {"status":"error","error":"<code>","message":...}.
  C4  8-char code, CSPRNG, case-insensitive, 30-min TTL.
  C5  Lapsed code -> 200 + status:"expired" (NOT an HTTP error); sensie is
      null unless completed.
  C6  Claim: pending -> claimed (atomic); already claimed -> 410.
  C7  Complete: pending -> 409 code_not_claimed; completed -> 410;
      expired -> 410. Atomic, immutable.
  C9  >3 outstanding codes -> 429 error:"rate_limited" + Retry-After.

Five cases required by the lane brief:
  1. happy path   — `run --live --yes` exits 0; prints a real read
                     (whips/flowing/agreement) and never "SYNTHETIC";
                     pending line shows a positive time-left annotation.
  2. expired      — advancing the mock clock past TTL -> exit 76.
  3. status       — `status <code>` for pending, completed, expired.
  4. rate limit   — a 4th outstanding code -> non-zero exit + the
                     rate_limited message.
  5. non-TTY      — `run --live` (no --yes) on a non-tty stdin -> exit 2
                     AND the mock never sees a consent record.
"""

from __future__ import annotations

import contextlib
import importlib.util as _ilu
import io
import json
import os
import re
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout


@contextlib.contextmanager
def redirect_stdin(target):
    """Backport of contextlib.redirect_stdin for Python <3.14."""
    original = sys.stdin
    sys.stdin = target
    try:
        yield target
    finally:
        sys.stdin = original

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)  # for the fixture import below

# The mock fixture is shipped as a file under tests/fixtures, not as a
# package; import it by path so the in-process start_server/stop_server
# symbols are reachable.
_FIXTURE_PATH = os.path.join(ROOT, "tests", "fixtures", "mock_activation_server.py")
_spec = _ilu.spec_from_file_location("_live_e2e_mock", _FIXTURE_PATH)
_mock = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mock)

from sensie_eval.cli import (  # noqa: E402
    EXIT_EXPIRED,
    EXIT_INTERRUPTED,
    EXIT_NO_KEY,
    main,
)

TRIAL_KEY = "sk_sensie_" + "0" * 64
APP_SECRET = "mock-app-secret"
DEVICE_ID = "test-device-001"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_mock_with_test_hooks():
    """Start the mock in-process with MOCK_ACTIVATION_ALLOW_TEST_HOOKS=1.

    Returns (server, base_url). Caller MUST call stop_server(server) on
    tearDown.
    """
    # The handler reads MOCK_ACTIVATION_ALLOW_TEST_HOOKS at request time
    # via os.environ.get, so we set it on the live os.environ. Tests run
    # serially within a single process; we restore in stop_mock().
    os.environ["MOCK_ACTIVATION_ALLOW_TEST_HOOKS"] = "1"
    server, base_url = _mock.start_server(
        port=0, ttl_seconds=1800, app_secret=APP_SECRET,
    )
    return server, base_url


def _stop_mock(server):
    try:
        _mock.stop_server(server)
    finally:
        os.environ.pop("MOCK_ACTIVATION_ALLOW_TEST_HOOKS", None)


def _post_json(url, payload, api_key=TRIAL_KEY, app_secret=None):
    """POST JSON with x-api-key. If app_secret is given, also send x-app-secret
    (claim + complete require it; consent + activation-code do not)."""
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    if app_secret is not None:
        headers["x-app-secret"] = app_secret
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _patch_json(url, payload, app_secret=APP_SECRET):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "x-app-secret": app_secret,
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _advance_clock(base_url, seconds):
    """Use the mock-only /__mock/advance hook to push the wall clock."""
    req = urllib.request.Request(
        f"{base_url}/__mock/advance?seconds={int(seconds)}",
        data=b"", method="POST",
        headers={"x-api-key": TRIAL_KEY},
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _wait_for_new_code(server, prior_codes, timeout=10.0):
    """Block until the mock's store has a code absent from `prior_codes`.

    The CLI thread (which we're racing against) will issue exactly one
    code via `request_activation_code`. We need its value before we can
    POST claim. Returning the 8-char code.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with server.store._lock:
            new = [c for c in server.store._codes.keys()
                   if c not in prior_codes]
        if new:
            return new[0]
        time.sleep(0.02)
    raise AssertionError(
        f"no new activation code appeared in {timeout}s; "
        f"prior_codes={prior_codes}"
    )


class _CliHarness(unittest.TestCase):
    """Shared setup: a fresh in-process mock per test."""

    def setUp(self):
        # Sandbox the env so tests cannot accidentally hit production
        # even if a developer forgot to unset it.
        self._saved_env = {}
        for var in ("SENSIE_API_KEY", "SENSIE_API_URL",
                    "MOCK_ACTIVATION_ALLOW_TEST_HOOKS"):
            self._saved_env[var] = os.environ.get(var)
        os.environ["SENSIE_API_KEY"] = TRIAL_KEY
        # Empty string keeps the fallback to DEFAULT_API_URL out of play
        # while still letting the harness override later in the test.
        os.environ.pop("SENSIE_API_URL", None)
        self.server, self.base_url = _start_mock_with_test_hooks()
        os.environ["SENSIE_API_URL"] = self.base_url

    def tearDown(self):
        _stop_mock(self.server)
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def _run_main(self, argv):
        """Call `sensie_eval.cli.main(argv)`, capturing stdout + stderr."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Case 1 — happy path
# ---------------------------------------------------------------------------


class TestLiveHappyPath(_CliHarness):
    """`run --live --yes` polls, the simulated app claims+completes, the
    CLI prints the real read and exits 0."""

    def test_run_live_happy_path(self):
        prior_codes = set(self.server.store._codes.keys())

        # The simulated SomaCheck app runs in a thread: wait for the
        # code, claim it with a device_id, then complete with the
        # contract-valid scalars. All headers as the real app would send.
        app_done = threading.Event()
        app_error = []

        def simulate_app():
            try:
                code = _wait_for_new_code(
                    self.server, prior_codes, timeout=15.0,
                )
                status, body = _post_json(
                    f"{self.base_url}/sdk-api/activation/{code}/claim",
                    {"device_id": DEVICE_ID},
                    app_secret=APP_SECRET,
                )
                self.assertEqual(status, 200, f"claim failed: {body}")
                self.assertEqual(body["status"], "success")
                status, body = _patch_json(
                    f"{self.base_url}/sdk-api/activation/{code}/complete",
                    {"whips": 3, "flowing": 1, "agreement": 2},
                )
                self.assertEqual(status, 200, f"complete failed: {body}")
                self.assertEqual(body["status"], "success")
            except Exception as exc:  # noqa: BLE001
                app_error.append(exc)
            finally:
                app_done.set()

        thread = threading.Thread(target=simulate_app, daemon=True)
        thread.start()

        # Timeout 30s default; keep an explicit large ceiling so a
        # blocked thread is loudly red, not silently green.
        exit_code, out, err = self._run_main(
            ["run", "--live", "--yes", "--poll-interval", "1",
             "--timeout", "5"]
        )

        thread.join(timeout=20.0)
        self.assertFalse(app_error, f"app thread error: {app_error!r}")

        # Exit code is the success code; not 76 (expired), not 130
        # (Ctrl-C), not 2 (refused), not 78 (no key).
        self.assertEqual(exit_code, 0,
                         f"err={err!r}\nout={out!r}")

        # Output prints the three scalars from the completed gesture.
        self.assertIn("whips:     3", out)
        self.assertIn("flowing:   1", out)
        self.assertIn("agreement: 2", out)

        # Crucial: the real read does NOT use the synthetic-demo wording.
        self.assertNotIn("SYNTHETIC", out,
                         "live read must never carry SYNTHETIC wording")

        # Real read banner is present.
        self.assertIn("Your live read", out)
        self.assertIn("Real gesture, done on your phone", out)

        # Consent text printed BEFORE the code (consent comes first).
        self.assertLess(out.index("Live gesture: what you are agreeing to"),
                        out.index("Enter this code"))

        # The pending status line printed while the CLI was still
        # waiting should carry a real positive time-left annotation,
        # not an empty string. Format from cli._format_remaining:
        #   " (Nm SSs left on the code)"
        m = re.search(
            r"status: pending[^\n]*\((\d+)m (\d+)s left on the code\)",
            out,
        )
        self.assertIsNotNone(
            m,
            "expected a pending status line with a positive time-left "
            f"annotation; got:\n{out}",
        )
        minutes, secs = int(m.group(1)), int(m.group(2))
        self.assertGreater(
            minutes * 60 + secs, 0,
            f"time-left must be positive while pending; got {minutes}m {secs}s",
        )

    def test_pending_line_uses_real_duration_not_zero(self):
        """Tighter check on the annotation: with a 30-min TTL and a poll
        that fires within seconds of issuance, time-left must be > 29m
        (no '0m 00s' or empty annotation)."""
        prior_codes = set(self.server.store._codes.keys())
        proceed = threading.Event()

        def simulate_quick_claim():
            code = _wait_for_new_code(
                self.server, prior_codes, timeout=15.0,
            )
            # Block the CLI's poll from seeing a completion until at
            # least one pending line has been printed.
            proceed.wait(timeout=10.0)
            _post_json(
                f"{self.base_url}/sdk-api/activation/{code}/claim",
                {"device_id": DEVICE_ID},
                app_secret=APP_SECRET,
            )
            _patch_json(
                f"{self.base_url}/sdk-api/activation/{code}/complete",
                {"whips": 3, "flowing": 1, "agreement": 2},
            )

        thread = threading.Thread(target=simulate_quick_claim, daemon=True)
        thread.start()

        # We need to know when at least one pending line has been
        # written so we can release the thread. Capture into a wrapper
        # StringIO that signals on every write.
        captured = []
        ready = threading.Event()

        class _SignalWriter(io.StringIO):
            def write(self, s):
                captured.append(s)
                if "status: pending" in s:
                    ready.set()
                return super().write(s)

        with redirect_stdout(_SignalWriter()):
            with redirect_stderr(io.StringIO()):
                code = main(["run", "--live", "--yes",
                             "--poll-interval", "1", "--timeout", "5"])
        # If we saw a pending line, release the simulated app to
        # complete the code; otherwise the test would block forever
        # on the CLI's poll loop.
        if ready.is_set():
            proceed.set()
        thread.join(timeout=20.0)

        self.assertEqual(code, 0,
                         "CLI must exit 0 once the app completes the code")
        # The first status: pending line must include a positive m+s.
        joined = "".join(captured)
        m = re.search(
            r"status: pending[^\n]*\((\d+)m (\d+)s left on the code\)",
            joined,
        )
        self.assertIsNotNone(m,
                             f"no pending line with time-left found:\n{joined}")
        minutes, secs = int(m.group(1)), int(m.group(2))
        self.assertGreater(minutes * 60 + secs, 29 * 60,
                           "expected a near-full 30 min TTL on the first "
                           f"pending poll; got {minutes}m {secs}s")
        self.assertLessEqual(minutes * 60 + secs, 30 * 60,
                             f"time-left must not exceed TTL; "
                             f"got {minutes}m {secs}s")


# ---------------------------------------------------------------------------
# Case 2 — expired via /__mock/advance
# ---------------------------------------------------------------------------


class TestLiveExpired(_CliHarness):
    """Advancing the mock clock past the code's TTL must yield exit 76."""

    def test_run_live_expired_via_clock_advance(self):
        prior_codes = set(self.server.store._codes.keys())

        def advance_after_issue():
            code = _wait_for_new_code(
                self.server, prior_codes, timeout=15.0,
            )
            # Push the mock wall clock well past the 30-min TTL so the
            # CLI's next poll observes the code as lapsed. C5 says the
            # GET then returns 200 + status:"expired", and the CLI maps
            # that to exit 76.
            _advance_clock(self.base_url, seconds=3600)

        thread = threading.Thread(target=advance_after_issue, daemon=True)
        thread.start()

        exit_code, out, err = self._run_main(
            ["run", "--live", "--yes", "--poll-interval", "1",
             "--timeout", "5"]
        )
        thread.join(timeout=10.0)

        self.assertEqual(exit_code, EXIT_EXPIRED,
                         f"expected EXIT_EXPIRED (76); got {exit_code}\n"
                         f"out={out!r}\nerr={err!r}")
        # Friendly message printed to stderr (per cli._expired_exit).
        self.assertIn("This activation code has expired.", err)
        # The "code is still valid" line must NOT appear on this path.
        self.assertNotIn("still valid", err)


# ---------------------------------------------------------------------------
# Case 3 — `status <code>` for pending, completed, expired
# ---------------------------------------------------------------------------


class TestLiveStatus(_CliHarness):
    """`sensie-eval status <code>` covers the resume path. Three sub-cases:
    pending (returns 0, prints status line), completed (returns 0, prints
    the real read), expired (returns 76)."""

    def _issue_code_via_consent(self):
        """Hit the mock directly to seed a code with a known consent.
        Returns (consent_id, activation_code, expires_at)."""
        status, body = _post_json(
            f"{self.base_url}/sdk-api/trial/consent",
            {"consent_version": "live-gesture-v1-draft",
             "scope": "live-gesture", "accepted": True},
        )
        self.assertEqual(status, 201, body)
        consent_id = body["data"]["consent"]["id"]
        status, body = _post_json(
            f"{self.base_url}/sdk-api/trial/activation-code",
            {"consent_id": consent_id},
        )
        self.assertEqual(status, 201, body)
        code = body["data"]["activation"]["code"]
        expires_at = body["data"]["activation"]["expires_at"]
        return consent_id, code, expires_at

    def test_status_pending_exits_0_prints_status_line(self):
        _, code, _ = self._issue_code_via_consent()
        exit_code, out, err = self._run_main(["status", code])
        self.assertEqual(exit_code, 0, f"err={err!r}")
        self.assertIn("status: pending", out)
        self.assertIn("waiting for the code to be entered", out)
        # Time-left annotation present and positive.
        m = re.search(r"\((\d+)m (\d+)s left on the code\)", out)
        self.assertIsNotNone(m, f"no time-left on pending: {out!r}")
        self.assertGreater(int(m.group(1)) * 60 + int(m.group(2)), 0)

    def test_status_completed_exits_0_prints_real_read(self):
        _, code, _ = self._issue_code_via_consent()
        # App side: claim + complete with the contract scalars.
        status, body = _post_json(
            f"{self.base_url}/sdk-api/activation/{code}/claim",
            {"device_id": DEVICE_ID},
            app_secret=APP_SECRET,
        )
        self.assertEqual(status, 200, body)
        status, body = _patch_json(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
            {"whips": 3, "flowing": 1, "agreement": 2},
        )
        self.assertEqual(status, 200, body)

        exit_code, out, err = self._run_main(["status", code])
        self.assertEqual(exit_code, 0, f"err={err!r}")
        self.assertIn("Your live read", out)
        self.assertIn("whips:     3", out)
        self.assertIn("flowing:   1", out)
        self.assertIn("agreement: 2", out)
        self.assertNotIn("SYNTHETIC", out)

    def test_status_expired_exits_76(self):
        _, code, _ = self._issue_code_via_consent()
        _advance_clock(self.base_url, seconds=3600)

        exit_code, out, err = self._run_main(["status", code])
        self.assertEqual(exit_code, EXIT_EXPIRED,
                         f"got {exit_code}; out={out!r} err={err!r}")
        self.assertIn("This activation code has expired.", err)


# ---------------------------------------------------------------------------
# Case 3b — D9: agreement is optional; null renders as 'not provided'
# ---------------------------------------------------------------------------


def _patch_json_no_agreement(url, app_secret=APP_SECRET):
    """Like _patch_json but sends a body with NO `agreement` key at all,
    mirroring the real SomaCheck app (which never invents an agreement
    value). The mock must accept this as a valid complete (D9)."""
    body = json.dumps({"whips": 3, "flowing": 1}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "x-app-secret": app_secret,
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _get_json(url, api_key=TRIAL_KEY):
    """GET via x-api-key. Used to inspect activation state after a write."""
    req = urllib.request.Request(
        url, headers={"x-api-key": api_key}, method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _issue_pending_code_via_mock(harness):
    """Issue one activation code directly against the harness's in-process
    mock. Returns (consent_id, code, activation_body).

    Lives at module scope so both TestLiveStatus and TestLiveAgreementOptional
    can reuse it (TestLiveStatus keeps its existing _issue_code_via_consent
    for backwards-compat)."""
    status, body = _post_json(
        f"{harness.base_url}/sdk-api/trial/consent",
        {"consent_version": "live-gesture-v1-draft",
         "scope": "live-gesture", "accepted": True},
    )
    harness.assertEqual(status, 201, body)
    consent_id = body["data"]["consent"]["id"]
    status, body = _post_json(
        f"{harness.base_url}/sdk-api/trial/activation-code",
        {"consent_id": consent_id},
    )
    harness.assertEqual(status, 201, body)
    code = body["data"]["activation"]["code"]
    expires_at = body["data"]["activation"]["expires_at"]
    return consent_id, code, expires_at


class TestLiveAgreementOptional(_CliHarness):
    """D9: the SomaCheck app never sends an `agreement` value (it's optional
    feedback collected AFTER the reveal and must never be invented). The
    contract says complete must accept agreement OMITTED or null, the GET
    returns sensie.agreement == null, and the CLI must render that as
    'not provided' — never 0, never a default, never any verdict derived
    from agreement. Mirrors Lane 3's print_live_report change.

    Three sub-cases required by the lane brief:
      A. `run --live` after a complete-with-agreement-OMITTED — the printed
         read shows 'agreement: not provided' (not 'None', not '0').
      B. `status <code>` after the same — same line in the status report.
      C. complete with agreement=2 still prints 'agreement: 2' (value path
         is unchanged from the existing happy-path assertion; re-confirmed
         in this class for symmetry).
      D. agreement=0 is rejected with 400 invalid_payload, surfaced as a
         non-zero exit (the simulated app surfaces the 400; the CLI's
         poll loop sees a SensieApiError and exits 1).
    """

    def _app_thread_complete_no_agreement(self, prior_codes):
        """Simulated SomaCheck: claim, then complete with NO agreement key.
        The mock must accept this (D9). The CLI's read of the resulting
        sensie is asserted by the calling test."""
        code = _wait_for_new_code(
            self.server, prior_codes, timeout=15.0,
        )
        status, body = _post_json(
            f"{self.base_url}/sdk-api/activation/{code}/claim",
            {"device_id": DEVICE_ID},
            app_secret=APP_SECRET,
        )
        self.assertEqual(status, 200, f"claim failed: {body}")
        status, body = _patch_json_no_agreement(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
        )
        self.assertEqual(
            status, 200,
            f"complete (no agreement) must succeed per D9; "
            f"got {status} {body}",
        )

    def test_run_live_prints_agreement_not_provided_when_omitted(self):
        """A. `run --live` after a no-agreement complete must print
        `agreement: not provided` (not None, not 0, not the int 2)."""
        prior_codes = set(self.server.store._codes.keys())

        thread = threading.Thread(
            target=self._app_thread_complete_no_agreement,
            args=(prior_codes,), daemon=True,
        )
        thread.start()

        exit_code, out, err = self._run_main(
            ["run", "--live", "--yes", "--poll-interval", "1",
             "--timeout", "5"]
        )
        thread.join(timeout=20.0)

        self.assertEqual(exit_code, 0,
                         f"err={err!r}\nout={out!r}")
        # The line printed by print_live_report when read['agreement']
        # is None — Lane 3's contract for D9.
        self.assertIn("agreement: not provided", out,
                      f"expected 'agreement: not provided' in:\n{out}")
        # And explicitly NOT the strings the CLI must never emit:
        #   - "agreement: None"  (would mean we forgot to format)
        #   - "agreement: 0"     (would mean we defaulted to a sentinel)
        #   - "agreement: 2"     (would mean we invented a verdict)
        self.assertNotIn("agreement: None", out)
        self.assertNotIn("agreement: 0\n", out)
        self.assertNotIn("agreement: 2\n", out)
        # The other two scalars from the complete payload still print.
        self.assertIn("whips:     3", out)
        self.assertIn("flowing:   1", out)

    def test_status_prints_agreement_not_provided_when_omitted(self):
        """B. `status <code>` after a no-agreement complete prints the same
        'agreement: not provided' line."""
        # Issue + claim + complete-with-no-agreement directly via HTTP.
        _, code, _ = _issue_pending_code_via_mock(self)
        status, _ = _post_json(
            f"{self.base_url}/sdk-api/activation/{code}/claim",
            {"device_id": DEVICE_ID},
            app_secret=APP_SECRET,
        )
        self.assertEqual(status, 200)
        status, body = _patch_json_no_agreement(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
        )
        self.assertEqual(status, 200, body)

        exit_code, out, err = self._run_main(["status", code])
        self.assertEqual(exit_code, 0, f"err={err!r}\nout={out!r}")
        self.assertIn("agreement: not provided", out,
                      f"status report must show 'agreement: not provided'; "
                      f"got:\n{out}")
        # Same negative checks as above.
        self.assertNotIn("agreement: None", out)
        self.assertNotIn("agreement: 0\n", out)
        self.assertNotIn("agreement: 2\n", out)

    def test_run_live_still_prints_agreement_value_when_provided(self):
        """C. The value-path is unchanged: complete with agreement=2 still
        prints 'agreement: 2' (Lane 3's renderer formats a non-None value
        as-is). Re-confirms we did not regress the old happy path."""
        prior_codes = set(self.server.store._codes.keys())

        app_done = threading.Event()
        app_error = []

        def simulate_app():
            try:
                code = _wait_for_new_code(
                    self.server, prior_codes, timeout=15.0,
                )
                status, body = _post_json(
                    f"{self.base_url}/sdk-api/activation/{code}/claim",
                    {"device_id": DEVICE_ID},
                    app_secret=APP_SECRET,
                )
                self.assertEqual(status, 200, f"claim failed: {body}")
                status, body = _patch_json(
                    f"{self.base_url}/sdk-api/activation/{code}/complete",
                    {"whips": 3, "flowing": 1, "agreement": 2},
                )
                self.assertEqual(status, 200, f"complete failed: {body}")
            except Exception as exc:  # noqa: BLE001
                app_error.append(exc)
            finally:
                app_done.set()

        thread = threading.Thread(target=simulate_app, daemon=True)
        thread.start()
        exit_code, out, err = self._run_main(
            ["run", "--live", "--yes", "--poll-interval", "1",
             "--timeout", "5"]
        )
        thread.join(timeout=20.0)
        self.assertFalse(app_error, f"app thread error: {app_error!r}")

        self.assertEqual(exit_code, 0, f"err={err!r}\nout={out!r}")
        self.assertIn("agreement: 2", out,
                      f"value-path agreement must still print; got:\n{out}")
        # And NOT 'not provided'.
        self.assertNotIn("agreement: not provided", out)

    def test_complete_with_agreement_zero_is_rejected(self):
        """D. agreement=0 is NOT in {-1, 1, 2}, so it must 400. Drive
        this by issuing a code, claiming it, and sending complete with
        agreement=0 directly. The mock must return 400 invalid_payload
        (value-domain is checked before the code is looked up — D7 —
        and agreement=0 is still out-of-domain after D9)."""
        _, code, _ = _issue_pending_code_via_mock(self)
        status, _ = _post_json(
            f"{self.base_url}/sdk-api/activation/{code}/claim",
            {"device_id": DEVICE_ID},
            app_secret=APP_SECRET,
        )
        self.assertEqual(status, 200)
        status, body = _patch_json(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
            {"whips": 3, "flowing": 1, "agreement": 0},
        )
        self.assertEqual(
            status, 400,
            f"agreement=0 must be rejected with 400; got {status} {body!r}",
        )
        self.assertEqual(body.get("error"), "invalid_payload", body)
        # And the code must NOT have been completed (single-use, atomic).
        # Subsequent GET must still see status=claimed.
        status, body = _get_json(
            f"{self.base_url}/sdk-api/activation/{code}",
        )
        self.assertEqual(status, 200, body)
        # GET shape: status is "claimed" (a 400 on complete doesn't flip
        # the code to completed; the value-domain check is upfront).
        inner = body["data"]["activation"]
        self.assertEqual(
            inner.get("status"), "claimed",
            f"a 400 on complete must not flip the code to completed; "
            f"got activation={inner!r}",
        )


# ---------------------------------------------------------------------------
# Case 4 — rate limit (4th outstanding code)
# ---------------------------------------------------------------------------


class TestLiveRateLimited(_CliHarness):
    """C9: 4 outstanding codes on a single key -> 429 rate_limited.

    Issue 3 codes via direct mock hits, then ask the CLI to do its
    consent + activation-code path. The 4th issuance is rejected with
    429 error:"rate_limited"; the CLI prints the rate_limited message
    and exits non-zero (cli._live_error_exit returns 1 for
    SensieRateLimitedError)."""

    def test_fourth_outstanding_code_is_rate_limited(self):
        # Seed 3 outstanding (pending, not expired, not completed) codes
        # directly so we don't need to drive 3 prior CLI runs.
        for _ in range(3):
            self._issue_pending_code()

        # Now the CLI tries to add a 4th. Use a very short timeout
        # because the error fires BEFORE we enter the poll loop.
        exit_code, out, err = self._run_main(
            ["run", "--live", "--yes", "--poll-interval", "1",
             "--timeout", "1"]
        )
        # cli._live_error_exit returns 1 (not 76 or 75).
        self.assertNotEqual(exit_code, 0, f"expected non-zero exit; "
                         f"out={out!r} err={err!r}")
        self.assertNotEqual(exit_code, EXIT_EXPIRED)
        self.assertNotEqual(exit_code, EXIT_INTERRUPTED)
        # The CLI's rate-limit copy is on stderr.
        self.assertIn("rate_limited", err.lower())
        self.assertIn("HTTP 429", err)
        # Retry-After is exposed by SensieRateLimitedError.retry_after.
        self.assertIn("Retry after", err)

    def _issue_pending_code(self):
        status, body = _post_json(
            f"{self.base_url}/sdk-api/trial/consent",
            {"consent_version": "live-gesture-v1-draft",
             "scope": "live-gesture", "accepted": True},
        )
        self.assertEqual(status, 201, body)
        consent_id = body["data"]["consent"]["id"]
        status, body = _post_json(
            f"{self.base_url}/sdk-api/trial/activation-code",
            {"consent_id": consent_id},
        )
        self.assertEqual(status, 201, body)


# ---------------------------------------------------------------------------
# Case 5 — non-TTY without --yes
# ---------------------------------------------------------------------------


class TestLiveNonTtyRefuses(_CliHarness):
    """With stdin not a TTY and no --yes, the CLI must refuse consent
    with exit 2 AND must NOT call the API (so the mock never sees a
    consent record)."""

    def test_non_tty_without_yes_exits_2_no_consent(self):
        consents_before = len(self.server.store._consents)
        codes_before = len(self.server.store._codes)

        # StringIO.isatty() returns False, so sys.stdin.isatty() is
        # False for the duration of main().
        fake_stdin = io.StringIO("")
        with redirect_stdin(fake_stdin):
            exit_code, out, err = self._run_main(
                ["run", "--live", "--poll-interval", "1", "--timeout", "1"]
            )

        self.assertEqual(exit_code, 2,
                         f"expected exit 2 (consent refused); got {exit_code}\n"
                         f"err={err!r}")
        self.assertIn("consent needs a person at the keyboard", err)
        # Mock state proves no consent + no code were created.
        self.assertEqual(
            len(self.server.store._consents), consents_before,
            "non-TTY refusal must not create a consent record",
        )
        self.assertEqual(
            len(self.server.store._codes), codes_before,
            "non-TTY refusal must not create an activation code",
        )


# ---------------------------------------------------------------------------
# Revert-to-prove — mock-only single-use guarantee on `complete`
# ---------------------------------------------------------------------------


class TestRevertToProveAtomicComplete(_CliHarness):
    """When the mock's single-use guard on `complete` is removed (the
    `rec["status"] == "completed"` branch raising 410 is bypassed), a
    second complete on the same code must SUCCEED. We prove the test
    would go red without that guard by patching the mock in this test
    process only and showing the second complete succeeds. With the
    guard intact (the real mock), the second complete MUST 410.

    This isolates the property under test from the real CLI: the
    guarantee lives in the mock, and the mock is what we are
    protecting.
    """

    def test_second_complete_on_same_code_returns_410(self):
        _, code, _ = self._seed_code()
        # First complete: success.
        status, body = _patch_json(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
            {"whips": 3, "flowing": 1, "agreement": 2},
        )
        self.assertEqual(status, 200, body)

        # Second complete on the same code: 410 code_already_claimed.
        status, body = _patch_json(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
            {"whips": 3, "flowing": 1, "agreement": 2},
        )
        self.assertEqual(status, 410, f"second complete must 410; got "
                         f"{status} {body}")
        self.assertEqual(body.get("error"), "code_already_claimed",
                         body)

    def test_second_complete_succeeds_when_guard_removed(self):
        """The revert-to-prove pair. Patch the live mock to remove the
        single-use guard on `complete`, then run the same scenario.
        The second complete must now succeed — proving the guard (not
        some other property) is what made the previous test red."""
        _, code, _ = self._seed_code()
        # First complete: success.
        status, _ = _patch_json(
            f"{self.base_url}/sdk-api/activation/{code}/complete",
            {"whips": 3, "flowing": 1, "agreement": 2},
        )
        self.assertEqual(status, 200)

        # Patch the live mock in-process: monkey-patch the store's
        # `complete` method to skip the completed-already guard. This
        # is in-process only; the on-disk mock is untouched.
        real_complete = self.server.store.complete

        def lax_complete(code_, whips, flowing, agreement):
            # Same as real, but the "completed -> 410" branch is gone.
            # Everything else (value-domain validation, expiry,
            # pending-not-claimed) stays.
            from tests.fixtures.mock_activation_server import _ApiError
            if (not isinstance(whips, int) or isinstance(whips, bool)
                    or whips < 0):
                raise _ApiError(400, "invalid_payload",
                                "whips must be a non-negative integer")
            if flowing not in _mock._Store.__init__.__globals__[
                    "VALID_FLOWING"]:
                raise _ApiError(400, "invalid_payload", "flowing invalid")
            if agreement not in _mock._Store.__init__.__globals__[
                    "VALID_AGREEMENT"]:
                raise _ApiError(400, "invalid_payload",
                                "agreement invalid")
            with self.server.store._lock:
                rec = self.server.store._codes.get(code_)
                if rec is None:
                    raise _ApiError(404, "code_not_found",
                                    "unknown activation code")
                if (rec["status"] != "completed"
                        and self.server.store.now() > rec["expires_at"]):
                    rec["status"] = "expired"
                if rec["status"] == "pending":
                    raise _ApiError(409, "code_not_claimed",
                                    "code must be claimed first")
                if rec["status"] == "expired":
                    raise _ApiError(410, "code_expired",
                                    "code has expired")
                # completed branch: NO 410, just overwrite (the lax path).
                rec["status"] = "completed"
                rec["completed_at"] = self.server.store.now()
                rec["sensie"] = {"whips": whips, "flowing": flowing,
                                 "agreement": agreement}
                return {"activation": {
                    "status": "completed",
                    "completed_at": self.server.store._iso(
                        rec["completed_at"]),
                }}

        try:
            self.server.store.complete = lax_complete

            status, body = _patch_json(
                f"{self.base_url}/sdk-api/activation/{code}/complete",
                {"whips": 3, "flowing": 1, "agreement": 2},
            )
            # Without the guard, this should NOT 410. If this assertion
            # fires, the guard we tested above is NOT what was making
            # the original test red — investigate.
            self.assertNotEqual(status, 410,
                                "guard removal did not change behavior: "
                                f"second complete still 410 (body={body!r})")
            self.assertEqual(status, 200,
                             f"expected 200 with guard removed; got "
                             f"{status} {body}")
        finally:
            self.server.store.complete = real_complete

    def _seed_code(self):
        """Issue a code via the mock and claim it. Returns the code."""
        status, body = _post_json(
            f"{self.base_url}/sdk-api/trial/consent",
            {"consent_version": "live-gesture-v1-draft",
             "scope": "live-gesture", "accepted": True},
        )
        self.assertEqual(status, 201, body)
        consent_id = body["data"]["consent"]["id"]
        status, body = _post_json(
            f"{self.base_url}/sdk-api/trial/activation-code",
            {"consent_id": consent_id},
        )
        self.assertEqual(status, 201, body)
        code = body["data"]["activation"]["code"]
        # Claim so complete has a non-pending record.
        status, body = _post_json(
            f"{self.base_url}/sdk-api/activation/{code}/claim",
            {"device_id": DEVICE_ID},
            app_secret=APP_SECRET,
        )
        self.assertEqual(status, 200, body)
        return consent_id, code, body


# ---------------------------------------------------------------------------
# Case 6 — F2a: draft consent is refused against the production default host
# ---------------------------------------------------------------------------


# Module-scoped recording state for the draft-guard test below. The
# stub client (defined here so it can be referenced from setUp) holds
# no reference to the test instance — tests run isolated, but class
# bodies and the surrounding module both live for the duration of the
# test process.
_calls = []


class _ForbiddenCall(AssertionError):
    """Raised by the recording stub if any API method is invoked when
    the draft guard should have fired."""


class _RecordingClient:
    """Stand-in for SensieApiClient used by the draft-guard test.

    Any method call is recorded and then raises _ForbiddenCall. The
    guard in cli.run_live must short-circuit BEFORE this stub is ever
    asked to do anything, so the recorded list ends up containing at
    most a single ``__init__`` entry (which the test treats as
    acceptable — the client object IS constructed before the guard
    check).
    """

    def post_consent(self, *a, **kw):
        _calls.append(("post_consent", a, kw))
        raise _ForbiddenCall(
            "post_consent must NOT be called when the draft "
            "guard fires against production"
        )

    def request_activation_code(self, *a, **kw):
        _calls.append(("request_activation_code", a, kw))
        raise _ForbiddenCall(
            "request_activation_code must NOT be called when "
            "the draft guard fires against production"
        )

    def get_activation(self, *a, **kw):
        _calls.append(("get_activation", a, kw))
        raise _ForbiddenCall(
            "get_activation must NOT be called when the draft "
            "guard fires against production"
        )


class TestLiveDraftGuardAgainstProduction(unittest.TestCase):
    """Lane 3 introduced a guard (F2a): when the bundled CONSENT_VERSION
    ends in ``-draft`` and the resolved API host equals the production
    default (``sensie_eval.api_client.DEFAULT_API_URL``), ``run --live``
    must exit 2 AND must never send a consent request.

    Two safety properties this test must prove:
      * the CLI exits 2 with the documented error copy on stderr;
      * no method on the API client is called (so no HTTP request to
        production is ever issued).

    The second property is the important one: the lane brief explicitly
    forbids contacting the real production host. We prove it by
    monkeypatching ``sensie_eval.cli._client_from_env`` to return a
    *recording* stub whose every method raises AssertionError if called.
    If the draft guard fires (which it must), the stub is constructed
    but no method is invoked, so the assertion list stays empty. If a
    future refactor regresses the guard, one of those calls would fire
    and this test would go red.
    """

    def setUp(self):
        # Capture original env so we never leak SENSIE_API_URL to other
        # tests, and so the in-process mock from sibling tests isn't
        # accidentally picked up here. This test does NOT use the
        # in-process mock — the whole point is to prove NO request goes
        # out (and certainly not to production).
        self._saved_env = {}
        for var in ("SENSIE_API_KEY", "SENSIE_API_URL"):
            self._saved_env[var] = os.environ.get(var)
        os.environ["SENSIE_API_KEY"] = TRIAL_KEY

        # Point at the production default exactly as the CLI resolves it
        # in production. We import DEFAULT_API_URL so this test stays
        # in sync if the constant ever moves.
        from sensie_eval.api_client import DEFAULT_API_URL as PROD
        self._prod_url = PROD
        os.environ["SENSIE_API_URL"] = PROD

        # Recording stub. The list lives at module scope so the
        # nested-client class can append to it without holding a
        # reference to the test instance.
        self.calls = _calls
        _calls.clear()

        # Sentinel: if the real client construction is bypassed, we want
        # the test to fail loudly rather than fall through and silently
        # hit production.
        from sensie_eval import cli as _cli_mod
        # CRITICAL: capture the real function BEFORE we overwrite the
        # attribute, otherwise tearDown would re-install our own stub
        # and leak it to every test that runs after us.
        real_cli_from_env = _cli_mod._client_from_env

        def _stub_client_from_env():
            # Mirror the real one enough to satisfy the call: it returns
            # a client object (or an int exit code when key is missing).
            # We assume the key is set in setUp; mirror the success path.
            _calls.append(("__init__", (os.environ.get("SENSIE_API_KEY"),)))
            return _RecordingClient()

        _cli_mod._client_from_env = _stub_client_from_env
        self._original_client_from_env = real_cli_from_env

    def tearDown(self):
        from sensie_eval import cli as _cli_mod
        _cli_mod._client_from_env = self._original_client_from_env
        for var, val in self._saved_env.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    def test_run_live_against_prod_default_exits_2_and_makes_no_request(self):
        out, err = io.StringIO(), io.StringIO()
        # Sanity: we are actually pointing at the production default.
        # If this assert ever fails, either DEFAULT_API_URL changed or
        # the env stub above is wrong; either way this test is no
        # longer proving what it claims to prove.
        from sensie_eval.api_client import DEFAULT_API_URL as PROD
        self.assertEqual(os.environ.get("SENSIE_API_URL"), PROD,
                         "this test must point at the production default")
        self.assertTrue(PROD.startswith("https://"),
                        "production default must be https")

        # Run the CLI. We pass --yes so a non-TTY stdin doesn't get
        # blamed for the refusal — the refusal must come from the
        # draft guard, not the TTY check.
        try:
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = main(["run", "--live", "--yes",
                                  "--poll-interval", "1", "--timeout", "1"])
        finally:
            # Belt-and-suspenders: even if an assertion below fires,
            # make sure the next test in this process sees the real
            # _client_from_env, not our stub.
            from sensie_eval import cli as _cli_mod
            _cli_mod._client_from_env = self._original_client_from_env

        # (a) Exit code 2.
        self.assertEqual(
            exit_code, 2,
            f"draft-against-production must exit 2; got {exit_code}\n"
            f"out={out.getvalue()!r}\nerr={err.getvalue()!r}",
        )

        # (b) The documented error copy is printed on stderr. We assert
        # on a stable substring ("draft") and the production-host cue
        # so we don't over-couple to Lane 3's exact wording.
        err_text = err.getvalue()
        self.assertIn("draft", err_text.lower(),
                      f"expected 'draft' in stderr; got: {err_text!r}")
        self.assertIn("production", err_text.lower(),
                      f"expected 'production' in stderr; got: {err_text!r}")
        # The CLI says nothing was sent — that's the safety claim.
        self.assertIn("nothing was sent", err_text.lower(),
                      f"expected 'nothing was sent' in stderr; "
                      f"got: {err_text!r}")

        # (c) No API method was called. The stub constructor may run
        # (the CLI builds the client before the guard check), but the
        # only method that matters — post_consent — must not be
        # reached. We accept either: zero calls of any kind, or only
        # the constructor. Anything else means the guard regressed.
        method_calls = [c for c in self.calls if c[0] != "__init__"]
        self.assertEqual(
            method_calls, [],
            "draft guard must fire BEFORE any API call; "
            f"recorded method calls: {method_calls!r}",
        )

        # (d) Consent text was NOT printed to stdout. The guard fires
        # before the consent banner is displayed; if the banner
        # appears, the guard has regressed and a real consent request
        # is about to be sent.
        self.assertNotIn("Live gesture: what you are agreeing to",
                         out.getvalue(),
                         "draft guard must fire before the consent banner "
                         "is printed; got:\n" + out.getvalue())


# ---------------------------------------------------------------------------
# Tiny stdin-redirection context manager (stdlib only)
# ---------------------------------------------------------------------------
# (See the redirect_stdin helper at the top of this file; this file does
# NOT rely on contextlib.redirect_stdin because that name only landed in
# Python 3.14 and the project still tests on 3.13.)

if __name__ == "__main__":
    unittest.main()