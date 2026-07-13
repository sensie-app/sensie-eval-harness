"""
sensie_eval — Subject-disjoint evaluation harness for motion-biomarker
classification, with an optional client for the Sensie live API.

Public API:
    generate_subject_dataset, generate_gesture_template, add_subject_noise
    load_dataset, compute_signal_reliability, subject_disjoint_split,
    classify_subject, evaluate_subject_disjoint, print_report
"""

__version__ = "0.1.2"

from sensie_eval.generate_synthetic_imu import (
    generate_gesture_template,
    add_subject_noise,
    generate_subject_dataset,
)
from sensie_eval.evaluate import (
    load_dataset,
    compute_signal_reliability,
    subject_disjoint_split,
    classify_subject,
    evaluate_subject_disjoint,
    print_report,
)

__all__ = [
    "__version__",
    "generate_gesture_template",
    "add_subject_noise",
    "generate_subject_dataset",
    "load_dataset",
    "compute_signal_reliability",
    "subject_disjoint_split",
    "classify_subject",
    "evaluate_subject_disjoint",
    "print_report",
]
