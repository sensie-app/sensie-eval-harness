"""
evaluate.py — Subject-Disjoint Evaluation Harness

Loads synthetic IMU data and computes subject-disjoint evaluation metrics
matching the pre-registered pilot endpoints:

  1. Per-subject signal reliability scoring
  2. Subject-disjoint classification accuracy
  3. Routing validity (high-signal vs. low-signal cohort gap)

IP GUARDRAIL: This is a metrics skeleton. It contains NO real feature
definitions, NO model code, NO training data, and NO CFD feature families.
Uses only numpy/scipy — no ML frameworks. All classification logic is a
placeholder; real classification runs server-side at Sensie.

Usage:
    python evaluate.py --data path/to/synthetic_data.npz --train_frac 0.7
"""

import argparse
import numpy as np
from scipy import stats
from typing import Dict, List, Tuple


def load_dataset(filepath: str) -> List[Dict]:
    """
    Load a synthetic dataset from a .npz file (produced by
    generate_synthetic_imu.py).

    Returns a list of subject dicts with keys: subject_id, signal_quality,
    repetitions (list of {"accel": array, "gyro": array}).
    """
    archive = np.load(filepath, allow_pickle=False)

    # Parse the flat key structure back into subject-organized data
    subjects = {}
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


def compute_signal_reliability(
    subject: Dict,
) -> float:
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
    sets. This mirrors real deployment where Sensie calibrates per-user on
    a known set and evaluates routing on new users.
    """
    rng = np.random.RandomState(seed)
    n_train = max(1, int(len(subjects) * train_frac))

    indices = rng.permutation(len(subjects))
    train_idx = set(indices[:n_train].tolist())
    test_idx = set(indices[n_train:].tolist())

    train_subjects = [subjects[i] for i in range(len(subjects)) if i in train_idx]
    test_subjects = [subjects[i] for i in range(len(subjects)) if i in test_idx]

    return train_subjects, test_subjects


def classify_subject(
    subject: Dict,
    threshold: float = 0.5,
) -> Tuple[int, float]:
    """
    Classify a subject as high-signal (1) or low-signal (0) based on
    signal reliability score.

    In real Sensie deployment, this classification runs server-side using
    the proprietary calibrated model. Here it's a simple threshold on the
    reliability score — sufficient for evaluating the subject-disjoint
    methodology against synthetic data.
    """
    reliability = compute_signal_reliability(subject)
    predicted = 1 if reliability >= threshold else 0
    return predicted, reliability


def evaluate_subject_disjoint(
    subjects: List[Dict],
    train_frac: float = 0.7,
    seed: int = 42,
) -> Dict:
    """
    Run full subject-disjoint evaluation.

    Computes the metrics that map to the pre-registered pilot endpoints:
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
        pred, rel = classify_subject(subject, threshold=0.5)
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
        u_stat = 0.0

    # Per-cohort stats
    results = {
        "n_train_subjects": len(train_subjects),
        "n_test_subjects": len(test_subjects),
        "accuracy": float(accuracy),
        "precision": float(precision),
        "recall": float(recall),
        "f1_score": float(f1),
        "confusion_matrix": {
            "tp": int(tp), "fp": int(fp),
            "tn": int(tn), "fn": int(fn),
        },
        "routing_validity": {
            "high_signal_mean_reliability": float(np.mean(high_reliabilities)) if len(high_reliabilities) > 0 else 0.0,
            "low_signal_mean_reliability": float(np.mean(low_reliabilities)) if len(low_reliabilities) > 0 else 0.0,
            "routing_gap": float(routing_gap),
            "p_value": float(p_value),
            "significant_at_0_05": bool(p_value < 0.05),
        },
        "reliability_threshold": 0.5,
    }

    return results


def print_report(results: Dict) -> None:
    """Pretty-print evaluation results."""
    print("=" * 60)
    print("SUBJECT-DISJOINT EVALUATION REPORT")
    print("=" * 60)
    print(f"\nSplit: {results['n_train_subjects']} train / "
          f"{results['n_test_subjects']} test subjects")
    print(f"\nClassification (threshold={results['reliability_threshold']}):")
    print(f"  Accuracy:  {results['accuracy']:.3f}")
    print(f"  Precision: {results['precision']:.3f}")
    print(f"  Recall:    {results['recall']:.3f}")
    print(f"  F1 Score:  {results['f1_score']:.3f}")

    cm = results["confusion_matrix"]
    print(f"\nConfusion Matrix:")
    print(f"  TP={cm['tp']:3d}  FP={cm['fp']:3d}")
    print(f"  FN={cm['fn']:3d}  TN={cm['tn']:3d}")

    rv = results["routing_validity"]
    print(f"\nRouting Validity:")
    print(f"  High-signal cohort mean reliability: {rv['high_signal_mean_reliability']:.3f}")
    print(f"  Low-signal cohort mean reliability:  {rv['low_signal_mean_reliability']:.3f}")
    print(f"  Routing gap:                         {rv['routing_gap']:.3f}")
    print(f"  p-value (Mann-Whitney U):            {rv['p_value']:.4f}")
    print(f"  Significant at α=0.05:               {rv['significant_at_0_05']}")

    print("\nPilot Endpoint Mapping:")
    print(f"  Primary (noise reduction proxy):     Accuracy={results['accuracy']:.1%}")
    print(f"  Secondary (cohort gap):              Gap={rv['routing_gap']:.3f}, "
          f"p={rv['p_value']:.4f}")

    noise_reduction_passes = results["accuracy"] >= 0.70
    routing_passes = rv["significant_at_0_05"] and rv["routing_gap"] > 0.1
    print(f"\nPre-registered thresholds (synthetic baseline):")
    print(f"  Accuracy ≥ 70%:     {'PASS' if noise_reduction_passes else 'FAIL'}")
    print(f"  Routing gap valid:  {'PASS' if routing_passes else 'FAIL'}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Subject-disjoint evaluation of synthetic IMU biomarker data."
    )
    parser.add_argument(
        "--data", type=str, required=True,
        help="Path to synthetic .npz file from generate_synthetic_imu.py"
    )
    parser.add_argument(
        "--train_frac", type=float, default=0.7,
        help="Fraction of subjects for calibration (default: 0.7)"
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for train/test split (default: 42)"
    )
    parser.add_argument(
        "--threshold", type=float, default=0.5,
        help="Reliability threshold for classification (default: 0.5)"
    )
    args = parser.parse_args()

    subjects = load_dataset(args.data)
    print(f"Loaded {len(subjects)} subjects from {args.data}")

    results = evaluate_subject_disjoint(
        subjects,
        train_frac=args.train_frac,
        seed=args.seed,
    )
    results["reliability_threshold"] = args.threshold

    print_report(results)


if __name__ == "__main__":
    main()
