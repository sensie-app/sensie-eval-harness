"""Command-line interface for sensie-eval.

Subcommands:
  generate  Generate a synthetic IMU dataset (.npz)
  run       Run the subject-disjoint evaluation on a dataset

Everything runs locally and offline — no API key, no network calls.
"""

import argparse
import json
import sys
from typing import Optional

from sensie_eval import __version__
from sensie_eval.evaluate import (
    build_json_output,
    evaluate_subject_disjoint,
    load_dataset,
    print_report,
)
from sensie_eval.generate import (
    generate_subject_dataset,
    print_generation_summary,
    save_dataset,
)


def _sample_data_path() -> str:
    """Return the filesystem path of the bundled sample dataset."""
    if sys.version_info >= (3, 9):
        from importlib.resources import files

        return str(files("sensie_eval").joinpath("data", "sample_dataset.npz"))
    raise RuntimeError("Python >= 3.9 required")


def _cmd_generate(args: argparse.Namespace) -> int:
    dataset = generate_subject_dataset(
        n_subjects=args.n_subjects,
        n_repetitions=args.n_repetitions,
        duration=args.duration,
        sample_rate=100.0,
        base_noise=args.noise,
        seed=args.seed,
    )
    print_generation_summary(dataset, duration=args.duration, n_repetitions=args.n_repetitions)

    if args.output:
        save_dataset(dataset, args.output)
        print(f"\nSaved dataset to {args.output}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.sample and args.data:
        print("error: pass either --data or --sample, not both", file=sys.stderr)
        return 2
    if not args.sample and not args.data:
        print("error: one of --data or --sample is required", file=sys.stderr)
        return 2

    data_path = _sample_data_path() if args.sample else args.data
    data_source = "bundled-sample" if args.sample else args.data

    subjects = load_dataset(data_path)
    print(f"Loaded {len(subjects)} subjects from {data_source}")

    results = evaluate_subject_disjoint(
        subjects,
        train_frac=args.train_frac,
        seed=args.seed,
        threshold=args.threshold,
    )

    print_report(results)

    if args.output_json:
        document = build_json_output(
            results,
            config={
                "data_source": data_source,
                "train_frac": args.train_frac,
                "seed": args.seed,
                "threshold": args.threshold,
            },
        )
        with open(args.output_json, "w") as f:
            json.dump(document, f, indent=2)
            f.write("\n")
        print(f"\nWrote machine-readable results to {args.output_json}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sensie-eval",
        description=(
            "Subject-disjoint evaluation harness for motion-biomarker "
            "classification. Runs fully offline on synthetic or local data."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_gen = subparsers.add_parser(
        "generate",
        help="Generate a synthetic IMU dataset (.npz)",
    )
    p_gen.add_argument(
        "--n-subjects", type=int, default=50, help="Number of synthetic subjects (default: 50)"
    )
    p_gen.add_argument(
        "--n-repetitions", type=int, default=5, help="Repetitions per subject (default: 5)"
    )
    p_gen.add_argument(
        "--duration", type=float, default=4.0, help="Gesture duration in seconds (default: 4.0)"
    )
    p_gen.add_argument("--noise", type=float, default=0.15, help="Base noise level (default: 0.15)")
    p_gen.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p_gen.add_argument(
        "--output", type=str, default=None, help="Output .npz path (default: print summary only)"
    )
    p_gen.set_defaults(func=_cmd_generate)

    p_run = subparsers.add_parser(
        "run",
        help="Run the subject-disjoint evaluation on a dataset",
    )
    p_run.add_argument("--data", type=str, default=None, help="Path to a dataset .npz file")
    p_run.add_argument(
        "--sample",
        action="store_true",
        help="Use the bundled anonymized sample dataset (fully offline)",
    )
    p_run.add_argument(
        "--train-frac",
        type=float,
        default=0.7,
        help="Fraction of subjects for calibration (default: 0.7)",
    )
    p_run.add_argument(
        "--seed", type=int, default=42, help="Random seed for train/test split (default: 42)"
    )
    p_run.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Reliability threshold for classification (default: 0.5)",
    )
    p_run.add_argument(
        "--output-json",
        type=str,
        default=None,
        metavar="PATH",
        help="Write machine-readable results (conforms to schemas/output.schema.json)",
    )
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
