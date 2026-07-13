# sensie-eval

Sensie verifies the human behind AI training and eval data — a 3-second phone gesture yields an involuntary motion biomarker; the API returns whether the rater's judgment is genuinely congruent with the task. **This repo is the evaluation harness**: a subject-disjoint scaffold that lets RLHF and human-data engineers benchmark the routing methodology against synthetic IMU data they control, before touching real annotators or a paid pilot.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/sensie-eval.svg)](https://pypi.org/project/sensie-eval/)
[![Release](https://img.shields.io/github/v/tag/sensie-app/sensie-eval-harness?label=release)](https://github.com/sensie-app/sensie-eval-harness/releases)

## Quickstart

Requires Python ≥ 3.10. numpy + scipy only.

```bash
pip install sensie-eval
sensie-eval run
```

That generates 50 synthetic subjects at 100 Hz (3-axis accel + gyro), splits them 70/30 subject-disjoint, scores per-subject signal reliability on the held-out cohort, and prints an evaluation report ending in a PASS/FAIL banner against pre-registered thresholds.

Common flags:

```bash
sensie-eval run --n-subjects 100 --seed 7      # bigger cohort, different seed
sensie-eval run --noise 0.5                    # more noise → easier routing decision (intentional)
sensie-eval run --train-frac 0.8               # different calibration split
```

macOS with Homebrew Python blocks bare `pip install`; use `pipx install sensie-eval` instead. Full CLI reference and live-API mode: [docs/quickstart.md](docs/quickstart.md).

### Running tests

```bash
pip install pytest && pytest -q
```

## What's in the harness

| Path | Role |
|---|---|
| `src/sensie_eval/generate_synthetic_imu.py` | Synthesizes 100 Hz 3-axis accel + gyro streams. Each subject has a latent `signal_quality ∈ [0, 1]` controlling separability; noise is per-subject and reproducible. |
| `src/sensie_eval/evaluate.py` | Subject-disjoint train/test split, cross-repetition reliability scoring, classification metrics (accuracy / precision / recall / F1), and Mann-Whitney U on the high-vs-low-signal cohort gap ("routing validity"). |
| `src/sensie_eval/cli.py` | The `sensie-eval` command. `run` executes the offline pipeline; `run --api` posts scalar summary reads to the live trial API (raw motion never leaves the machine). |
| `src/sensie_eval/api_client.py` | Thin client for the Sensie trial API — session create, post read, list reads, structured 401/429. |
| `tests/` | 38 unit + integration tests: generation, reliability scoring, split integrity, save/load roundtrip, end-to-end pipeline, and CLI behavior. |
| `docs/` | [quickstart](docs/quickstart.md), [api-reference](docs/api-reference.md), [quota-limits](docs/quota-limits.md), [troubleshooting](docs/troubleshooting.md). |

The PASS/FAIL banner is a **methodology demo**, not a performance claim: it applies pre-registered thresholds to synthetic data with configurable noise, and reports honestly — at default noise the accuracy threshold typically **fails**, which is the harness working as intended. Details in `docs/quickstart.md`.

## Methodology & evidence

- Subject-disjoint by construction: no synthetic subject appears in both train and test — this mirrors real deployment, where Sensie calibrates per-user and routes new users through a pre-screen.
- Two metrics map to the two pilot endpoints: (1) classification accuracy on held-out subjects (noise-reduction proxy); (2) Mann-Whitney U gap between high-signal and low-signal cohorts (routing validity).
- Evidence base for the underlying biomarker (not this harness): 9 PhD-led research trials across 18,000+ sessions; 83.6% state-classification accuracy on calibrated users (personalized, post-calibration — accuracy is person-variable and rises with calibration, which is exactly why the routing/pre-screen layer exists); two granted US utility patents plus one additional filing; trials led and advised by researchers affiliated with Harvard Medical School and Oxford (DPhil).

**What this repo is not.** No model weights, no training data, no feature definitions, no CFD feature families. All classification logic in the harness is a placeholder threshold on cross-repetition correlation; the production classifier runs server-side at Sensie. This harness lets you validate the *methodology* — subject-disjoint evaluation, signal reliability scoring, routing validity — against data you control.

## Commercial pilots

Once the methodology checks out, the next step is a fixed-scope, pre-registered pilot on Sensie's managed pre-screened panel: 4–6 weeks, $25,000 fixed, with a primary endpoint that filtering to verified reads reduces label disagreement/error by ≥30% relative on gold-anchored tasks (secondary endpoint: pre-agreed high-vs-low cohort gap). Thresholds are negotiated and locked before data collection. Contact **mike@joinsensie.com**.

## License & citation

Apache 2.0 — see [LICENSE](LICENSE).

```bibtex
@software{sensie_eval,
  author  = {{Sensie, LLC}},
  title   = {sensie-eval: subject-disjoint evaluation harness for motion-biomarker classification},
  year    = {2026},
  url     = {https://github.com/sensie-app/sensie-eval-harness}
}
```
