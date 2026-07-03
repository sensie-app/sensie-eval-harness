"""sensie-eval — subject-disjoint evaluation harness for motion-biomarker classification.

This package is client-side tooling only. It contains no model weights, no
proprietary feature definitions, and no production inference logic. The
bundled reliability scorer is a deliberately simple cross-repetition
correlation baseline used to demonstrate the evaluation *methodology* on
synthetic data.
"""

__version__ = "0.1.0"

from sensie_eval.evaluate import (
    build_json_output,
    classify_subject,
    compute_signal_reliability,
    evaluate_subject_disjoint,
    load_dataset,
    subject_disjoint_split,
)
from sensie_eval.generate import (
    add_subject_noise,
    generate_gesture_template,
    generate_subject_dataset,
    save_dataset,
)

__all__ = [
    "__version__",
    "add_subject_noise",
    "build_json_output",
    "classify_subject",
    "compute_signal_reliability",
    "evaluate_subject_disjoint",
    "generate_gesture_template",
    "generate_subject_dataset",
    "load_dataset",
    "save_dataset",
    "subject_disjoint_split",
]
