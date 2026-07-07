"""Toolchain smoke test: proves pytest, imports, and packaging are wired correctly."""

import iotshield


def test_package_imports() -> None:
    assert iotshield.__version__ == "0.1.0"
