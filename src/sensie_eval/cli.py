"""
cli.py — `sensie-eval` command-line interface.

Commands:
    sensie-eval run           Offline synthetic evaluation (default; no network).
    sensie-eval run --api     Same evaluation, then posts scalar summary reads
                              to the Sensie live API and lists them back.
    sensie-eval version       Print the harness version.

Live mode environment:
    SENSIE_API_KEY   required — trial key (sk_sensie_<64 hex>), from
                     https://somabets.com/trial
    SENSIE_API_URL   optional — API base URL (default: production)

Exit codes (live mode):
    75  quota exhausted (HTTP 429)
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

from sensie_eval import __version__
from sensie_eval.api_client import (
    DEFAULT_API_URL,
    SensieApiClient,
    SensieApiError,
    SensieAuthError,
    SensieQuotaError,
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
EXIT_AUTH = 77
EXIT_NO_KEY = 78


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
                  measured signal reliability, rounded to 2 decimals.
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
            "whips": round(3 * reliability, 2),
            "flowing": 1 if predicted == 1 else -1,
            "agreement": agreement,
        })
    return reads


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


def run_api(args, subjects):
    """Live mode: create session, post reads, list them back."""
    api_key = os.environ.get("SENSIE_API_KEY")
    if not api_key:
        print("Error: SENSIE_API_KEY is not set.", file=sys.stderr)
        print("Get a trial key at https://somabets.com/trial and run:",
              file=sys.stderr)
        print("  export SENSIE_API_KEY=sk_sensie_...", file=sys.stderr)
        return EXIT_NO_KEY

    base_url = os.environ.get("SENSIE_API_URL", DEFAULT_API_URL)
    user_id = args.user_id or default_user_id()
    client = SensieApiClient(api_key=api_key, base_url=base_url)

    # Post reads for held-out (test) subjects — mirrors the offline protocol.
    _, test_subjects = subject_disjoint_split(
        subjects, train_frac=args.train_frac, seed=args.seed
    )
    reads = derive_reads(test_subjects or subjects, args.reads, args.threshold)

    try:
        session = client.create_session(user_id, sdk_version=__version__)
        session_id = session["id"]
        print(f"\nSession created: id={session_id} (user_id={user_id})")

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
        print(f"  Quota remaining: not reported on success "
              f"(the API reports used/limit on HTTP 429)")
        print("-" * 40)
        return 0

    except SensieQuotaError as exc:
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
        print("  Each posted read counts against the trial quota. "
              "See docs/quota-limits.md.", file=sys.stderr)
        return EXIT_QUOTA

    except SensieAuthError:
        print("\nAuthentication failed (HTTP 401).", file=sys.stderr)
        print("  Check SENSIE_API_KEY — it should look like "
              "sk_sensie_<64 hex characters>.", file=sys.stderr)
        print("  Keys are shown once at issuance. If yours is lost, see "
              "docs/troubleshooting.md.", file=sys.stderr)
        return EXIT_AUTH

    except SensieApiError as exc:
        print(f"\nAPI error (HTTP {exc.status}): {exc.body}", file=sys.stderr)
        return 1


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

    sub.add_parser("version", help="Print the harness version")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(f"sensie-eval {__version__}")
        return 0

    if args.command == "run":
        subjects = run_offline(args)
        if args.api:
            return run_api(args, subjects)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
