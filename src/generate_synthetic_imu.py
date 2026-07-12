"""
Compatibility shim — the module moved to the sensie_eval package.

Use `from sensie_eval.generate_synthetic_imu import ...` (or
`pip install -e .` and import sensie_eval). This shim is not part of the
installed package.
"""

from sensie_eval.generate_synthetic_imu import *  # noqa: F401,F403
from sensie_eval.generate_synthetic_imu import main  # noqa: F401

if __name__ == "__main__":
    main()
