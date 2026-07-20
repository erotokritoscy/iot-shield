"""The evaluation-plane switch, in a module with no eval imports of its own.

Deliberately separate from ``api/evaluation.py``: the Pipeline API must be able to ask
"is the eval plane on?" without importing the eval package, so that package stays
genuinely deletable from a product image (ADR-0021 D1). Reading the flag and *using* it
are different concerns, and only one of them may depend on the harness.
"""

import os

EVAL_API_ENV = "IOTSHIELD_EVAL_API"


def eval_api_enabled() -> bool:
    """Whether this deployment serves the benchmarking endpoints. Off unless opted in."""
    return os.environ.get(EVAL_API_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
