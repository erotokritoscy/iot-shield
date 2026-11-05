"""LEAD conversion: a tiny train.csv through the real fetch, bundle and normalizer."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from iotshield.eval.corpus import CorpusUnavailable, load_bundle
from iotshield.eval.fetch import fetch, fetch_hint
from iotshield.normalize.meter import MeterNormalizer

T0 = datetime(2016, 1, 1, tzinfo=UTC)


def write_train(directory: Path, buildings: int = 11, hours: int = 24) -> None:
    lines = ["building_id,timestamp,meter_reading,anomaly"]
    for building in range(1, buildings + 1):
        for hour in range(hours):
            stamp = (T0 + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S")
            # Hours 3-4 are blank (a gap) though flagged; hours 10-11 are labelled anomalous.
            reading = "" if hour in (3, 4) else str(float(building + hour))
            flag = 1 if hour in (3, 4, 10, 11) else 0
            lines.append(f"{building},{stamp},{reading},{flag}")
    (directory / "train.csv").write_text("\n".join(lines) + "\n")


def test_each_building_is_a_case_and_the_sample_takes_every_tenth(tmp_path: Path) -> None:
    write_train(tmp_path)
    root = tmp_path / "eval"
    assert fetch("lead", root, source=tmp_path) == [("lead", 11), ("lead_sample", 2)]
    assert load_bundle(root / "lead").licence


def test_the_bundle_reads_back_as_the_readings_it_was_given(tmp_path: Path) -> None:
    write_train(tmp_path, buildings=1)
    root = tmp_path / "eval"
    fetch("lead", root, source=tmp_path)
    case = load_bundle(root / "lead").cases[0]
    result = MeterNormalizer().normalize(
        case.inputs[0].text, source_format=case.inputs[0].source_format, origin="t"
    )
    assert not result.malformed
    # 24 hours less the blank two, including the first hour and the one after the gap.
    assert len(result.events) == 22
    assert result.events[0].timestamp == T0
    assert result.events[3].timestamp == T0 + timedelta(hours=5)
    assert result.events[3].value == pytest.approx(6.0)
    assert result.events[0].asset_id == "LEAD_1"
    assert (result.events[0].metric or "").endswith("kwh")


def test_the_labels_cover_observed_hours_only(tmp_path: Path) -> None:
    write_train(tmp_path, buildings=1)
    root = tmp_path / "eval"
    fetch("lead", root, source=tmp_path)
    truth = load_bundle(root / "lead").cases[0].truth
    asset = truth.of("LEAD_1")
    assert asset is not None
    # The flags on the blank hours go with them, leaving the one real incident.
    assert len(truth.incidents) == 1
    assert truth.incidents[0].start == T0 + timedelta(hours=10)
    assert truth.incidents[0].end == T0 + timedelta(hours=11)
    assert len(asset.clock) == 22


def test_a_reading_a_counter_cannot_carry_leaves_the_answer_key_too(tmp_path: Path) -> None:
    lines = ["building_id,timestamp,meter_reading,anomaly"]
    for hour in range(24):
        stamp = (T0 + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S")
        reading = "" if hour == 3 else "1.0"
        lines.append(f"1,{stamp},{reading},0")
    (tmp_path / "train.csv").write_text("\n".join(lines) + "\n")
    root = tmp_path / "eval"
    fetch("lead", root, source=tmp_path)
    case = load_bundle(root / "lead").cases[0]
    asset = case.truth.of("LEAD_1")
    assert asset is not None
    events = (
        MeterNormalizer()
        .normalize(case.inputs[0].text, source_format=case.inputs[0].source_format, origin="t")
        .events
    )
    assert {event.timestamp for event in events} == set(asset.clock)
    assert T0 + timedelta(hours=4) not in asset.clock


def test_a_directory_without_the_file_says_where_to_get_it(tmp_path: Path) -> None:
    with pytest.raises(CorpusUnavailable, match=r"train\.csv"):
        fetch("lead", tmp_path / "eval", source=tmp_path)


def test_an_unfetched_corpus_says_how_to_fetch_it() -> None:
    assert "fetch lead" in (fetch_hint("lead_sample") or "")
