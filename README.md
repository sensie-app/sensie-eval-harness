# Sensie Eval Harness

### Verify the Human, Not the Label

**Pre-hoc annotator state verification via motion biomarker — evaluation harness for human-data and RLHF platform teams.**

---

## The Problem

Preference data quality is the binding constraint on alignment work. Published inter-annotator disagreement on preference labels exceeds 20% in widely used RLHF datasets — roughly 24–27%, given reported agreement rates of ~73–77% (Stiennon et al. 2020; Ouyang et al. 2022). Not all of that is honest disagreement: inattention and LLM-assisted answers hide inside it, and alignment performance degrades sharply with noisy or flipped labels. The industry's answer has been post-hoc and sampled: gold tasks, spot checks (~10% inspection rates), inter-annotator agreement thresholds, and statistical filtering after the labels are already bought.

Two failure modes slip through all of it:

1. **Inattention and fatigue.** Preference annotation is cognitively expensive. A disengaged annotator produces plausible-looking labels that pass surface QA and poison the reward model anyway.
2. **LLM-assisted completion.** An annotator pasting tasks into a frontier model produces output that is, by construction, indistinguishable from engaged human judgment at the label level. Output review cannot catch it, because the output is the disguise.

Both failure modes share a root: **all existing QA inspects the label. None of it inspects the human.**

## What Sensie Measures

Sensie is a smartphone motion-based biomarker — no wearable, no camera, no added hardware. The annotator performs a brief gesture while holding their phone; accelerometer and gyroscope data from the involuntary components of that motion are classified into a nervous-system state read relative to a stated proposition.

Properties that matter:

- **Involuntary signal.** The classified features derive from micro-dynamics of motion the user does not consciously control. It cannot be performed by a language model, scripted, or faked by an inattentive user going through the motions — degraded engagement degrades the signal itself.
- **Per-user calibrated models.** Each user runs through a multi-stage calibration; classification is personalized, not population-average.
- **Server-side only.** No model ships on device. The read is computed against Sensie's trained classifier and 18,000+ session labeled dataset.
- **A routing layer, not a polygraph.** The core product is a **pre-screen**: before an annotator's labels enter your pipeline, Sensie predicts per-user signal reliability and routes accordingly ("reads clearly" vs. "still calibrating"). You pay for verified reads from high-signal humans, not for hope.

## Evidence Base

- 9 PhD-led research trials across 18,000+ sessions
- 83.6% state-classification accuracy on calibrated users (personalized, post-calibration — the real-conditions figure; accuracy is person-variable and rises with calibration, which is precisely why the pre-screen/routing layer exists)
- Two granted US utility patents plus one additional filing covering the gesture biomarker and classification system
- Trials led and advised by researchers affiliated with Harvard Medical School and Oxford (DPhil)

## About This Repository

This repository contains a **subject-disjoint evaluation harness** for benchmarking motion-biomarker classification systems against synthetic data. It is designed to let technical teams independently assess the methodology before committing to a paid pilot.

**Note on PASS/FAIL output:** The harness prints PASS/FAIL at the end of each run. This banner demonstrates the evaluation methodology (pre-registered thresholds applied to a subject-disjoint split) — it operates on synthetic data only and does not represent performance on real human subjects. All PASS/FAIL results are artifacts of the synthetic data generator's configurable noise parameters.

The harness generates synthetic IMU streams (100Hz accelerometer + gyroscope, 3-axis) with configurable subject-specific signal characteristics and noise profiles. It then evaluates classification performance under a strict subject-disjoint protocol — models are calibrated on one set of synthetic subjects and tested on a held-out set they have never seen. This mirrors the real-world deployment: Sensie calibrates per-user and routes new users through a pre-screen before accepting their reads.

### What this repo contains

- **`src/sensie_eval/`** — The installable `sensie-eval` package: synthetic IMU generation, subject-disjoint evaluation, the CLI, and an optional live-API client.
- **`src/generate_synthetic_imu.py`** — Produces synthetic IMU data streams matching the sensor spec (100Hz, 3-axis accelerometer + gyroscope) with configurable noise levels per subject.
- **`src/evaluate.py`** — Loads synthetic data and computes subject-disjoint evaluation metrics: per-subject signal reliability scoring, cohort-level classification accuracy, and routing validity (gap between high-signal and low-signal cohorts).
- **`docs/`** — [Quickstart](docs/quickstart.md), [API reference](docs/api-reference.md), [quota & limits](docs/quota-limits.md), [troubleshooting](docs/troubleshooting.md).
- **`tests/`** — Unit tests validating the generation and evaluation pipeline.
- **`requirements.txt`** — Minimal dependencies: `numpy` and `scipy` only.

## Run It

Offline, on synthetic data you control — no account, no network:

```bash
pip install sensie-eval
sensie-eval run
```

The offline run generates synthetic subjects, evaluates them under the subject-disjoint protocol, and prints the report with the PASS/FAIL banner described above.

## Run against the live API

The same CLI can post summary reads to Sensie's live API, so you can see the metered read path — session creation, reads, and quota transparency — before committing to anything.

1. Get a free trial key at **https://somabets.com/trial** (email verification, no credit card). The key is shown exactly once: `sk_sensie_<64 hex>`. Trial terms: **100 reads per rolling 7-day window, one trial per email, ever.**

2. Export it and run:

<!-- doctest: api -->
```bash
export SENSIE_API_KEY=sk_sensie_your_key_here
sensie-eval run --api
```

This verifies your API key first (creating the session up front, so a bad key fails in seconds), then runs the offline evaluation and posts 5 summary reads to that session (change with `--reads N`). Expected output ends with:

```text
Live API summary
----------------------------------------
  Session id:      <uuid>
  Reads posted:    5
  Reads returned:  5
  ...
```

3. When the quota is exhausted, the API returns a structured **HTTP 429** — this is deliberate quota transparency, not an error to debug:

```text
Trial quota exhausted (HTTP 429).
  Used 100 of 100 reads in the current rolling 7-day window.
  Window resets at: 2026-07-12T00:00:00+00:00
```

The `window_reset_at` timestamp is when the oldest counted read ages out of the rolling window; clients self-schedule retries from it. Details in [docs/quota-limits.md](docs/quota-limits.md).

**Privacy note:** live mode sends only three scalar summary values per read (`whips`, `flowing`, `agreement`). Raw accelerometer/gyroscope arrays never leave your machine — the trial tier rejects raw motion at the API boundary.

### What this repo does NOT contain

This is an evaluation scaffold, not a model release. It contains **no model weights, no training data, no feature definitions, and no CFD feature families.** All classification logic is server-side at Sensie; this harness lets you validate the *methodology* — subject-disjoint evaluation, signal reliability scoring, and routing validity — against synthetic data you control.

## The Pilot

The next step after evaluating this harness is a fixed-scope, pre-registered pilot:

- **Structure:** 4–6 weeks. Run on Sensie's managed, pre-screened panel — zero integration work or annotator disruption on your side.
- **Primary endpoint:** Filtering to verified reads from high-signal users reduces label disagreement/error rate versus the unfiltered panel by ≥30% relative on gold-anchored tasks.
- **Secondary endpoint:** The pre-screen's high-signal cohort outperforms the low-signal cohort by a pre-agreed margin, demonstrating the score predicts label quality.
- **Price:** $25,000, paid, fixed.
- **On success:** Pilot converts to a customer-funded Phase 2 — SDK integration into your annotator workforce, scoped as a paid SOW plus production pricing on a two-part tariff.

Pass = primary endpoint met + operational gate met. All thresholds are negotiated and locked before data collection; no post-hoc goalpost movement.

---

**Sensie, LLC** · joinsensie.com · mike@joinsensie.com

*"Every existing QA inspects the label. Sensie inspects the human."*
