"""The Compose wizard's sample library (scripts/make_sample_data.py).

The wizard tells an operator what each scenario will find. These tests are what make that a
fact rather than a caption: every scenario is run through the real pipeline and must produce
exactly the counts its manifest entry promises, and both committed copies must be what the
generator writes today.
"""

import importlib.util
import json
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from iotshield._paths import REPO_ROOT
from iotshield.pipeline.config import PipelineSpec
from iotshield.pipeline.registry import DETECTORS, NORMALIZERS
from iotshield.pipeline.runner import compute_run

COPIES = (REPO_ROOT / "data" / "samples", REPO_ROOT / "web" / "public" / "samples")


def _generator() -> ModuleType:
    path = REPO_ROOT / "scripts" / "make_sample_data.py"
    spec = importlib.util.spec_from_file_location("make_sample_data", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANIFEST: dict[str, Any] = json.loads((COPIES[0] / "manifest.json").read_text(encoding="utf-8"))
SAMPLES = {sample["id"]: sample for sample in MANIFEST["samples"]}


@pytest.mark.parametrize("copy", COPIES, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_each_committed_copy_is_what_the_generator_writes(copy: Path) -> None:
    # Regenerate with `uv run python scripts/make_sample_data.py` if this fails.
    for name, text in _generator().files().items():
        assert (copy / name).read_text(encoding="utf-8") == text, f"{copy / name} is stale"


def test_every_sample_names_a_real_normalizer_and_format() -> None:
    for sample in MANIFEST["samples"]:
        assert sample["normalizer"] in NORMALIZERS, sample["id"]
        assert (COPIES[0] / sample["file"]).is_file(), sample["file"]


@pytest.mark.parametrize("scenario", MANIFEST["scenarios"], ids=lambda s: s["id"])
def test_each_scenario_produces_exactly_what_it_promises(scenario: dict[str, Any]) -> None:
    sources = [SAMPLES[sample_id] for sample_id in scenario["samples"]]
    assert all(detector["name"] in DETECTORS for detector in scenario["detectors"])
    spec = PipelineSpec.model_validate(
        {
            "sources": [
                {"normalizer": s["normalizer"], "source_format": s["source_format"]}
                for s in sources
            ],
            "detectors": scenario["detectors"],
            "alert_engine": scenario["alert_engine"],
        }
    )
    result = compute_run(
        spec,
        [(COPIES[0] / s["file"]).read_text(encoding="utf-8") for s in sources],
        origins=[s["file"] for s in sources],
    )
    got = {
        "events": len(result.events),
        "malformed": len(result.malformed),
        "detections": len(result.detections),
        "alerts": len(result.alerts),
    }
    assert got == scenario["expected"]
    # A scenario must be runnable as shipped: no guard warning, no "rejected 100% of input".
    assert [w for w in result.warnings if "rejected" in w] == []


def test_the_quiet_control_is_quiet_with_every_detector_on() -> None:
    quiet = next(s for s in MANIFEST["scenarios"] if s["id"] == "quiet")
    assert quiet["expected"]["detections"] == 0
    assert {d["name"] for d in quiet["detectors"]} >= {"rules", "flow_burst", "log_burst"}


def test_the_stuck_meter_scenario_finds_what_its_description_says() -> None:
    scenario = next(s for s in MANIFEST["scenarios"] if s["id"] == "stuck_meter")
    sample = SAMPLES["stuck_meter"]
    spec = PipelineSpec.model_validate(
        {
            "sources": [
                {"normalizer": sample["normalizer"], "source_format": sample["source_format"]}
            ],
            "detectors": scenario["detectors"],
            "alert_engine": scenario["alert_engine"],
        }
    )
    text = (COPIES[0] / sample["file"]).read_text(encoding="utf-8")
    detections = compute_run(spec, [text], origins=[sample["file"]]).detections

    by_method = Counter(detection.method for detection in detections)
    assert by_method == {"meter_baseline": 2, "meter_lof": 6}  # meter_iforest: silent, as it says
    baseline = {
        str(detection.evidence["signature"]): detection.window_start.isoformat()
        for detection in detections
        if detection.method == "meter_baseline"
    }
    assert baseline == {
        "stuck": "2026-05-19T03:00:00+00:00",  # inside the freeze
        "spike": "2026-05-20T09:00:00+00:00",  # the hour the counter catches up
    }
