"""Offline fitting for the meter detector (ADR-0028 D6).

Produces the committed artifact by doing exactly what a deployment would do: read a
meter's **known-clean history**, normalize it through the real ingest path, and fit. Going
through the normalizer rather than reading the CSV directly is deliberate — it means the
artifact can only ever be fitted on events shaped the way production events are shaped, so
a change to the normalizer cannot silently invalidate the model.

There is no separate ``train_state`` pure function here (team3 has one because its CLI and
its ``fit()`` had to be kept in step). Here ``fit()`` *is* the single definition, and this
module is a thin driver over it: read -> normalize -> ``fit`` -> ``state_for`` -> write.

Run:
    uv run python -m iotshield.detect.meter.train
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from iotshield.detect.meter.artifact import MeterArtifact
from iotshield.detect.meter.detector import MeterIForestDetector
from iotshield.detect.meter.params import MeterParams
from iotshield.normalize.meter import EOA_METER_CSV, MeterNormalizer

# Repo layout: src/iotshield/detect/meter/train.py -> parents[4] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_SOURCE = _REPO_ROOT / "data" / "demo" / "demo_meter_history.csv"
_DEFAULT_OUT = Path(__file__).resolve().parent / "artifacts" / "demo_meter.json"


def fit_from_file(
    source: Path,
    params: MeterParams,
    *,
    source_format: str = EOA_METER_CSV.name,
) -> MeterIForestDetector:
    """Normalize a clean meter export and fit one model per meter it contains."""
    text = source.read_text(encoding="utf-8")
    result = MeterNormalizer().normalize(text, source_format=source_format, origin=str(source))
    if not result.events:
        raise ValueError(
            f"{source} produced no consumption events "
            f"({len(result.malformed)} record(s) quarantined); nothing to fit"
        )
    return MeterIForestDetector(params).fit(result.events)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fit the meter detector artifact.")
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE, help="clean meter export")
    parser.add_argument(
        "--source-format",
        default=EOA_METER_CSV.name,
        help="which meter dialect the export is in",
    )
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="artifact JSON output path")
    parser.add_argument("--fp-budget", type=float, default=MeterParams().fp_budget)
    parser.add_argument("--n-estimators", type=int, default=MeterParams().n_estimators)
    args = parser.parse_args(argv)

    params = MeterParams(fp_budget=args.fp_budget, n_estimators=args.n_estimators)
    detector = fit_from_file(args.source, params, source_format=args.source_format)
    states = [detector.state_for(asset_id) for asset_id in detector.fitted_assets]
    args.out.write_text(
        MeterArtifact(params=params, meters=states).model_dump_json(indent=2), encoding="utf-8"
    )

    # Report from the models already in hand. Reading `artifact.fitted` here would re-fit
    # every meter from the rows just written — correct, but it doubles a fleet's training
    # time to print numbers this detector is holding.
    for asset_id in detector.fitted_assets:
        model = detector.model_for(asset_id)
        print(
            f"  meter {asset_id}: {len(model.training_rows)} clean hour(s), "
            f"threshold={model.threshold:.5f} (p{(1 - params.fp_budget) * 100:.2f})"
        )
    print(f"wrote {args.out} — {len(states)} meter(s) at fp_budget={params.fp_budget:g}")


if __name__ == "__main__":
    main()
