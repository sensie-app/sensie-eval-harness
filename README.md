# sensie-eval

Subject-disjoint evaluation harness for motion-biomarker classification. Generates synthetic IMU data with controllable per-subject signal quality, runs a strict subject-disjoint evaluation protocol against it, and emits both a human-readable report and schema-validated JSON. Runs entirely offline — no account, no API key, no network calls.

License: Apache-2.0 (provisional — pending counsel confirmation; see [LICENSE](LICENSE)).

## What the underlying signal is

Sensie measures a smartphone motion signal: a user performs a brief gesture while holding their phone, and the resulting accelerometer and gyroscope streams (100 Hz, 3-axis each) are classified into a state read relative to a stated proposition. The features used derive from involuntary micro-dynamics of the motion, and the classifier is personalized — each user completes a calibration phase, and accuracy is person-variable. In Sensie's internal research trials (9 PhD-led trials, 18,000+ sessions), personalized post-calibration state-classification accuracy was 83.6% among users whose reads lock in. That person-variability is the entire motivation for this harness: if accuracy varies per user, the honest deployment question is not "what is the average accuracy?" but "can you predict, for a *new* user, whether their signal is reliable enough to route on?" — which is exactly what a subject-disjoint evaluation tests. The trial figures are internal and have not been independently replicated; this repository exists so you can inspect and exercise the evaluation methodology yourself.

## Quickstart

```bash
git clone https://github.com/sensie-app/sensie-eval-harness
cd sensie-eval-harness
pip install .
sensie-eval run --sample --output-json results.json
python -m json.tool results.json
```

`--sample` uses a small synthetic dataset bundled with the package (32 subjects, 3 gesture repetitions each), so the command works with no downloads and no network access. The console report shows the train/test split, held-out classification accuracy, and the routing-validity gap; `results.json` contains the same numbers in the machine-readable format described below.

> The bundled sample is synthetic data with parameters chosen to demonstrate the evaluation methodology. Effect sizes and accuracy are illustrative of the harness, not representative of the real somatic signal — real-signal evaluation requires live reads via the hosted API.

## Reading the output

The evaluation splits subjects into disjoint calibration (train) and evaluation (test) sets — no subject appears in both. For each held-out subject it computes a **signal reliability score** (cross-repetition correlation of the raw IMU streams) and classifies the subject as high-signal or low-signal against a threshold. It then reports:

- **Classification metrics** — accuracy, precision, recall, F1, and a confusion matrix of predicted vs. true signal quality (true labels come from the synthetic generator's latent quality parameter).
- **Routing validity** — the mean reliability gap between the truly-high-signal and truly-low-signal cohorts, with a Mann-Whitney U test. A positive, significant gap means the reliability score orders unseen subjects correctly, i.e. it would be valid to route on.
- **PASS/FAIL banner** — pre-set methodology thresholds (accuracy ≥ 70%, significant routing gap > 0.1) applied to the run. On synthetic data these are demonstrations of the protocol, not claims about real humans.

## Generating your own data

The generator gives every synthetic subject a latent `signal_quality` in [0, 1] that controls how reproducibly their gesture renders through noise (white noise, low-frequency drift, and a 16-bit quantization floor):

```bash
sensie-eval generate --n-subjects 50 --n-repetitions 5 --noise 0.3 --seed 1 --output my_data.npz
sensie-eval run --data my_data.npz --train-frac 0.7 --seed 42 --threshold 0.5 --output-json my_results.json
```

### CLI reference

| Command | Flag | Default | Meaning |
|---|---|---|---|
| `generate` | `--n-subjects` | 50 | Number of synthetic subjects |
| | `--n-repetitions` | 5 | Gesture repetitions per subject |
| | `--duration` | 4.0 | Gesture duration in seconds (100 Hz sample rate) |
| | `--noise` | 0.15 | Base noise level applied to quality-0 subjects |
| | `--seed` | 42 | RNG seed |
| | `--output` | — | Output `.npz` path (omit to print a summary only) |
| `run` | `--data` / `--sample` | — | Dataset path, or the bundled sample (exactly one required) |
| | `--train-frac` | 0.7 | Fraction of subjects used for calibration |
| | `--seed` | 42 | Seed for the subject-disjoint split |
| | `--threshold` | 0.5 | Reliability threshold for high/low-signal classification |
| | `--output-json` | — | Write machine-readable results to this path |

## Data format

Datasets are compressed NumPy archives (`.npz`) with a flat key layout, so you can build them from your own IMU recordings without this package:

| Key pattern | Value |
|---|---|
| `subject_<id>_quality` | scalar float in [0, 1] — ground-truth signal quality |
| `subject_<id>_rep_<n>_accel` | `(3, N)` float array, m/s², 100 Hz |
| `subject_<id>_rep_<n>_gyro` | `(3, N)` float array, rad/s, 100 Hz |

`<id>` is a 1-indexed integer, `<n>` is a 0-indexed repetition. Every subject needs at least 2 repetitions for a meaningful reliability score.

## Machine-readable output (`--output-json`)

The JSON document conforms to [`schemas/output.schema.json`](schemas/output.schema.json) (JSON Schema 2020-12). Top level:

- `schema_version` — output schema version, currently `"1"`
- `harness_version` — the `sensie-eval` package version
- `config` — `data_source`, `train_frac`, `seed`, `threshold`
- `results` — all metrics from the console report: subject counts, `accuracy`, `precision`, `recall`, `f1_score`, `confusion_matrix`, and `routing_validity` (cohort means, `routing_gap`, `p_value`, `significant_at_0_05`)

To validate a results file against the schema:

```bash
pip install jsonschema
python -c "import json, jsonschema; jsonschema.validate(json.load(open('results.json')), json.load(open('schemas/output.schema.json'))); print('results.json: valid')"
```

The schema is validated in CI on every run, and `schema_version` will change if the format ever changes incompatibly.

## Python API

Everything the CLI does is importable:

```python
from sensie_eval import (
    generate_subject_dataset,
    evaluate_subject_disjoint,
    compute_signal_reliability,
    save_dataset,
    load_dataset,
)

subjects = generate_subject_dataset(n_subjects=40, n_repetitions=3, seed=7)
results = evaluate_subject_disjoint(subjects, train_frac=0.7, seed=42, threshold=0.5)
print(f"accuracy={results['accuracy']:.3f}",
      f"routing_gap={results['routing_validity']['routing_gap']:.3f}")
```

## Limitations — read this before drawing conclusions

- **All bundled and generated data is synthetic.** Nothing this harness outputs is evidence about real human subjects. The ground-truth labels are the generator's own latent parameter, so the classification task is solvable by construction — the harness demonstrates the *evaluation protocol*, not classifier capability.
- **The reliability scorer is a baseline, not the production system.** It is a plain cross-repetition correlation. Sensie's production classifier, features, and weights are not in this repository, and results here do not transfer to it.
- **The PASS/FAIL banner is a methodology demo.** Thresholds are applied to synthetic data whose difficulty you control via `--noise`; you can make any run pass or fail.
- **The 83.6% figure is internal.** It is personalized, post-calibration, and measured on users whose reads lock in — population-level accuracy before calibration is lower, and the figure has not been independently replicated.
- **Subject-disjoint on synthetic data is a lower bar than real deployment.** Real-world factors (device variance, gesture drift over time, adversarial users) are not modeled by this generator.
- **No network API is included.** This release is offline-only tooling; evaluation against a live classifier is not part of this package.

## What this repository does NOT contain

No model weights, no training data, no feature definitions, and no production inference or scoring logic. All classification in this repository is the placeholder correlation baseline described above.

## Development

```bash
pip install -e '.[dev]'
ruff check .
pytest
```

CI (GitHub Actions) runs lint, the test suite, a scripted end-to-end walkthrough of every command in this README (`scripts/readme_walkthrough.sh`), and JSON-schema validation of the harness output. The bundled sample dataset is regenerated deterministically by `scripts/make_sample_data.py`.

## License

Apache-2.0 (provisional — the license choice is pending final counsel confirmation; the full text is in [LICENSE](LICENSE) and applies as published unless this note is removed in a future release).

---

A hosted service that runs this same evaluation protocol against Sensie's production classifier is available at [joinsensie.com](https://joinsensie.com).
