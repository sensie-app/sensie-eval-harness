# Changelog

All notable changes to `sensie-eval` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.2] — 2026-07-13

- `run --api` now prints a clearly-labeled synthetic routing report after
  posting reads, previewing what a real cohort-routing report looks like.
- README rewritten for an engineer-first audience: runnable quickstart up
  top, honest what's-in-the-harness table, commercial pilot details moved
  to the bottom.

## [0.1.1] — 2026-07-12

- Added `run --api` live mode: posts scalar summary reads to the Sensie
  trial API and lists them back (`src/sensie_eval/api_client.py`).
- CLI verifies the trial key up front (unmetered session-create call) so a
  bad key fails in one round-trip instead of after the offline eval.
- Structured exit codes for scripting: `75` (quota), `77` (auth), `78`
  (no key set).
- Added `docs/quota-limits.md` and `docs/troubleshooting.md`; error copy
  in the CLI links to both.
- `docs/quickstart.md` gained a macOS-first `pipx` install path (Homebrew
  Python blocks bare `pip install`).
- Added `tests/run_doc_tests.sh`, which executes every fenced `bash` block
  in the README and `docs/*.md` — the documented commands are verified in
  CI, not just written.
- Added the tag-triggered `Release to PyPI` GitHub Actions workflow, with
  a guard that the git tag matches `pyproject.toml`'s version.

## [0.1.0] — 2026-07-03

- Initial PyPI release. PEP 621 packaging (`src/` layout, `sensie-eval`
  console script), `numpy`/`scipy`-only dependencies.
- Synthetic IMU generator (`generate_synthetic_imu.py`) and subject-disjoint
  evaluation (`evaluate.py`): per-subject signal reliability, classification
  accuracy on held-out subjects, and Mann-Whitney U routing-validity gap.
- GitHub Actions CI (unit tests + doc tests + build/twine check) on
  Python 3.11 and 3.12.
