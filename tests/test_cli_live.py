"""
test_cli_live.py — Unit tests for `sensie-eval run --live` and `status`.

No network: the API client is replaced with a MagicMock, and time.sleep /
time.monotonic are patched so polling loops run instantly.

Tests cover:
  1. Consent: non-TTY without --yes refuses (exit 2, no request made);
     interactive 'n' records nothing; --yes / interactive 'y' proceeds
  2. Consent is recorded before a code is requested; copy carries the
     placeholder and the draft version
  3. Up-front block: install link, code, deep link, time budget
  4. Polling: status transitions, completed render (not SYNTHETIC),
     expired -> 76, timeout -> resume hint + 0, Ctrl-C -> resume hint + 130
  5. `status` subcommand
  6. 403 consent_required / 429 rate_limited / 429 quota_exceeded / 401 / 78
  7. The offline/--api routing report is unchanged by the live parameter
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sensie_eval.api_client import (
    SensieActivationGoneError,
    SensieActivationNotFoundError,
    SensieAuthError,
    SensieConsentRequiredError,
    SensieQuotaError,
    SensieRateLimitedError,
)
from sensie_eval.cli import (
    CONSENT_COPY,
    CONSENT_VERSION,
    EXIT_AUTH,
    EXIT_EXPIRED,
    EXIT_NO_KEY,
    EXIT_QUOTA,
    INSTALL_URL,
    NEEDS_POLICY_APPROVAL,
    main,
    print_routing_report,
)

FAKE_KEY = "sk_sensie_" + "0" * 64
CODE = "ABCD2345"
READ = {"whips": 3, "flowing": 1, "agreement": 2}


def make_client(states=None):
    """Client whose get_activation walks through `states` (last one repeats)."""
    client = mock.MagicMock()
    client.post_consent.return_value = {
        "id": "consent-1", "consented_at": "2026-09-21T12:00:00Z"}
    client.request_activation_code.return_value = {
        "code": CODE, "expires_at": "2099-01-01T00:00:00Z",
        "install_url": INSTALL_URL}
    seq = list(states or [])

    def get_activation(code):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item

    if seq:
        client.get_activation.side_effect = get_activation
    return client


def act(status, sensie=None):
    return {"status": status, "sensie": sensie,
            "expires_at": "2099-01-01T00:00:00Z"}


class LiveTestCase(unittest.TestCase):

    def invoke(self, argv, client, env=None, tty=False, stdin_answer=None,
               monotonic=None):
        """Run main(argv) with the client patched. Returns (code, out, err)."""
        env = {"SENSIE_API_KEY": FAKE_KEY} if env is None else env
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.MagicMock()
        stdin.isatty.return_value = tty
        patches = [
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("sensie_eval.cli.SensieApiClient", return_value=client),
            mock.patch("sensie_eval.cli.time.sleep"),
            mock.patch("sys.stdin", stdin),
        ]
        if monotonic is not None:
            patches.append(mock.patch("sensie_eval.cli.time.monotonic",
                                      side_effect=monotonic))
        if stdin_answer is not None:
            patches.append(mock.patch("builtins.input",
                                      side_effect=stdin_answer
                                      if isinstance(stdin_answer, BaseException)
                                      else [stdin_answer]))
        for var in ("SENSIE_API_KEY", "SENSIE_API_URL"):
            if var not in env:
                os.environ.pop(var, None)
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv)
        finally:
            for p in reversed(patches):
                p.stop()
        return code, out.getvalue(), err.getvalue()


class TestConsent(LiveTestCase):

    def test_non_tty_without_yes_refuses_and_makes_no_request(self):
        client = make_client([act("pending")])
        code, out, err = self.invoke(["run", "--live"], client, tty=False)
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        client.post_consent.assert_not_called()
        client.request_activation_code.assert_not_called()
        client.get_activation.assert_not_called()

    def test_interactive_no_records_nothing(self):
        client = make_client([act("pending")])
        code, out, _ = self.invoke(["run", "--live"], client, tty=True,
                                   stdin_answer="n")
        self.assertEqual(code, 1)
        self.assertIn("No consent recorded", out)
        client.post_consent.assert_not_called()

    def test_interactive_eof_is_a_no(self):
        client = make_client([act("pending")])
        code, _, _ = self.invoke(["run", "--live"], client, tty=True,
                                 stdin_answer=EOFError())
        self.assertEqual(code, 1)
        client.post_consent.assert_not_called()

    def test_interactive_yes_proceeds(self):
        client = make_client([act("completed", READ)])
        code, out, _ = self.invoke(["run", "--live"], client, tty=True,
                                   stdin_answer="y")
        self.assertEqual(code, 0)
        client.post_consent.assert_called_once_with(CONSENT_VERSION)

    def test_yes_flag_skips_prompt_but_still_prints_consent(self):
        client = make_client([act("completed", READ)])
        code, out, _ = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertIn(CONSENT_COPY, out)
        client.post_consent.assert_called_once_with(CONSENT_VERSION)

    def test_consent_recorded_before_code_requested(self):
        client = make_client([act("completed", READ)])
        self.invoke(["run", "--live", "--yes"], client)
        names = [c[0] for c in client.method_calls]
        self.assertLess(names.index("post_consent"),
                        names.index("request_activation_code"))
        client.request_activation_code.assert_called_once_with("consent-1")

    def test_consent_copy_content(self):
        self.assertEqual(CONSENT_VERSION, "live-gesture-v1-draft")
        self.assertIn(NEEDS_POLICY_APPROVAL, CONSENT_COPY)
        self.assertIn("pending Sensie policy approval", NEEDS_POLICY_APPROVAL)
        for word in ("whips", "flowing", "agreement", "stop at any time",
                     "Raw motion stays on the phone"):
            self.assertIn(word, CONSENT_COPY)

    def test_live_and_api_together_rejected(self):
        client = make_client([act("pending")])
        code, _, err = self.invoke(["run", "--live", "--api", "--yes"], client)
        self.assertEqual(code, 2)
        client.post_consent.assert_not_called()


class TestUpfrontAndPolling(LiveTestCase):

    def test_upfront_block(self):
        client = make_client([act("completed", READ)])
        _, out, _ = self.invoke(["run", "--live", "--yes"], client)
        self.assertIn(INSTALL_URL, out)
        self.assertIn(CODE, out)
        self.assertIn(f"somacheck://activate/{CODE}", out)
        self.assertIn("15-20 minutes including calibration", out)
        self.assertIn("valid for\n30 minutes", out)
        self.assertIn("already ran the offline demo", out)
        self.assertIn("real gesture", out)
        # The offline evaluation must not run on the live path.
        self.assertNotIn("SUBJECT-DISJOINT EVALUATION REPORT", out)
        client.create_session.assert_not_called()

    def test_status_transitions_shown_in_order(self):
        client = make_client([act("pending"), act("pending"), act("claimed"),
                               act("completed", READ)])
        code, out, _ = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertIn("status: pending", out)
        self.assertIn("status: claimed", out)
        self.assertLess(out.index("status: pending"),
                        out.index("status: claimed"))
        self.assertLess(out.index("status: claimed"),
                        out.index("Your live read"))
        # Unchanged status is not reprinted on every poll.
        self.assertEqual(out.count("status: pending"), 1)
        self.assertEqual(client.get_activation.call_count, 4)

    def test_poll_interval_passed_to_sleep(self):
        client = make_client([act("pending"), act("completed", READ)])
        with mock.patch("sensie_eval.cli.time.sleep") as sleep:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"SENSIE_API_KEY": FAKE_KEY}), \
                    mock.patch("sensie_eval.cli.SensieApiClient",
                               return_value=client), \
                    redirect_stdout(out), redirect_stderr(err):
                code = main(["run", "--live", "--yes",
                             "--poll-interval", "2.5"])
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(2.5)

    def test_completed_render_is_live_not_synthetic(self):
        client = make_client([act("completed", READ)])
        code, out, _ = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertIn("Your live read", out)
        self.assertIn("whips:     3", out)
        self.assertIn("flowing:   1", out)
        self.assertIn("agreement: 2", out)
        self.assertNotIn("SYNTHETIC", out)
        self.assertNotIn("annotator", out.split("Your live read")[1])
        self.assertNotIn("Route accordingly", out)

    def test_expired_exits_76(self):
        client = make_client([act("pending"), act("expired")])
        code, out, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, EXIT_EXPIRED)
        self.assertEqual(EXIT_EXPIRED, 76)
        self.assertIn("expired", err)
        self.assertIn("No result was produced", err)
        self.assertIn("nothing is stored beyond the expired code", err)
        self.assertNotIn("Your live read", out)

    def test_ctrl_c_prints_resume_hint_and_exits_130(self):
        client = make_client([act("pending"), KeyboardInterrupt()])
        code, out, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 130)
        self.assertIn(f"`sensie-eval status {CODE}` to check back later", out)
        self.assertNotIn("Traceback", err)

    def test_timeout_prints_resume_hint_and_exits_0(self):
        client = make_client([act("pending")])
        # monotonic: start=0 -> deadline 60s; first poll at t=0, next at 61.
        ticks = iter([0, 0, 0, 61, 61, 61, 61, 61])
        code, out, _ = self.invoke(
            ["run", "--live", "--yes", "--timeout", "1"], client,
            monotonic=lambda: next(ticks))
        self.assertEqual(code, 0)
        self.assertIn("Stopped waiting after 1 minutes", out)
        self.assertIn(f"`sensie-eval status {CODE}` to check back later", out)
        self.assertNotIn("Your live read", out)

    def test_repeated_network_failures_give_up_with_hint(self):
        client = make_client([OSError("network down")])
        code, out, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertIn("Could not reach the Sensie API", err)
        self.assertIn(f"sensie-eval status {CODE}", out)
        self.assertEqual(client.get_activation.call_count, 6)

    def test_one_network_blip_is_tolerated(self):
        client = make_client([OSError("blip"), act("completed", READ)])
        code, out, _ = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 0)
        self.assertIn("Your live read", out)


class TestStatusCommand(LiveTestCase):

    def test_completed(self):
        client = make_client([act("completed", READ)])
        code, out, _ = self.invoke(["status", CODE.lower()], client)
        self.assertEqual(code, 0)
        self.assertIn("Your live read", out)
        self.assertNotIn("SYNTHETIC", out)
        client.get_activation.assert_called_once_with(CODE)
        client.post_consent.assert_not_called()

    def test_expired(self):
        client = make_client([act("expired")])
        code, _, err = self.invoke(["status", CODE], client)
        self.assertEqual(code, EXIT_EXPIRED)
        self.assertIn("No result was produced", err)

    def test_pending_and_claimed_show_status_and_time_left(self):
        for status in ("pending", "claimed"):
            client = make_client([act(status)])
            code, out, _ = self.invoke(["status", CODE], client)
            self.assertEqual(code, 0)
            self.assertIn(f"status: {status}", out)
            self.assertIn("left on the code", out)
            self.assertEqual(client.get_activation.call_count, 1)

    def test_not_found(self):
        client = make_client([SensieActivationNotFoundError(
            404, {"status": "error", "error": "code_not_found"})])
        code, _, err = self.invoke(["status", CODE], client)
        self.assertEqual(code, 1)
        self.assertIn("code_not_found", err)

    def test_missing_key_exits_78(self):
        client = make_client([act("pending")])
        code, _, err = self.invoke(["status", CODE], client, env={})
        self.assertEqual(code, EXIT_NO_KEY)
        client.get_activation.assert_not_called()


class TestApiErrors(LiveTestCase):

    def test_403_consent_required(self):
        client = make_client()
        client.request_activation_code.side_effect = SensieConsentRequiredError(
            403, {"status": "error", "error": "consent_required",
                  "message": "x"})
        code, out, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertIn("consent_required", err)
        self.assertIn("No activation code was issued", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(CODE, out)

    def test_429_rate_limited(self):
        client = make_client()
        client.request_activation_code.side_effect = SensieRateLimitedError(
            429, {"error": "rate_limited"}, {"Retry-After": "120"})
        code, _, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, 1)
        self.assertIn("rate_limited", err)
        self.assertIn("Retry after: 120 seconds", err)
        self.assertIn("sensie-eval status", err)
        self.assertNotIn("quota", err.lower())

    def test_429_quota_exceeded_still_exits_75(self):
        client = make_client()
        client.post_consent.side_effect = SensieQuotaError(
            429, {"error": "quota_exceeded", "used": 100, "limit": 100})
        code, _, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, EXIT_QUOTA)

    def test_401_exits_77(self):
        client = make_client()
        client.post_consent.side_effect = SensieAuthError(
            401, {"error": "unauthorized"})
        code, _, err = self.invoke(["run", "--live", "--yes"], client)
        self.assertEqual(code, EXIT_AUTH)
        client.request_activation_code.assert_not_called()

    def test_missing_key_exits_78_before_any_prompt(self):
        client = make_client()
        code, out, err = self.invoke(["run", "--live", "--yes"], client,
                                     env={})
        self.assertEqual(code, EXIT_NO_KEY)
        self.assertNotIn(CONSENT_COPY, out)
        client.post_consent.assert_not_called()

    def test_gone_already_claimed_is_not_expiry(self):
        client = make_client([SensieActivationGoneError(
            410, {"error": "code_already_claimed"})])
        code, _, err = self.invoke(["status", CODE], client)
        self.assertEqual(code, 1)
        self.assertIn("code_already_claimed", err)


class TestRoutingReportUnchanged(unittest.TestCase):

    def test_default_output_still_synthetic(self):
        out = io.StringIO()
        with redirect_stdout(out):
            print_routing_report([READ])
        self.assertIn("SYNTHETIC DEMO", out.getvalue())
        self.assertIn("1 annotator evaluated", out.getvalue())

    def test_live_true_never_labels_synthetic(self):
        out = io.StringIO()
        with redirect_stdout(out):
            print_routing_report([READ], live=True)
        self.assertNotIn("SYNTHETIC", out.getvalue())
        self.assertNotIn("annotator", out.getvalue())


if __name__ == "__main__":
    unittest.main()
