"""Synthetic IMU data generator.

Produces synthetic accelerometer and gyroscope streams matching the sensor
specification used by the harness: 100 Hz sample rate, 3-axis per sensor,
with configurable subject-specific noise levels.

Each synthetic subject has a latent ``signal_quality`` parameter in [0, 1]
that governs how clearly their motion signal reproduces across repetitions.
High-quality subjects produce clean, reproducible gesture patterns;
low-quality subjects produce noisy, ambiguous streams. This lets the
evaluation demonstrate subject-disjoint routing methodology end-to-end on
data you fully control.

IP note: synthetic data only. No real feature definitions, no model code,
no training data. Uses only numpy — no ML frameworks.
"""

from typing import Dict, List

import numpy as np


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
        axis_weights = np.array(
            [
                np.cos(phase),
                np.sin(phase),
                0.3 * np.sin(2 * phase),
            ]
        )

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
) -> List[Dict]:
    """
    Generate a full synthetic dataset with multiple subjects.

    Each subject has:
      - A latent signal_quality value (uniform [0, 1])
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

        dataset.append(
            {
                "subject_id": subj_idx + 1,
                "signal_quality": quality,
                "repetitions": repetitions,
            }
        )

    return dataset


def save_dataset(dataset: List[Dict], filepath: str) -> None:
    """
    Save a dataset to a compressed .npz archive readable by
    :func:`sensie_eval.evaluate.load_dataset`.

    The archive uses a flat key structure:
      - ``subject_<id>_quality`` — scalar signal quality
      - ``subject_<id>_rep_<n>_accel`` / ``..._gyro`` — (3, N) streams
    """
    save_dict: Dict[str, np.ndarray] = {}
    for s in dataset:
        save_dict[f"subject_{s['subject_id']}_quality"] = np.array(s["signal_quality"])
        for r_idx, rep in enumerate(s["repetitions"]):
            for sensor in ("accel", "gyro"):
                key = f"subject_{s['subject_id']}_rep_{r_idx}_{sensor}"
                save_dict[key] = rep[sensor]

    np.savez_compressed(filepath, **save_dict)


def print_generation_summary(dataset: List[Dict], duration: float, n_repetitions: int) -> None:
    """Print summary statistics for a generated dataset."""
    qualities = [s["signal_quality"] for s in dataset]
    n_samples = dataset[0]["repetitions"][0]["accel"].shape[1]

    print(f"Generated {len(dataset)} subjects")
    print(f"  Duration: {duration}s @ 100 Hz -> {n_samples} samples/stream")
    print(f"  Repetitions per subject: {n_repetitions}")
    print(f"  Signal quality range: [{min(qualities):.3f}, {max(qualities):.3f}]")
    print(f"  Mean signal quality: {np.mean(qualities):.3f}")

    high_signal = sum(1 for q in qualities if q >= 0.6)
    low_signal = sum(1 for q in qualities if q < 0.4)
    print(f"  High-signal subjects (q >= 0.6): {high_signal}")
    print(f"  Low-signal subjects (q < 0.4):  {low_signal}")
    print(f"  Indeterminate subjects:         {len(dataset) - high_signal - low_signal}")
