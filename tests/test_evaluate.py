"""
test_evaluate.py — Unit tests for the evaluation harness.

Tests cover:
  1. Synthetic data generation consistency
  2. Feature extraction from IMU streams
  3. Signal reliability scoring
  4. Subject-disjoint split integrity
  5. End-to-end evaluation pipeline
  6. Signal quality → reliability score monotonic relationship
"""

import sys
import os
import unittest
import numpy as np

# Add src to path so tests run without an installed package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sensie_eval.generate_synthetic_imu import (
    generate_gesture_template,
    add_subject_noise,
    generate_subject_dataset,
)
from sensie_eval.evaluate import (
    compute_signal_reliability,
    subject_disjoint_split,
    classify_subject,
    evaluate_subject_disjoint,
    load_dataset,
)


class TestGestureGeneration(unittest.TestCase):
    """Tests for synthetic IMU data generation."""

    def test_template_shape(self):
        """Template produces correct shapes at 100Hz."""
        template = generate_gesture_template(duration=4.0, sample_rate=100.0)
        self.assertEqual(template["accel"].shape, (3, 400))
        self.assertEqual(template["gyro"].shape, (3, 400))

    def test_template_nonzero(self):
        """Template contains non-zero signal (not flat)."""
        template = generate_gesture_template(duration=4.0)
        self.assertTrue(np.any(np.abs(template["accel"]) > 0.01),
                        "Acceleration template is too flat")
        self.assertTrue(np.any(np.abs(template["gyro"]) > 0.0),
                        "Gyroscope template is flat")

    def test_template_reproducible(self):
        """Same parameters produce identical templates."""
        t1 = generate_gesture_template(duration=4.0, sample_rate=100.0)
        t2 = generate_gesture_template(duration=4.0, sample_rate=100.0)
        np.testing.assert_array_almost_equal(t1["accel"], t2["accel"])
        np.testing.assert_array_almost_equal(t1["gyro"], t2["gyro"])

    def test_noise_quality_zero(self):
        """Signal quality 0 produces high-noise data."""
        template = generate_gesture_template(duration=4.0)
        noisy = add_subject_noise(template, signal_quality=0.0, noise_std=0.5)
        # Noise should increase overall signal power
        noisy_std = np.std(noisy["accel"])
        clean_std = np.std(template["accel"])
        self.assertGreater(noisy_std, clean_std * 1.2)

    def test_noise_quality_one(self):
        """Signal quality 1 produces near-clean data."""
        template = generate_gesture_template(duration=4.0)
        noisy = add_subject_noise(template, signal_quality=1.0, noise_std=0.3)
        # Should be very close to template (only quantization noise)
        diff = np.abs(noisy["accel"] - template["accel"])
        self.assertLess(np.max(diff), 0.01,
                        "Quality=1 data should be nearly identical to template")


class TestDatasetGeneration(unittest.TestCase):
    """Tests for full dataset generation."""

    def test_dataset_structure(self):
        """Dataset has correct number of subjects and repetitions."""
        ds = generate_subject_dataset(
            n_subjects=20, n_repetitions=5, duration=3.0, seed=123
        )
        self.assertEqual(len(ds), 20)
        for subject in ds:
            self.assertIn("subject_id", subject)
            self.assertIn("signal_quality", subject)
            self.assertIn("repetitions", subject)
            self.assertEqual(len(subject["repetitions"]), 5)
            for rep in subject["repetitions"]:
                self.assertIn("accel", rep)
                self.assertIn("gyro", rep)

    def test_dataset_reproducible(self):
        """Same seed produces identical datasets."""
        ds1 = generate_subject_dataset(n_subjects=10, seed=42)
        ds2 = generate_subject_dataset(n_subjects=10, seed=42)
        for s1, s2 in zip(ds1, ds2):
            self.assertEqual(s1["subject_id"], s2["subject_id"])
            self.assertAlmostEqual(s1["signal_quality"], s2["signal_quality"])
            for r1, r2 in zip(s1["repetitions"], s2["repetitions"]):
                np.testing.assert_array_almost_equal(r1["accel"], r2["accel"])
                np.testing.assert_array_almost_equal(r1["gyro"], r2["gyro"])

    def test_signal_quality_range(self):
        """Signal qualities span [0, 1]."""
        ds = generate_subject_dataset(n_subjects=200, seed=42)
        qualities = [s["signal_quality"] for s in ds]
        self.assertLess(min(qualities), 0.1)
        self.assertGreater(max(qualities), 0.9)


class TestSignalReliability(unittest.TestCase):
    """Tests for signal reliability scoring."""

    def test_perfect_subject_high_reliability(self):
        """Subject with identical repetitions scores high reliability."""
        ds = generate_subject_dataset(
            n_subjects=1, n_repetitions=10, duration=4.0,
            base_noise=0.0, seed=42
        )
        # Override with perfect quality
        ds[0]["signal_quality"] = 1.0
        # Regenerate with near-zero noise
        template = generate_gesture_template(duration=4.0)
        ds[0]["repetitions"] = [
            add_subject_noise(template, signal_quality=0.99, noise_std=0.001)
            for _ in range(10)
        ]
        reliability = compute_signal_reliability(ds[0])
        self.assertGreater(reliability, 0.85,
                           f"High-quality subject should have reliability > 0.85, got {reliability}")

    def test_reliability_in_range(self):
        """Reliability score is always in [0, 1]."""
        ds = generate_subject_dataset(n_subjects=10, n_repetitions=5, seed=42)
        for subject in ds:
            rel = compute_signal_reliability(subject)
            self.assertGreaterEqual(rel, 0.0)
            self.assertLessEqual(rel, 1.0)


class TestSubjectDisjointSplit(unittest.TestCase):
    """Tests for subject-disjoint train/test split."""

    def test_no_overlap(self):
        """Train and test sets have zero subject overlap."""
        ds = generate_subject_dataset(n_subjects=30, n_repetitions=3, seed=42)
        train, test = subject_disjoint_split(ds, train_frac=0.7, seed=42)
        train_ids = {s["subject_id"] for s in train}
        test_ids = {s["subject_id"] for s in test}
        self.assertTrue(train_ids.isdisjoint(test_ids),
                        "Train and test sets must be disjoint")

    def test_all_subjects_accounted(self):
        """All subjects appear in either train or test."""
        ds = generate_subject_dataset(n_subjects=50, n_repetitions=2, seed=42)
        train, test = subject_disjoint_split(ds, train_frac=0.7, seed=42)
        self.assertEqual(len(train) + len(test), len(ds))

    def test_split_reproducible(self):
        """Same seed produces same split."""
        ds = generate_subject_dataset(n_subjects=30, n_repetitions=2, seed=42)
        t1, te1 = subject_disjoint_split(ds, train_frac=0.7, seed=42)
        t2, te2 = subject_disjoint_split(ds, train_frac=0.7, seed=42)
        self.assertEqual({s["subject_id"] for s in t1},
                         {s["subject_id"] for s in t2})
        self.assertEqual({s["subject_id"] for s in te1},
                         {s["subject_id"] for s in te2})


class TestEndToEnd(unittest.TestCase):
    """End-to-end integration tests."""

    def test_full_pipeline(self):
        """Generate → evaluate runs without error and produces valid metrics."""
        ds = generate_subject_dataset(
            n_subjects=50, n_repetitions=5, duration=4.0,
            base_noise=0.15, seed=42
        )
        results = evaluate_subject_disjoint(ds, train_frac=0.7, seed=42)

        self.assertIn("accuracy", results)
        self.assertIn("routing_validity", results)
        self.assertGreater(results["n_test_subjects"], 0)
        self.assertGreaterEqual(results["accuracy"], 0.0)
        self.assertLessEqual(results["accuracy"], 1.0)

    def test_signal_quality_correlates_with_reliability(self):
        """Higher signal quality subjects tend to have higher reliability scores."""
        ds = generate_subject_dataset(
            n_subjects=100, n_repetitions=5, duration=4.0,
            base_noise=0.2, seed=42
        )
        qualities = []
        reliabilities = []
        for subject in ds:
            qualities.append(subject["signal_quality"])
            reliabilities.append(compute_signal_reliability(subject))

        # Spearman correlation should be positive
        from scipy import stats
        spearman = stats.spearmanr(qualities, reliabilities)
        rho = spearman.statistic  # type: ignore[union-attr]
        p = spearman.pvalue  # type: ignore[union-attr]
        self.assertGreater(float(rho), 0.3,
                           f"Signal quality should positively correlate with "
                           f"reliability (rho={rho:.3f}, p={p:.4f})")

    def test_routing_gap_exists(self):
        """High-signal subjects should score higher reliability than low-signal."""
        ds = generate_subject_dataset(
            n_subjects=100, n_repetitions=5, duration=4.0,
            base_noise=0.3, seed=42
        )
        results = evaluate_subject_disjoint(ds, train_frac=0.7, seed=42)

        rv = results["routing_validity"]
        self.assertGreater(
            rv["high_signal_mean_reliability"],
            rv["low_signal_mean_reliability"],
            "High-signal cohort must have higher mean reliability"
        )
        self.assertGreater(rv["routing_gap"], 0.0,
                           "Routing gap must be positive")

    def test_accuracy_above_chance(self):
        """Classification accuracy should be above random chance (0.5)."""
        ds = generate_subject_dataset(
            n_subjects=100, n_repetitions=5, duration=4.0,
            base_noise=0.15, seed=42
        )
        results = evaluate_subject_disjoint(ds, train_frac=0.7, seed=42)
        self.assertGreater(results["accuracy"], 0.5,
                           f"Accuracy should be above chance, got {results['accuracy']:.3f}")


class TestLoadSaveRoundtrip(unittest.TestCase):
    """Tests for save/load roundtrip."""

    def test_roundtrip(self):
        """Generate, save, load, and verify data integrity."""
        import tempfile

        ds = generate_subject_dataset(
            n_subjects=10, n_repetitions=3, duration=2.0,
            base_noise=0.1, seed=42
        )

        with tempfile.NamedTemporaryFile(suffix=".npz", delete=False) as f:
            tmp_path = f.name

        try:
            # Save
            save_dict = {}
            for s in ds:
                save_dict[f"subject_{s['subject_id']}_quality"] = np.array(s["signal_quality"])
                for r_idx, rep in enumerate(s["repetitions"]):
                    for sensor in ("accel", "gyro"):
                        key = f"subject_{s['subject_id']}_rep_{r_idx}_{sensor}"
                        save_dict[key] = rep[sensor]
            np.savez_compressed(tmp_path, **save_dict)

            # Load
            loaded = load_dataset(tmp_path)
            self.assertEqual(len(loaded), len(ds))

            for orig, loaded_subj in zip(ds, loaded):
                self.assertEqual(orig["subject_id"], loaded_subj["subject_id"])
                self.assertAlmostEqual(orig["signal_quality"],
                                       loaded_subj["signal_quality"])
                self.assertEqual(len(orig["repetitions"]),
                                 len(loaded_subj["repetitions"]))
                for orig_rep, loaded_rep in zip(orig["repetitions"],
                                                loaded_subj["repetitions"]):
                    np.testing.assert_array_almost_equal(orig_rep["accel"],
                                                         loaded_rep["accel"])
                    np.testing.assert_array_almost_equal(orig_rep["gyro"],
                                                         loaded_rep["gyro"])
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
