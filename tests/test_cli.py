"""
test_cli.py — Unit tests for the sensie-eval CLI.

No network: live-API paths are exercised with mocked clients.

Tests cover:
  1. `version` command
  2. Offline `run` (small dataset, exit 0)
  3. `run --api` without SENSIE_API_KEY → exit 78
  4. `run --api` quota exhausted → exit 75, friendly message
  5. `run --api` auth failure → exit 77
  6. Derived read payloads respect the API contract
  7. Stable default user id
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sensie_eval import __version__
from sensie_eval.api_client import (
    SensieAuthError,
    SensieQuotaError,
    VALID_AGREEMENT,
    VALID_FLOWING,
)
from sensie_eval.cli import (
    EXIT_AUTH,
    EXIT_NO_KEY,
    EXIT_QUOTA,
    default_user_id,
    derive_reads,
    main,
)
from sensie_eval.generate_synthetic_imu import generate_subject_dataset

OFFLINE_ARGS = ["run", "--n-subjects", "10", "--n-repetitions", "2",
                "--duration", "1.0"]


class TestVersion(unittest.TestCase):

    def test_version_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), f"sensie-eval {__version__}")


class TestOfflineRun(unittest.TestCase):

    def test_offline_run_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(OFFLINE_ARGS)
        self.assertEqual(code, 0)
        self.assertIn("SUBJECT-DISJOINT EVALUATION REPORT", out.getvalue())


class TestApiMode(unittest.TestCase):

    def _run(self, env, client=None):
        args = OFFLINE_ARGS + ["--api", "--reads", "2"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("SENSIE_API_KEY", "SENSIE_API_URL"):
                if var not in env:
                    os.environ.pop(var, None)
            patcher = mock.patch(
                "sensie_eval.cli.SensieApiClient", return_value=client
            ) if client else None
            if patcher:
                patcher.start()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(args)
            finally:
                if patcher:
                    patcher.stop()
        return code, out.getvalue(), err.getvalue()

    def test_missing_key_exits_78(self):
        code, _, err = self._run(env={})
        self.assertEqual(code, EXIT_NO_KEY)
        self.assertIn("SENSIE_API_KEY", err)
        self.assertIn("somabets.com/trial", err)

    def test_successful_run(self):
        client = mock.MagicMock()
        client.create_session.return_value = {"id": 42}
        client.post_sensie.return_value = {"id": 1}
        client.list_sensies.return_value = [{"id": 1}, {"id": 2}]
        code, out, _ = self._run(
            env={"SENSIE_API_KEY": "sk_sensie_" + "a" * 64}, client=client
        )
        self.assertEqual(code, 0)
        self.assertIn("Session created: id=42", out)
        self.assertIn("Reads posted:    2", out)
        self.assertIn("Reads returned:  2", out)
        self.assertEqual(client.post_sensie.call_count, 2)
        # sdkVersion is the harness version
        _, kwargs = client.create_session.call_args
        self.assertEqual(kwargs.get("sdk_version"), __version__)

    def test_quota_exceeded_exits_75(self):
        client = mock.MagicMock()
        client.create_session.return_value = {"id": 42}
        client.post_sensie.side_effect = SensieQuotaError(
            429,
            {"error": "quota_exceeded", "used": 100, "limit": 100,
             "window_reset_at": "2026-07-09T14:00:00Z"},
            {"Retry-After": "3600"},
        )
        code, _, err = self._run(
            env={"SENSIE_API_KEY": "sk_sensie_" + "a" * 64}, client=client
        )
        self.assertEqual(code, EXIT_QUOTA)
        self.assertIn("100 of 100", err)
        self.assertIn("2026-07-09T14:00:00Z", err)
        self.assertIn("3600", err)
        self.assertNotIn("Traceback", err)

    def test_auth_failure_exits_77(self):
        client = mock.MagicMock()
        client.create_session.side_effect = SensieAuthError(
            401, {"status": "fail", "message": "Invalid API key."}
        )
        code, _, err = self._run(
            env={"SENSIE_API_KEY": "sk_sensie_bad"}, client=client
        )
        self.assertEqual(code, EXIT_AUTH)
        self.assertIn("SENSIE_API_KEY", err)
        self.assertNotIn("Traceback", err)


class TestDerivedReads(unittest.TestCase):

    def test_reads_respect_contract(self):
        subjects = generate_subject_dataset(
            n_subjects=5, n_repetitions=2, duration=1.0, seed=7
        )
        reads = derive_reads(subjects, n_reads=8, threshold=0.5)
        self.assertEqual(len(reads), 8)
        for read in reads:
            # Scalars only, exactly the contract fields
            self.assertEqual(set(read.keys()),
                             {"whips", "flowing", "agreement"})
            self.assertIsInstance(read["whips"], int)
            self.assertIn(read["flowing"], VALID_FLOWING)
            self.assertIn(read["agreement"], VALID_AGREEMENT)


class TestDefaultUserId(unittest.TestCase):

    def test_stable_and_prefixed(self):
        uid1 = default_user_id()
        uid2 = default_user_id()
        self.assertEqual(uid1, uid2)
        self.assertTrue(uid1.startswith("eval-"))
        self.assertEqual(len(uid1), len("eval-") + 12)


if __name__ == "__main__":
    unittest.main()
