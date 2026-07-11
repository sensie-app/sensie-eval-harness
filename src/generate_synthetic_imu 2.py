"""
generate_synthetic_imu.py — Synthetic IMU Data Generator

Produces synthetic accelerometer and gyroscope streams matching the sensor
specification: 100 Hz sample rate, 3-axis per sensor, with configurable
subject-specific noise levels.

This generates data for subject-disjoint evaluation: each synthetic subject
has a latent "signal quality" parameter that governs how clearly their
motion signal can be classified. High-signal subjects produce clean,
separable gesture patterns; low-signal subjects produce noisy, ambiguous
streams. This mirrors the real-world deployment where Sensie routes
high-signal annotators and filters out low-signal ones.

IP GUARDRAIL: Synthetic data only. No real feature definitions, no model
code, no training data, no CFD feature families. Uses only numpy — no ML
frameworks.

Usage:
    python generate_synthetic_imu.py --n_subjects 50 --duration 4.0 --noise 0.1
"""

import argparse
import numpy as np
from typing import Dict, Tuple


def generate_gesture_template(
    duration: float = 4.0,
    sample_rate: float = 100.0,
    gesture_type: str = "triple_whip",
) -> Dict[str, np.ndarray]:
    """
    Generate a noise-free gesture template — the "ideal" motion pattern
    that a perfectly engaged subject would produce.

    Parameters
    ----------
    duration : float
        Gesture duration in seconds.
    sample_rate : float
        Sensor sample rate in Hz (default 100 Hz).
    gesture_type : str
        Type of gesture template. Currently only "triple_whip" is supported
        and produces a canonical three-pulse acceleration pattern with
        corresponding gyroscope rotations.

    Returns
    -------
    dict
        Keys: "accel" (3, N) and "gyro" (3, N) arrays in m/s^2 and rad/s.
    """
    n_samples = int(duration * sample_rate)
    t = np.linspace(0, duration, n_samples)

    # Triple-pulse acceleration pattern: three distinct movement impulses
    # separated by brief pauses. This is a synthetic approximation of the
    # involuntary motion dynamics captured during the gesture.
    pulse_centers = np.array([0.8, 1.8, 2.8])  # seconds
    pulse_width = 0.25
    pulse_amp = np.array([1.5, 2.0, 1.8])  # varying intensities

    accel = np.zeros((3, n_samples))
    gyro = np.zeros((3, n_samples))

    for center, amp in zip(pulse_centers, pulse_amp):
        envelope = amp * np.exp(-0.5 * ((t - center) / pulse_width) ** 2)

        # Each pulse has a characteristic 3D direction (the whip motion)
        phase = 2 * np.pi * (center / pulse_centers[-1])
        axis_weights = np.array([
            np.cos(phase),
            np.sin(phase),
            0.3 * np.sin(2 * phase),
        ])

        for axis in range(3):
            accel[axis] += envelope * axis_weights[axis]

    # Gyroscope derived from acceleration changes (rotational component)
    # Simple finite-difference approximation of angular velocity
    for axis in range(3):
        gyro[axis, 1:-1] = np.diff(accel[axis], n=2) * sample_rate * 0.3

    return {"accel": accel, "gyro": gyro}


def add_subject_noise(
    template: Dict[str, np.ndarray],
    signal_quality: float,
    noise_std: float = 0.1,
    drift_rate: float = 0.005,
    seed: int = 0,
) -> Dict[str, np.ndarray]:
    """
    Apply subject-specific noise to a gesture template.

    Each synthetic subject has a `signal_quality` in [0, 1]:
      - 1.0 = perfect signal (no noise added beyond sensor floor)
      - 0.0 = pure noise (template is completely obscured)

    Parameters
    ----------
    template : dict
        Clean gesture template from generate_gesture_template().
    signal_quality : float
        Subject-specific signal quality in [0, 1].
    noise_std : float
        Base noise standard deviation (scaled by 1 - signal_quality).
    drift_rate : float
        Sensor drift rate for low-frequency baseline wander
        (also scaled by 1 - signal_quality).
    seed : int
        Seed for the noise RNG (for reproducibility).

    Returns
    -------
    dict
        Noisy IMU streams with same structure as template.
    """
    n_samples = template["accel"].shape[1]
    degradation = 1.0 - signal_quality
    rng = np.random.RandomState(seed)

    result = {}
    for sensor in ("accel", "gyro"):
        clean = template[sensor].copy()
        n_axes = clean.shape[0]

        # White noise: dominant noise source, inversely proportional to quality
        effective_noise = noise_std * degradation
        white_noise = rng.randn(n_axes, n_samples) * effective_noise

        # Low-frequency drift (sensor bias wander), scaled by degradation
        effective_drift = drift_rate * degradation
        drift = np.cumsum(rng.randn(n_axes, n_samples) * effective_drift, axis=1)
        drift -= drift.mean(axis=1, keepdims=True)

        # Quantization noise floor (simulating 16-bit ADC)
        if sensor == "accel":
            lsb = 0.001  # ~0.001 m/s^2 for typical phone IMU
        else:
            lsb = 0.0001  # ~0.0001 rad/s for gyro
        quant_noise = rng.uniform(-lsb, lsb, (n_axes, n_samples))

        noisy = clean + white_noise + drift + quant_noise
        result[sensor] = noisy

    return result


def generate_subject_dataset(
    n_subjects: int = 50,
    n_repetitions: int = 5,
    duration: float = 4.0,
    sample_rate: float = 100.0,
    base_noise: float = 0.15,
    seed: int = 42,
) -> list:
    """
    Generate a full synthetic dataset with multiple subjects.

    Each subject has:
      - A latent signal_quality value (uniform [0, 1], representing the
        spectrum from "reads clearly" to "still calibrating")
      - n_repetitions of the gesture, each with independent noise draws
      - A subject label (1-indexed integer)

    Parameters
    ----------
    n_subjects : int
        Number of synthetic subjects.
    n_repetitions : int
        Number of gesture repetitions per subject.
    duration : float
        Gesture duration in seconds.
    sample_rate : float
        Sample rate in Hz (default 100 Hz).
    base_noise : float
        Base noise level for the noisiest subjects (quality=0).
    seed : int
        Random seed for reproducibility.

    Returns
    -------
    list of dicts
        Each dict has keys: subject_id, signal_quality, repetitions (list of
        {"accel": array, "gyro": array}).
    """
    rng = np.random.RandomState(seed)

    # Generate signal qualities — uniform distribution across subjects
    signal_qualities = rng.uniform(0.0, 1.0, n_subjects)

    # Generate a single gesture template for all subjects
    template = generate_gesture_template(
        duration=duration,
        sample_rate=sample_rate,
        gesture_type="triple_whip",
    )

    dataset = []
    for subj_idx in range(n_subjects):
        quality = float(signal_qualities[subj_idx])
        repetitions = []

        for rep in range(n_repetitions):
            # Derive a reproducible per-repetition seed from the global seed
            rep_seed = seed * 10000 + subj_idx * 100 + rep
            noisy = add_subject_noise(
                template,
                signal_quality=quality,
                noise_std=base_noise,
                seed=rep_seed,
            )
            repetitions.append(noisy)

        dataset.append({
            "subject_id": subj_idx + 1,
            "signal_quality": quality,
            "repetitions": repetitions,
        })

    return dataset


def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic IMU data for subject-disjoint evaluation."
    )
    parser.add_argument(
        "--n_subjects", type=int, default=50,
        help="Number of synthetic subjects (default: 50)"
    )
    parser.add_argument(
        "--duration", type=float, default=4.0,
        help="Gesture duration in seconds (default: 4.0)"
    )
    parser.add_argument(
        "--noise", type=float, default=0.15,
        help="Base noise level (default: 0.15)"
    )
    parser.add_argument(
        "--n_repetitions", type=int, default=5,
        help="Repetitions per subject (default: 5)"
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed (default: 42)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output .npz path (default: print summary only)"
    )
    args = parser.parse_args()

    dataset = generate_subject_dataset(
        n_subjects=args.n_subjects,
        n_repetitions=args.n_repetitions,
        duration=args.duration,
        sample_rate=100.0,
        base_noise=args.noise,
        seed=args.seed,
    )

    # Summary statistics
    qualities = [s["signal_quality"] for s in dataset]
    n_samples = dataset[0]["repetitions"][0]["accel"].shape[1]

    print(f"Generated {len(dataset)} subjects")
    print(f"  Duration: {args.duration}s @ 100 Hz → {n_samples} samples/stream")
    print(f"  Repetitions per subject: {args.n_repetitions}")
    print(f"  Signal quality range: [{min(qualities):.3f}, {max(qualities):.3f}]")
    print(f"  Mean signal quality: {np.mean(qualities):.3f}")

    # Subject distribution for routing bands
    high_signal = sum(1 for q in qualities if q >= 0.6)
    low_signal = sum(1 for q in qualities if q < 0.4)
    print(f"  High-signal subjects (q >= 0.6): {high_signal}")
    print(f"  Low-signal subjects (q < 0.4):  {low_signal}")
    print(f"  Indeterminate subjects:         {len(dataset) - high_signal - low_signal}")

    if args.output:
        # Save as compressed numpy archive
        save_dict: dict = {}
        for s in dataset:
            save_dict[f"subject_{s['subject_id']}_quality"] = (
                np.array(s["signal_quality"])
            )
            for r_idx, rep in enumerate(s["repetitions"]):
                for sensor in ("accel", "gyro"):
                    key = f"subject_{s['subject_id']}_rep_{r_idx}_{sensor}"
                    save_dict[key] = rep[sensor]

        np.savez_compressed(args.output, **save_dict)
        print(f"\nSaved dataset to {args.output}")


if __name__ == "__main__":
    main()
