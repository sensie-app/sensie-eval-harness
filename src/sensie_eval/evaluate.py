"""Subject-disjoint evaluation metrics.

Loads IMU datasets (synthetic, or your own data in the same .npz layout)
and computes subject-disjoint evaluation metrics:

  1. Per-subject signal reliability scoring
  2. Subject-disjoint classification accuracy
  3. Routing validity (high-signal vs. low-signal cohort gap)

IP note: this is a metrics skeleton. It contains NO real feature
definitions, NO model code, and NO training data. The reliability scorer
here is a simple cross-repetition correlation baseline; it exists to
demonstrate the evaluation methodology, not to reproduce any production
classifier.
"""

from typing import Dict, List, Tuple

import numpy as np
from scipy import stats


def load_dataset(filepath: str) -> List[Dict]:
    """
    Load a dataset from a .npz file (produced by ``sensie-eval generate``
    or :func:`sensie_eval.generate.save_dataset`).

    Returns a list of subject dicts with keys: subject_id, signal_quality,
    repetitions (list of {"accel": array, "gyro": array}).
    """
    archive = np.load(filepath, allow_pickle=False)

    # Parse the flat key structure back into subject-organized data
    subjects: Dict[int, Dict] = {}
    for key in archive.files:
        parts = key.split("_")
        if parts[0] != "subject":
            continue

        subject_id = int(parts[1])
        if parts[2] == "quality":
            if subject_id not in subjects:
                subjects[subject_id] = {
                    "subject_id": subject_id,
                    "signal_quality": float(archive[key]),
                    "repetitions": [],
                }
            subjects[subject_id]["signal_quality"] = float(archive[key])
        elif parts[2] == "rep":
            rep_idx = int(parts[3])
            sensor = parts[4]  # "accel" or "gyro"
            if subject_id not in subjects:
                subjects[subject_id] = {
                    "subject_id": subject_id,
                    "signal_quality": None,
                    "repetitions": [],
                }
            # Extend repetitions list as needed
            while len(subjects[subject_id]["repetitions"]) <= rep_idx:
                subjects[subject_id]["repetitions"].append({})
            subjects[subject_id]["repetitions"][rep_idx][sensor] = archive[key]

    # Sort by subject_id and return as list
    result = [subjects[sid] for sid in sorted(subjects.keys())]
    archive.close()
    return result


def compute_signal_reliability(subject: Dict) -> float:
    """
    Compute a per-subject signal reliability score.

    Measures cross-repetition signal correlation — how consistently the
    raw IMU streams reproduce across repetitions. High signal quality
    produces highly reproducible gesture patterns; low signal quality
    introduces independent noise that decorrelates repetitions.

    Parameters
    ----------
    subject : dict
        Subject data with repetitions key.

    Returns
    -------
    float
        Reliability score in [0, 1]. Higher is more reliable.
    """
    reps = subject["repetitions"]
    n_reps = len(reps)
    if n_reps < 2:
        return 0.5

    correlations = []
    for i in range(n_reps):
        for j in range(i + 1, n_reps):
            for sensor in ("accel", "gyro"):
                # Flatten (3, N) into 1D for correlation
                data_i = reps[i][sensor].ravel()
                data_j = reps[j][sensor].ravel()
                corr = np.corrcoef(data_i, data_j)[0, 1]
                # Clip negative correlations (anti-correlation is also noise)
                correlations.append(max(0.0, corr))

    if not correlations:
        return 0.0

    mean_corr = np.mean(correlations)
    # Correlation is directly interpretable as reliability
    reliability = float(np.clip(mean_corr, 0.0, 1.0))
    return reliability


def subject_disjoint_split(
    subjects: List[Dict],
    train_frac: float = 0.7,
    seed: int = 42,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Split subjects into disjoint train and test sets.

    This is the key subject-disjoint constraint: no subject appears in both
    sets. Calibration happens on a known set of subjects; evaluation happens
    on subjects the system has never seen.
    """
    rng = np.random.RandomState(seed)
    n_train = max(1, int(len(subjects) * train_frac))

    indices = rng.permutation(len(subjects))
    train_idx = set(indices[:n_train].tolist())

    train_subjects = [subjects[i] for i in range(len(subjects)) if i in train_idx]
    test_subjects = [subjects[i] for i in range(len(subjects)) if i not in train_idx]

    return train_subjects, test_subjects


def classify_subject(
    subject: Dict,
    threshold: float = 0.5,
) -> Tuple[int, float]:
    """
    Classify a subject as high-signal (1) or low-signal (0) based on
    signal reliability score.

    This is a simple threshold on the cross-repetition reliability score —
    a baseline sufficient for exercising the subject-disjoint methodology
    against synthetic data. It is not a production classifier.
    """
    reliability = compute_signal_reliability(subject)
    predicted = 1 if reliability >= threshold else 0
    return predicted, reliability


def evaluate_subject_disjoint(
    subjects: List[Dict],
    train_frac: float = 0.7,
    seed: int = 42,
    threshold: float = 0.5,
) -> Dict:
    """
    Run full subject-disjoint evaluation.

    Computes:
      1. Classification accuracy on held-out subjects
      2. Routing validity: high-signal vs. low-signal cohort gap
      3. Per-cohort signal reliability statistics

    Parameters
    ----------
    subjects : list
        Full dataset (all subjects).
    train_frac : float
        Fraction of subjects for calibration/training.
    seed : int
        Random seed for the train/test split.
    threshold : float
        Reliability threshold for high/low-signal classification.

    Returns
    -------
    dict
        Evaluation metrics.
    """
    train_subjects, test_subjects = subject_disjoint_split(
        subjects, train_frac=train_frac, seed=seed
    )

    # Compute reliability scores for all test subjects
    predictions = []
    reliabilities = []
    ground_truth = []

    for subject in test_subjects:
        pred, rel = classify_subject(subject, threshold=threshold)
        predictions.append(pred)
        reliabilities.append(rel)

        # Ground truth: signal_quality >= 0.6 is "high-signal"
        gt = 1 if subject["signal_quality"] >= 0.6 else 0
        ground_truth.append(gt)

    predictions = np.array(predictions)
    reliabilities = np.array(reliabilities)
    ground_truth = np.array(ground_truth)

    # Primary metric: classification accuracy on held-out subjects
    accuracy = np.mean(predictions == ground_truth)

    # Confusion matrix components
    tp = np.sum((predictions == 1) & (ground_truth == 1))
    fp = np.sum((predictions == 1) & (ground_truth == 0))
    tn = np.sum((predictions == 0) & (ground_truth == 0))
    fn = np.sum((predictions == 0) & (ground_truth == 1))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    # Routing validity: gap between high-signal and low-signal cohorts
    high_mask = np.array([s["signal_quality"] >= 0.6 for s in test_subjects])
    low_mask = np.array([s["signal_quality"] < 0.4 for s in test_subjects])

    high_reliabilities = reliabilities[high_mask]
    low_reliabilities = reliabilities[low_mask]

    if len(high_reliabilities) > 0 and len(low_reliabilities) > 0:
        routing_gap = np.mean(high_reliabilities) - np.mean(low_reliabilities)
        # Mann-Whitney U test for statistical significance of the gap
        u_stat, p_value = stats.mannwhitneyu(
            high_reliabilities, low_reliabilities, alternative="greater"
        )
    else:
        routing_gap = 0.0
        p_value = 1.0

    results = {
        "n_train_subjects": len(train_subjects),
        "n_test_subjects": len(test_subjects),
        "accuracy": float(accuracy),
        "precision": float(precision),
        "recall": float(recall),
        "f1_score": float(f1),
        "confusion_matrix": {
            "tp": int(tp),
            "fp": int(fp),
            "tn": int(tn),
            "fn": int(fn),
        },
        "routing_validity": {
            "high_signal_mean_reliability": (
                float(np.mean(high_reliabilities)) if len(high_reliabilities) > 0 else 0.0
            ),
            "low_signal_mean_reliability": (
                float(np.mean(low_reliabilities)) if len(low_reliabilities) > 0 else 0.0
            ),
            "routing_gap": float(routing_gap),
            "p_value": float(p_value),
            "significant_at_0_05": bool(p_value < 0.05),
        },
        "reliability_threshold": float(threshold),
    }

    return results


def build_json_output(results: Dict, config: Dict) -> Dict:
    """
    Build the machine-readable output document for ``--output-json``.

    The returned dict conforms to ``schemas/output.schema.json``.

    Parameters
    ----------
    results : dict
        Metrics from :func:`evaluate_subject_disjoint`.
    config : dict
        Run configuration: data_source (str), train_frac (float),
        seed (int), threshold (float).
    """
    from sensie_eval import __version__

    return {
        "schema_version": "1",
        "harness_version": __version__,
        "config": {
            "data_source": str(config["data_source"]),
            "train_frac": float(config["train_frac"]),
            "seed": int(config["seed"]),
            "threshold": float(config["threshold"]),
        },
        "results": results,
    }


def print_report(results: Dict) -> None:
    """Pretty-print evaluation results."""
    print("=" * 60)
    print("SUBJECT-DISJOINT EVALUATION REPORT")
    print("=" * 60)
    print(
        f"\nSplit: {results['n_train_subjects']} train / "
        f"{results['n_test_subjects']} test subjects"
    )
    print(f"\nClassification (threshold={results['reliability_threshold']}):")
    print(f"  Accuracy:  {results['accuracy']:.3f}")
    print(f"  Precision: {results['precision']:.3f}")
    print(f"  Recall:    {results['recall']:.3f}")
    print(f"  F1 Score:  {results['f1_score']:.3f}")

    cm = results["confusion_matrix"]
    print("\nConfusion Matrix:")
    print(f"  TP={cm['tp']:3d}  FP={cm['fp']:3d}")
    print(f"  FN={cm['fn']:3d}  TN={cm['tn']:3d}")

    rv = results["routing_validity"]
    print("\nRouting Validity:")
    print(f"  High-signal cohort mean reliability: {rv['high_signal_mean_reliability']:.3f}")
    print(f"  Low-signal cohort mean reliability:  {rv['low_signal_mean_reliability']:.3f}")
    print(f"  Routing gap:                         {rv['routing_gap']:.3f}")
    print(f"  p-value (Mann-Whitney U):            {rv['p_value']:.4f}")
    print(f"  Significant at alpha=0.05:           {rv['significant_at_0_05']}")

    noise_reduction_passes = results["accuracy"] >= 0.70
    routing_passes = rv["significant_at_0_05"] and rv["routing_gap"] > 0.1
    print("\nMethodology thresholds (synthetic baseline — see README):")
    print(f"  Accuracy >= 70%:    {'PASS' if noise_reduction_passes else 'FAIL'}")
    print(f"  Routing gap valid:  {'PASS' if routing_passes else 'FAIL'}")
    print("=" * 60)
