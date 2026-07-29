"""Shared locations for the test suite.

`Path(__file__).parents[2]` was spelled out under three different names across the suite;
the ones that mean "the demo inputs" now say so once.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "data" / "demo"
