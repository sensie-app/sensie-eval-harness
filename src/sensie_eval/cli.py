"""
cli.py — `sensie-eval` command-line interface.

Commands:
    sensie-eval run           Offline synthetic evaluation (default; no network).
    sensie-eval run --api     Same evaluation, then posts scalar summary reads
                              to the Sensie live API and lists them back.
    sensie-eval run --live    Tier two: record consent, issue an activation
                              code, and wait for a real gesture done in the
                              SomaCheck app (see docs/live-mode.md).
    sensie-eval status CODE   One check on an activation code (resume path).
    sensie-eval version       Print the harness version.

Live mode environment:
    SENSIE_API_KEY   required — trial key (sk_sensie_<64 hex>), from
                     https://somabets.com/trial
    SENSIE_API_URL   optional — API base URL (default: production)

Exit codes (live mode):
    75  quota exhausted (HTTP 429)
    76  activation code expired — no result was produced
    77  authentication failed (HTTP 401)
    78  SENSIE_API_KEY not set

IP / PRIVACY GUARDRAIL: Live mode sends only scalar summary values per
read (whips, flowing, agreement). Raw accelerometer/gyroscope arrays
never leave the machine — the trial tier rejects raw motion.
"""

import argparse
import getpass
import hashlib
import os
import socket
import sys
import time
from datetime import datetime, timezone

from sensie_eval import __version__
from sensie_eval.api_client import (
    DEFAULT_API_URL,
    SensieActivationGoneError,
    SensieActivationNotFoundError,
    SensieApiClient,
    SensieApiError,
    SensieAuthError,
    SensieConsentRequiredError,
    SensieQuotaError,
    SensieRateLimitedError,
)
from sensie_eval.evaluate import (
    classify_subject,
    evaluate_subject_disjoint,
    load_dataset,
    print_report,
    subject_disjoint_split,
)
from sensie_eval.generate_synthetic_imu import generate_subject_dataset

EXIT_QUOTA = 75
EXIT_EXPIRED = 76
EXIT_AUTH = 77
EXIT_NO_KEY = 78

# Stable link that outlives this package's own release: it redirects to
# whatever's currently live (TestFlight today, the App Store once that
# listing publishes) via a single source of truth at go.somacheck.com, so
# this CTA never needs a new PyPI release to stay correct.
INSTALL_URL = "https://go.somacheck.com/install"
REAL_READ_CTA = (
    "Want to feel a real read? The same classifier runs our consumer app — "
    "calibrate yourself in ~10 min and check in on a real proposition. "
    f"Get the app: {INSTALL_URL}"
)
PILOT_CTA = "Pilot inquiries -> mike@joinsensie.com"

CONSENT_VERSION = "live-gesture-v1-draft"
# Retention period and deletion route are policy decisions this code does not
# make. Counsel/founder must replace this placeholder before release.
NEEDS_POLICY_APPROVAL = (
    "[retention/deletion terms pending Sensie policy approval]"
)
CONSENT_COPY = f"""\
Live gesture: what you are agreeing to
{"-" * 40}
This step uses a real gesture, done on a phone in the SomaCheck app.

What is captured and shared
  Raw motion stays on the phone and is never sent anywhere. The only things
  shared are three values the app derives from your gesture: whips, flowing
  and agreement. The researcher running this command receives those three
  values and nothing else.

Your choices
  You can stop at any time, before or during the gesture.
  Saying yes here records your consent with Sensie first; only then is an
  activation code issued. No code exists without it.

How long the values are kept, and how to have them deleted
  {NEEDS_POLICY_APPROVAL}

Consent version: {CONSENT_VERSION}
"""

# The activation code's lifetime, from the API contract (C4).
CODE_TTL_MINUTES = 30
DEFAULT_POLL_INTERVAL = 10.0
DEFAULT_TIMEOUT_MINUTES = 30.0
EXIT_INTERRUPTED = 130
# Consecutive network failures tolerated while polling before giving up.
MAX_POLL_NETWORK_FAILURES = 6

STATUS_LINES = {
    "pending": "waiting for the code to be entered in the SomaCheck app",
    "claimed": "code entered in the app — calibration and gesture in progress",
}


def default_user_id() -> str:
    """Stable, non-identifying user id for this machine/user pair."""
    raw = f"{getpass.getuser()}@{socket.gethostname()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"eval-{digest}"


def derive_reads(subjects, n_reads: int, threshold: float):
    """
    Derive scalar read payloads from the synthetic subjects' summary stats.

    Per read (cycling through held-out subjects):
      whips     — triple-whip template (3 whips) scaled by the subject's
                  measured signal reliability, rounded to the nearest
                  integer (the API stores whips as an integer count).
      flowing   — 1 if reliability >= threshold, else -1.
      agreement — 2 if prediction and ground truth agree on high-signal,
                  1 if they agree on low-signal, -1 on disagreement.

    Only these three scalars are ever sent — no raw IMU arrays.
    """
    reads = []
    for i in range(n_reads):
        subject = subjects[i % len(subjects)]
        predicted, reliability = classify_subject(subject, threshold=threshold)
        ground_truth = 1 if subject["signal_quality"] >= 0.6 else 0
        if predicted == ground_truth:
            agreement = 2 if predicted == 1 else 1
        else:
            agreement = -1
        reads.append({
            "whips": int(round(3 * reliability)),
            "flowing": 1 if predicted == 1 else -1,
            "agreement": agreement,
        })
    return reads


def print_live_report(read):
    """Render one real, self-read gesture. Deliberately not the routing
    report: no clear/calibrating verdict, since nothing here is an annotator
    being routed and the reading does not evaluate or gate the person."""
    print("\nYour live read")
    print("-" * 40)
    print("Real gesture, done on your phone in the SomaCheck app — "
          "not synthetic data.")
    print(f"  whips:     {read['whips']}")
    print(f"  flowing:   {read['flowing']}")
    print(f"  agreement: {read['agreement']}")
    print("Only these three values left your phone; raw motion did not.")


def print_routing_report(reads, live=False):
    """Render a synthetic cohort-routing preview from the posted scalars.

    live=True renders one real read via print_live_report instead — the
    synthetic-demo wording never applies to a real gesture.
    """
    if live:
        print_live_report(reads[0])
        return
    clear = sum(
        read["whips"] >= 2
        and read["flowing"] == 1
        and read["agreement"] == 2
        for read in reads
    )
    calibrating = len(reads) - clear
    annotator_word = "annotator" if len(reads) == 1 else "annotators"
    read_word = "read" if clear == 1 else "reads"
    calibrating_word = "annotator" if calibrating == 1 else "annotators"

    print("\nWhat you'd get: routing report")
    print("-" * 40)
    print("SYNTHETIC DEMO — real deployments render this from your "
          "annotator cohort")
    print(f"{len(reads)} {annotator_word} evaluated -> "
          f"{clear} read clearly, {calibrating} still calibrating")
    print("Demo rule: whips >= 2, flowing = 1, and agreement = 2 "
          "routes as clear.")
    print(f"Route accordingly: use {clear} clear {read_word}; keep "
          f"{calibrating} {calibrating_word} in calibration.")


def run_offline(args):
    """Offline synthetic evaluation — unchanged from the original harness."""
    if args.data:
        subjects = load_dataset(args.data)
        print(f"Loaded {len(subjects)} subjects from {args.data}")
    else:
        subjects = generate_subject_dataset(
            n_subjects=args.n_subjects,
            n_repetitions=args.n_repetitions,
            duration=args.duration,
            sample_rate=100.0,
            base_noise=args.noise,
            seed=args.seed,
        )
        print(f"Generated {len(subjects)} synthetic subjects "
              f"(seed={args.seed}, noise={args.noise})")

    results = evaluate_subject_disjoint(
        subjects, train_frac=args.train_frac, seed=args.seed
    )
    results["reliability_threshold"] = args.threshold
    print_report(results)
    return subjects


def _client_from_env():
    """Build the API client from SENSIE_API_KEY / SENSIE_API_URL, or return
    EXIT_NO_KEY (after printing how to get a key) when the key is unset."""
    api_key = os.environ.get("SENSIE_API_KEY")
    if not api_key:
        print("Error: SENSIE_API_KEY is not set.", file=sys.stderr)
        print("Get a trial key at https://somabets.com/trial and run:",
              file=sys.stderr)
        print("  export SENSIE_API_KEY=sk_sensie_...", file=sys.stderr)
        return EXIT_NO_KEY
    base_url = os.environ.get("SENSIE_API_URL", DEFAULT_API_URL)
    return SensieApiClient(api_key=api_key, base_url=base_url)


def api_preflight(args):
    """Verify the API key up front with one unmetered call (session create),
    so a bad key fails in one round-trip instead of after the offline eval.
    The session is reused by run_api — no extra session, no metered calls.

    Returns (client, session_id, user_id), or an int exit code on failure.
    """
    client = _client_from_env()
    if isinstance(client, int):
        return client
    user_id = args.user_id or default_user_id()

    try:
        session = client.create_session(user_id, sdk_version=__version__)
    except SensieQuotaError as exc:
        return _quota_error_exit(exc)
    except SensieAuthError:
        return _auth_error_exit()
    except SensieApiError as exc:
        print(f"\nAPI error (HTTP {exc.status}): {exc.body}", file=sys.stderr)
        return 1

    session_id = session["id"]
    print(f"\nSession created: id={session_id} (user_id={user_id}) "
          f"— API key verified")
    return client, session_id, user_id


def _quota_error_exit(exc):
    print("\nTrial quota exhausted (HTTP 429).", file=sys.stderr)
    used = exc.used if exc.used is not None else "?"
    limit = exc.limit if exc.limit is not None else "?"
    print(f"  Used {used} of {limit} reads in the current rolling "
          f"7-day window.", file=sys.stderr)
    if exc.window_reset_at:
        print(f"  Window resets at: {exc.window_reset_at}",
              file=sys.stderr)
    if exc.retry_after:
        print(f"  Retry after: {exc.retry_after} seconds",
              file=sys.stderr)
    print("  Each posted read counts against the trial quota. See "
          "https://github.com/sensie-app/sensie-eval-harness/blob/main/docs/quota-limits.md",
          file=sys.stderr)
    return EXIT_QUOTA


def _auth_error_exit():
    print("\nAuthentication failed (HTTP 401).", file=sys.stderr)
    print("  Check SENSIE_API_KEY — it should look like "
          "sk_sensie_<64 hex characters>.", file=sys.stderr)
    print("  Keys are shown once at issuance. If yours is lost, see "
          "https://github.com/sensie-app/sensie-eval-harness/blob/main/docs/troubleshooting.md",
          file=sys.stderr)
    return EXIT_AUTH


def run_api(args, subjects, client, session_id, user_id):
    """Live mode: post reads to the preflight session, list them back."""
    # Post reads for held-out (test) subjects — mirrors the offline protocol.
    _, test_subjects = subject_disjoint_split(
        subjects, train_frac=args.train_frac, seed=args.seed
    )
    reads = derive_reads(test_subjects or subjects, args.reads, args.threshold)

    try:
        posted = 0
        for read in reads:
            client.post_sensie(
                session_id,
                whips=read["whips"],
                flowing=read["flowing"],
                agreement=read["agreement"],
            )
            posted += 1

        sensies = client.list_sensies(session_id)

        print("\nLive API summary")
        print("-" * 40)
        print(f"  Session id:      {session_id}")
        print(f"  Reads posted:    {posted}")
        print(f"  Reads returned:  {len(sensies)}")
        print("  Quota remaining: not reported on success "
              "(the API reports used/limit on HTTP 429)")
        print("-" * 40)
        print_routing_report(reads)
        print(REAL_READ_CTA)
        print(PILOT_CTA)
        return 0

    except SensieQuotaError as exc:
        return _quota_error_exit(exc)

    except SensieAuthError:
        return _auth_error_exit()

    except SensieApiError as exc:
        print(f"\nAPI error (HTTP {exc.status}): {exc.body}", file=sys.stderr)
        return 1


def _seconds_remaining(expires_at):
    """Whole seconds until `expires_at` (ISO 8601), or None if unparseable."""
    if not expires_at:
        return None
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return max(0, int((expiry - datetime.now(timezone.utc)).total_seconds()))


def _format_remaining(expires_at):
    seconds = _seconds_remaining(expires_at)
    if not seconds:  # unparseable, or local clock says lapsed: say nothing
        return ""
    minutes, secs = divmod(seconds, 60)
    return f" ({minutes}m {secs:02d}s left on the code)"


def _resume_hint(code, expires_at=None):
    print(f"Run `sensie-eval status {code}` to check back later.")
    left = _format_remaining(expires_at)
    if left:
        print(f"The code is still valid{left}.")


def _network_error_exit(exc):
    print(f"\nCould not reach the Sensie API: {exc}", file=sys.stderr)
    return 1


def _live_error_exit(exc):
    """Map an API error from the live path to a plain message + exit code."""
    if isinstance(exc, SensieQuotaError):
        return _quota_error_exit(exc)
    if isinstance(exc, SensieAuthError):
        return _auth_error_exit()
    if isinstance(exc, SensieConsentRequiredError):
        print("\nThe API did not accept the consent record "
              "(HTTP 403, consent_required).", file=sys.stderr)
        print("  No activation code was issued. Run `sensie-eval run --live` "
              "again to give consent afresh.", file=sys.stderr)
        return 1
    if isinstance(exc, SensieRateLimitedError):
        print("\nToo many activation codes are already waiting on this key "
              "(HTTP 429, rate_limited).", file=sys.stderr)
        print("  Finish one, or let one expire, then try again. Check an "
              "existing code with `sensie-eval status <code>`.",
              file=sys.stderr)
        if exc.retry_after:
            print(f"  Retry after: {exc.retry_after} seconds", file=sys.stderr)
        return 1
    if isinstance(exc, SensieActivationNotFoundError):
        print("\nNo activation code with that value was found for this key "
              "(HTTP 404, code_not_found).", file=sys.stderr)
        print("  Check the 8 characters, and that SENSIE_API_KEY is the key "
              "that issued the code.", file=sys.stderr)
        return 1
    if isinstance(exc, SensieActivationGoneError):
        if exc.reason == "code_expired":
            return _expired_exit()
        print(f"\nThat code can no longer be used (HTTP 410, {exc.reason}).",
              file=sys.stderr)
        return 1
    print(f"\nAPI error (HTTP {exc.status}): {exc.body}", file=sys.stderr)
    return 1


def _expired_exit():
    print("\nThis activation code has expired.", file=sys.stderr)
    print("  No result was produced, and nothing is stored beyond the "
          "expired code.", file=sys.stderr)
    print("  To try again, run `sensie-eval run --live` for a new code.",
          file=sys.stderr)
    return EXIT_EXPIRED


def _report_activation(code, activation):
    """Handle a terminal activation status. Returns the exit code for
    completed/expired, or None while the code is still pending/claimed."""
    status = activation.get("status")
    if status == "completed":
        sensie = activation.get("sensie")
        if not sensie:
            print("\nThe API reported the code as completed but returned no "
                  "values.", file=sys.stderr)
            return 1
        print_routing_report([sensie], live=True)
        return 0
    if status == "expired":
        return _expired_exit()
    return None


def _status_line(status, expires_at):
    detail = STATUS_LINES.get(status, "")
    return f"  status: {status}" + (f" — {detail}" if detail else "") \
        + _format_remaining(expires_at)


def wait_for_activation(client, code, interval, timeout_minutes,
                        expires_at=None):
    """Poll until the code completes or expires; returns an exit code.

    On --timeout the code is still valid, so this prints the resume hint and
    returns 0. A status line prints on every change and at least once a
    minute, so a long wait never looks hung.
    """
    deadline = time.monotonic() + timeout_minutes * 60
    last_status, last_print = None, None
    failures = 0
    while True:
        try:
            activation = client.get_activation(code)
            failures = 0
        except SensieApiError as exc:
            return _live_error_exit(exc)
        except OSError as exc:
            failures += 1
            if failures >= MAX_POLL_NETWORK_FAILURES:
                _network_error_exit(exc)
                _resume_hint(code, expires_at)
                return 1
            activation = None
        if activation is not None:
            result = _report_activation(code, activation)
            if result is not None:
                return result
            status = activation.get("status")
            expires_at = activation.get("expires_at") or expires_at
            now = time.monotonic()
            if last_print is None or status != last_status \
                    or now - last_print >= 60:
                print(_status_line(status, expires_at), flush=True)
                last_status, last_print = status, now
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"\nStopped waiting after {timeout_minutes:g} minutes.")
            _resume_hint(code, expires_at)
            return 0
        time.sleep(min(interval, remaining))


def run_live(args):
    """Tier two: consent -> activation code -> wait for a real gesture."""
    if args.api:
        print("Error: --live and --api are separate paths; pick one.",
              file=sys.stderr)
        return 2
    if args.poll_interval <= 0 or args.timeout <= 0:
        print("Error: --poll-interval and --timeout must be positive.",
              file=sys.stderr)
        return 2
    client = _client_from_env()
    if isinstance(client, int):
        return client

    # Consent is collected here, before any code exists.
    print(CONSENT_COPY)
    if not args.yes:
        if not sys.stdin.isatty():
            print("Error: consent needs a person at the keyboard. Re-run in "
                  "an interactive terminal, or pass --yes once the person "
                  "doing the gesture has read the consent text above.",
                  file=sys.stderr)
            return 2
        try:
            answer = input("Do you consent to this? [y/N] ")
        except EOFError:
            answer = ""
        except KeyboardInterrupt:
            print("\nNo consent recorded, nothing was sent.")
            return EXIT_INTERRUPTED
        if answer.strip().lower() not in ("y", "yes"):
            print("No consent recorded, nothing was sent.")
            return 1

    try:
        consent = client.post_consent(CONSENT_VERSION)
        activation = client.request_activation_code(consent["id"])
    except SensieApiError as exc:
        return _live_error_exit(exc)
    except OSError as exc:
        return _network_error_exit(exc)
    except KeyboardInterrupt:
        print("\nInterrupted before a code was shown.")
        return EXIT_INTERRUPTED

    code = activation["code"]
    expires_at = activation.get("expires_at")
    print(f"""
Live gesture: tier two
{"-" * 40}
You already ran the offline demo. This one uses a real gesture, so budget
the time: about 15-20 minutes including calibration. The code is valid for
{CODE_TTL_MINUTES} minutes.

  1. Install SomaCheck:  {INSTALL_URL}
  2. Enter this code:    {code}
     or open this link:  somacheck://activate/{code}
  3. Do the gesture in the app. The result shows up here.

You are about to hand over a real gesture. Only the three derived values
(whips, flowing, agreement) come back; raw motion stays on the phone.
Press Ctrl-C to stop waiting at any time; the code keeps working until it
expires.
""", flush=True)
    try:
        return wait_for_activation(client, code, args.poll_interval,
                                   args.timeout, expires_at)
    except KeyboardInterrupt:
        print("\nStopped waiting.")
        _resume_hint(code, expires_at)
        return EXIT_INTERRUPTED


def run_status(args):
    """One check on an activation code (the resume path)."""
    client = _client_from_env()
    if isinstance(client, int):
        return client
    code = args.code.strip().upper()
    try:
        activation = client.get_activation(code)
    except SensieApiError as exc:
        return _live_error_exit(exc)
    except OSError as exc:
        return _network_error_exit(exc)
    result = _report_activation(code, activation)
    if result is not None:
        return result
    print(_status_line(activation.get("status"), activation.get("expires_at")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sensie-eval",
        description="Subject-disjoint evaluation harness for motion-biomarker "
                    "classification, with optional live-API mode.",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the evaluation (offline by default)")
    run.add_argument("--data", type=str, default=None,
                     help="Evaluate an existing .npz dataset instead of "
                          "generating one")
    run.add_argument("--n-subjects", type=int, default=50,
                     help="Number of synthetic subjects (default: 50)")
    run.add_argument("--n-repetitions", type=int, default=5,
                     help="Repetitions per subject (default: 5)")
    run.add_argument("--duration", type=float, default=4.0,
                     help="Gesture duration in seconds (default: 4.0)")
    run.add_argument("--noise", type=float, default=0.15,
                     help="Base noise level (default: 0.15)")
    run.add_argument("--train-frac", type=float, default=0.7,
                     help="Fraction of subjects for calibration (default: 0.7)")
    run.add_argument("--seed", type=int, default=42,
                     help="Random seed (default: 42)")
    run.add_argument("--threshold", type=float, default=0.5,
                     help="Reliability threshold (default: 0.5)")
    run.add_argument("--api", action="store_true",
                     help="After the offline run, post summary reads to the "
                          "Sensie live API (requires SENSIE_API_KEY)")
    run.add_argument("--reads", type=int, default=5,
                     help="Number of reads to post in --api mode (default: 5)")
    run.add_argument("--user-id", type=str, default=None,
                     help="User id for the API session (default: stable "
                          "auto-generated id for this machine)")

    run.add_argument("--live", action="store_true",
                     help="Tier two: record consent, get an activation code, "
                          "and wait for a real gesture done in the SomaCheck "
                          "app (requires SENSIE_API_KEY)")
    run.add_argument("--yes", action="store_true",
                     help="With --live: confirm consent non-interactively "
                          "(the consent text is still printed)")
    run.add_argument("--poll-interval", type=float,
                     default=DEFAULT_POLL_INTERVAL, metavar="SECONDS",
                     help="With --live: seconds between status checks "
                          "(default: 10)")
    run.add_argument("--timeout", type=float,
                     default=DEFAULT_TIMEOUT_MINUTES, metavar="MINUTES",
                     help="With --live: stop waiting after this many minutes; "
                          "the code stays valid and `status` resumes "
                          "(default: 30)")

    status = sub.add_parser(
        "status", help="Check an activation code from `run --live`")
    status.add_argument("code", help="The 8-character activation code")

    sub.add_parser("version", help="Print the harness version")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"sensie-eval {__version__}")
        return 0

    if args.command == "status":
        return run_status(args)

    if args.command == "run":
        if args.live:
            return run_live(args)
        api_ctx = None
        if args.api:
            api_ctx = api_preflight(args)
            if isinstance(api_ctx, int):
                return api_ctx
        subjects = run_offline(args)
        if args.api:
            return run_api(args, subjects, *api_ctx)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
