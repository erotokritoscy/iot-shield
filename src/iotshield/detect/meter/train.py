"""Offline fitting for the per-meter detectors (ADR-0030 D6, ADR-0048 D3).

Does exactly what a deployment does: read each meter's **known-clean history**, normalize it
through the real ingest path, and fit. Going through the normalizer rather than reading the CSV
directly is deliberate — it means an artifact can only ever be fitted on events shaped the way
production events are shaped, so a change to the normalizer cannot silently invalidate the model.

With no ``--name`` it regenerates the committed demo artifact. With one, it writes a fit to the
deployment's shelf, where runs and the dashboard's artifact dropdown find it by that name:

    uv run python -m iotshield.detect.meter.train                       # the demo artifact
    uv run python -m iotshield.detect.meter.train --name wbn-2025 --preset wbn_fs \\
        --source wbn.csv --source-format sensipro-hourly-csv --until 2025-09-01

A fit stores the clean history it learned from, so a shelf file is operator data and is gitignored.
``--until`` is how a fit stays out of the period it will judge. Nothing here checks that the history
really is clean: a fault inside it teaches the model that the fault is normal.

There is no separate ``train_state`` pure function here (team3 has one because its CLI and
its ``fit()`` had to be kept in step). Here ``fit()`` *is* the single definition, and this
module is a thin driver over it: read -> normalize -> ``fit`` -> ``state_for`` -> write.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from iotshield._paths import REPO_ROOT
from iotshield.contracts import NormalizedEvent
from iotshield.detect._artifacts import packaged_artifact_ids
from iotshield.detect.meter.artifact import _ARTIFACTS_PACKAGE, MeterArtifact, models_root
from iotshield.detect.meter.detector import MeterIForestDetector, PerMeterDetector
from iotshield.detect.meter.lof import _ARTIFACTS_PACKAGE as _LOF_ARTIFACTS_PACKAGE
from iotshield.detect.meter.lof import LofArtifact, MeterLofDetector
from iotshield.detect.meter.params import LOF_METHOD, METHOD, LofParams, MeterParams
from iotshield.normalize.meter import EOA_METER_CSV, MeterNormalizer

_DEFAULT_SOURCE = REPO_ROOT / "data" / "demo" / "demo_meter_history.csv"
_DEFAULT_OUT = {
    METHOD: Path(__file__).resolve().parent / "artifacts" / "demo_meter.json",
    LOF_METHOD: Path(__file__).resolve().parent / "artifacts_lof" / "demo_meter.json",
}
_PACKAGES = {METHOD: _ARTIFACTS_PACKAGE, LOF_METHOD: _LOF_ARTIFACTS_PACKAGE}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

CALIBRATED: dict[str, dict[str, dict[str, object]]] = {
    METHOD: {
        "wbn_fs": {"n_estimators": 400, "max_samples": 512},
        "wbn_rsv": {"n_estimators": 200, "max_samples": 512},
        "lead": {"n_estimators": 100, "max_samples": 512},
    },
    LOF_METHOD: {
        "wbn_fs": {"n_neighbors": 168, "metric": "manhattan"},
        "wbn_rsv": {"n_neighbors": 168, "metric": "minkowski"},
        "sensipro_electricity": {"n_neighbors": 240, "metric": "chebyshev"},
        "lead": {"n_neighbors": 80, "metric": "chebyshev"},
    },
}
"""Upstream's per-source calibration (``outputs/*_optimal_calibration.json``), as starting points.

Only sources whose winning configuration our four features and static profile can express. EOA and
SensiPro chose a 12-feature set or an MSTL residual we do not carry, and SensiPro's choices did not
survive their own cross-validation (pAUC 0.76, cross-validated 0.35 +/- 0.43), so they are absent
rather than half-applied. Chosen on synthetic injections, so sweep before trusting (ADR-0048 D3).
"""


def _events(
    sources: Sequence[Path], source_format: str, until: datetime | None
) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []
    quarantined = 0
    for source in sources:
        result = MeterNormalizer().normalize(
            source.read_text(encoding="utf-8"), source_format=source_format, origin=str(source)
        )
        events += result.events
        quarantined += len(result.malformed)
    if until is not None:
        events = [event for event in events if event.timestamp < until]
    if not events:
        raise ValueError(
            f"{', '.join(map(str, sources))} produced no consumption events to fit "
            f"({quarantined} record(s) quarantined)"
        )
    return events


def fit_from_file(
    source: Path,
    params: MeterParams,
    *,
    source_format: str = EOA_METER_CSV.name,
) -> MeterIForestDetector:
    """Normalize a clean meter export and fit one model per meter it contains."""
    return MeterIForestDetector(params).fit(_events([source], source_format, None))


def _params(
    detector: str, preset: str | None, fp_budget: float | None, n_estimators: int | None
) -> MeterParams | LofParams:
    chosen: dict[str, object] = {}
    if preset is not None:
        known = CALIBRATED[detector]
        if preset not in known:
            raise ValueError(
                f"{detector} has no calibrated preset {preset!r}; known: {', '.join(sorted(known))}"
            )
        chosen.update(known[preset])
    if fp_budget is not None:
        chosen["fp_budget"] = fp_budget
    if n_estimators is not None:
        if detector != METHOD:
            raise ValueError("--n-estimators applies only to meter_iforest")
        chosen["n_estimators"] = n_estimators
    model = MeterParams if detector == METHOD else LofParams
    return model.model_validate(chosen)


def _shelf_path(detector: str, name: str, root: Path | None = None) -> Path:
    """Where a named fit goes, refusing a name that could not be read back as that name."""
    if not _NAME.match(name):
        raise ValueError(f"--name must be a plain identifier, got {name!r}")
    if name in packaged_artifact_ids(_PACKAGES[detector]):
        raise ValueError(f"{name!r} is a shipped {detector} artifact, which would shadow this fit")
    return models_root(root) / detector / f"{name}.json"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fit a per-meter detector's artifact.")
    parser.add_argument("--detector", choices=(METHOD, LOF_METHOD), default=METHOD)
    parser.add_argument(
        "--source", type=Path, nargs="+", default=[_DEFAULT_SOURCE], help="clean meter export(s)"
    )
    parser.add_argument(
        "--source-format",
        default=EOA_METER_CSV.name,
        help="which meter dialect the export is in",
    )
    parser.add_argument("--name", help="write to the deployment's shelf under this artifact id")
    parser.add_argument("--out", type=Path, help="artifact JSON output path, instead of a name")
    parser.add_argument("--preset", help="upstream's calibrated hyperparameters for a source")
    parser.add_argument("--until", help="fit only on readings before this UTC date (YYYY-MM-DD)")
    parser.add_argument("--fp-budget", type=float, default=None)
    parser.add_argument("--n-estimators", type=int, default=None)
    args = parser.parse_args(argv)
    if args.name and args.out:
        parser.error("--name and --out both say where to write; give one")

    params = _params(args.detector, args.preset, args.fp_budget, args.n_estimators)
    until = datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until else None
    out = (
        _shelf_path(args.detector, args.name)
        if args.name
        else args.out or _DEFAULT_OUT[args.detector]
    )
    events = _events(args.source, args.source_format, until)

    detector: PerMeterDetector
    if isinstance(params, MeterParams):
        detector = MeterIForestDetector(params).fit(events)
    else:
        detector = MeterLofDetector(params).fit(events)
    states = [detector.state_for(asset_id) for asset_id in detector.fitted_assets]
    artifact = (
        MeterArtifact(params=params, meters=states)
        if isinstance(params, MeterParams)
        else LofArtifact(params=params, meters=states)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": write_text otherwise translates to os.linesep, so regenerating on Windows
    # rewrote every line of a committed artifact as CRLF and buried a real one-value change in
    # a 3,000-line diff. `bundle.NEWLINE` records the same lesson for corpora (ADR-0028).
    out.write_text(artifact.model_dump_json(indent=2), encoding="utf-8", newline="\n")

    # Report from the models already in hand. Reading `artifact.fitted` here would re-fit
    # every meter from the rows just written — correct, but it doubles a fleet's training
    # time to print numbers this detector is holding.
    for asset_id in detector.fitted_assets:
        model = detector.model_for(asset_id)
        print(
            f"  meter {asset_id}: {len(model.training_rows)} clean hour(s), "
            f"threshold={model.threshold:.5f} (p{(1 - params.fp_budget) * 100:.2f})"
        )
    too_short = sorted({event.asset_id for event in events} - set(detector.fitted_assets))
    if too_short:
        print(f"  NOT fitted, history under {params.min_train_hours}h: {', '.join(too_short)}")
    print(f"wrote {out} — {len(states)} meter(s) at fp_budget={params.fp_budget:g}")


if __name__ == "__main__":
    main()
