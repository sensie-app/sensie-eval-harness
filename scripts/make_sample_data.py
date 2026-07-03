"""Regenerate the bundled sample dataset at src/sensie_eval/data/sample_dataset.npz.

The sample is fully synthetic (anonymized by construction — no human data
was ever involved) and deterministic: re-running this script reproduces the
committed file byte-for-byte modulo zip timestamps. Arrays are stored as
float32 to keep the repository small.
"""

import os

import numpy as np

from sensie_eval.generate import generate_subject_dataset, save_dataset

OUT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "sensie_eval", "data", "sample_dataset.npz"
)

N_SUBJECTS = 32
N_REPETITIONS = 3
DURATION = 3.2
BASE_NOISE = 0.4
SEED = 13


def main() -> None:
    dataset = generate_subject_dataset(
        n_subjects=N_SUBJECTS,
        n_repetitions=N_REPETITIONS,
        duration=DURATION,
        sample_rate=100.0,
        base_noise=BASE_NOISE,
        seed=SEED,
    )
    # Downcast to float32: halves file size, negligible effect on metrics
    for subject in dataset:
        for rep in subject["repetitions"]:
            for sensor in ("accel", "gyro"):
                rep[sensor] = rep[sensor].astype(np.float32)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    save_dataset(dataset, OUT_PATH)
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"Wrote {OUT_PATH} ({size_kb:.0f} KiB, {N_SUBJECTS} subjects)")


if __name__ == "__main__":
    main()
