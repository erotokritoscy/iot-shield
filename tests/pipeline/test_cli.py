from pathlib import Path

import pytest

from iotshield.pipeline.__main__ import main

# Self-contained fixture (test modules are not importable from each other):
# two good lines, one above threshold -> 1 detection -> 1 alert.
INLINE_CSV = """\
2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0
2026-07-07T12:01:00+00:00,pump-02,Pressure,96.0
"""

CONFIG_YAML = """\
database_url: "sqlite:///{db}"
source_format: stub-csv
detectors:
  - name: stub_threshold
"""


def write_files(tmp_path: Path, config_text: str | None = None) -> tuple[Path, Path]:
    input_path = tmp_path / "input.csv"
    input_path.write_text(INLINE_CSV, encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    text = (config_text or CONFIG_YAML).format(db=(tmp_path / "cli.db").as_posix())
    config_path.write_text(text, encoding="utf-8")
    return input_path, config_path


def test_successful_run_prints_report_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_path, config_path = write_files(tmp_path)
    assert main(["--input", str(input_path), "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "Pipeline run complete" in out
    assert "alerts: 1" in out


def test_missing_input_file_exits_two_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, config_path = write_files(tmp_path)
    assert main(["--input", str(tmp_path / "nope.csv"), "--config", str(config_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_unknown_adapter_exits_two_with_known_alternatives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = CONFIG_YAML.replace("stub_threshold", "no_such_detector")
    input_path, config_path = write_files(tmp_path, config_text=bad)
    assert main(["--input", str(input_path), "--config", str(config_path)]) == 2
    assert "stub_threshold" in capsys.readouterr().err  # error lists what IS available
