"""Toolchain smoke test: proves pytest, imports, and packaging are wired correctly."""

from importlib.metadata import version

import iotshield


def test_version_single_sourced() -> None:
    # __version__ is authored once in src/iotshield/__init__.py; the build derives
    # the distribution version from it. A mismatch means packaging is out of sync
    # (e.g. version bumped without re-syncing the environment).
    assert iotshield.__version__ == version("iotshield")
