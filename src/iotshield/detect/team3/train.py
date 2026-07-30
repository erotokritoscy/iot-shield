"""Offline training for the team3 z-score detector (ADR-0012 D2/D3).

``train_state`` is a pure function (frame + hyperparameters in, ``Team3State`` out, no
I/O), so it serves both the ``main`` CLI that produces the committed artifact and the
detector's own ``fit()`` — which is what guarantees the two can never disagree about what
fitting means. It takes ``Team3Params`` rather than loose keyword arguments precisely
because that object *is* the hyperparameter set (ADR-0022): there is one place to add a
knob, and no way for a caller to pass half of them.

Two empirical findings shaped the defaults (see ADR-0012 D3; details in the
integration notes):
- **Train on the asset's own normal history, not a foreign clean recording.**
  Cross-file training on SKAB's ``anomaly-free.csv`` fails on valve runs (domain
  shift makes the threshold unreachable). We train on the leading *normal* prefix
  of the same recording (labelled ``anomaly == 0``), which is the realistic
  deployment pattern anyway.
- **Drop degenerate features.** A quantized/near-constant channel derivative has
  a MAD at the 1e-8 floor; robust-z then explodes on quantization noise and
  dominates the per-row max. Features whose training MAD is at/near the floor
  carry no robust signal and are dropped.

Run:
    uv run python -m iotshield.detect.team3.train
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from iotshield._paths import REPO_ROOT
from iotshield.detect.team3._features import build_multivariate_features
from iotshield.detect.team3._zscore import fit_robust_baseline, score_robust_z
from iotshield.detect.team3.artifact import Team3Artifact, Team3State
from iotshield.detect.team3.params import Team3Params

# SKAB's eight channels, in the order they appear in the dataset header.
SKAB_CHANNELS: tuple[str, ...] = (
    "Accelerometer1RMS",
    "Accelerometer2RMS",
    "Current",
    "Pressure",
    "Temperature",
    "Thermocouple",
    "Voltage",
    "Volume Flow RateRMS",
)
# A numerical-stability floor, not a modelling choice — hence a constant here rather than
# a hyperparameter: below it a feature's spread is at the MAD floor and carries no signal.
_DEFAULT_MIN_MAD = 1e-6

# The read-only reference tree sits beside the repo. We train on the normal prefix of an
# abrupt-anomaly valve run (ADR-0012 D3).
_DEFAULT_SOURCE = (
    REPO_ROOT.parent
    / "reference"
    / "team3-telemetry-ad"
    / "Datasets"
    / "SKAB"
    / "valve1"
    / "13.csv"
)
_DEFAULT_OUT = Path(__file__).resolve().parent / "artifacts" / "skab_zscore.json"

DEFAULT_ASSET_ID = "skab-testbed"
"""Which device the committed artifact's one model describes.

A model is filed under an asset id and only judges that asset (ADR-0034), so this value is
load-bearing rather than cosmetic: it is the id carried by ``data/demo/demo_team3.csv`` and
by every ``skab_*`` evaluation bundle, which is what keeps ``config/demo_team3.yaml`` and
the committed SKAB benchmarks running against the shipped artifact unchanged.
"""


def train_state(
    frame: pd.DataFrame,
    params: Team3Params,
    *,
    asset_id: str,
    channels: Sequence[str],
    provenance: str,
    min_mad: float = _DEFAULT_MIN_MAD,
) -> Team3State:
    """Fit one asset's robust baseline + threshold on ``frame`` (assumed all-normal).

    ``channels`` is passed separately rather than read from ``params.channels`` because it
    may have been *learned* from the training data when the caller left it unpinned — the
    resolution of "pinned or learned" belongs to the caller, and the answer lands in the
    returned state as this fit's schema (ADR-0022).

    ``asset_id`` names the device this baseline describes. It is an argument rather than
    something inferred from ``frame`` because by this point the frame is a plain matrix
    with no asset column — the caller (``detector.fit`` or ``main``) is the layer that
    still knows which device's rows these are.
    """
    channels = list(channels)
    matrix = frame.reindex(columns=channels)
    features = build_multivariate_features(
        matrix,
        window=params.window_size,
        lag_steps=params.lag_steps,
        fft_enabled=params.fft_enabled,
        fft_include_dc=params.fft_include_dc,
    )
    if features.empty:
        raise ValueError("training frame produced no feature rows (too few samples for the window)")

    all_columns = list(features.columns)
    values = features.to_numpy(dtype=float)
    baseline = fit_robust_baseline(values)

    keep = [i for i, m in enumerate(baseline["mad"]) if m > min_mad]
    if not keep:
        raise ValueError("every feature is degenerate (MAD at floor); check the training data")
    feature_columns = [all_columns[i] for i in keep]
    median = baseline["median"][keep]
    mad = baseline["mad"][keep]

    scores = score_robust_z(values[:, keep], {"median": median, "mad": mad})
    threshold = float(np.percentile(scores, params.percentile))

    return Team3State(
        asset_id=asset_id,
        provenance=provenance,
        channels=channels,
        feature_columns=feature_columns,
        median=median.tolist(),
        mad=mad.tolist(),
        threshold=threshold,
    )


def load_normal_frame(source: Path, channels: Sequence[str]) -> tuple[pd.DataFrame, int, int]:
    """Read a SKAB CSV and return (normal-prefix channel frame, prefix_rows, total_rows).

    If the file carries an ``anomaly`` label, only the leading contiguous normal
    block is used (training must never see the fault). A label-free file (e.g.
    ``anomaly-free.csv``) is used whole.
    """
    df = pd.read_csv(source, sep=";")
    missing = [c for c in channels if c not in df.columns]
    if missing:
        raise ValueError(f"{source} is missing expected channels: {missing}")
    total = len(df)
    if "anomaly" in df.columns and (df["anomaly"] == 1).any():
        first_anomaly = int(np.argmax(df["anomaly"].to_numpy() == 1))
        df = df.iloc[:first_anomaly]
    normal = pd.DataFrame(df[list(channels)]).reset_index(drop=True)
    return normal, len(df), total


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Train the team3 z-score artifact.")
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE, help="SKAB CSV")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="artifact JSON output path")
    parser.add_argument("--window", type=int, default=Team3Params().window_size)
    parser.add_argument("--percentile", type=float, default=Team3Params().percentile)
    parser.add_argument(
        "--asset",
        default=DEFAULT_ASSET_ID,
        help="which device this model describes; it judges only that asset (ADR-0034)",
    )
    args = parser.parse_args(argv)

    # The committed artifact pins SKAB's channel order explicitly: it ships as a
    # ready-to-run model for that rig, so its schema is a choice, not a discovery.
    params = Team3Params(
        window_size=args.window, percentile=args.percentile, channels=SKAB_CHANNELS
    )
    frame, prefix_rows, total_rows = load_normal_frame(args.source, SKAB_CHANNELS)
    source_label = f"{args.source.parent.name}/{args.source.name}"
    state = train_state(
        frame,
        params,
        asset_id=args.asset,
        channels=SKAB_CHANNELS,
        provenance=(
            f"team3-telemetry-ad robust z-score; trained on the normal prefix "
            f"({prefix_rows}/{total_rows} rows) of SKAB {source_label} (ADR-0012)"
        ),
    )
    artifact = Team3Artifact(params=params, models=[state])
    # newline="\n": see the note in detect/meter/train.py — write_text translates to
    # os.linesep, so a Windows regeneration rewrites every line of a committed artifact.
    args.out.write_text(artifact.model_dump_json(indent=2), encoding="utf-8", newline="\n")
    print(
        f"wrote {args.out} — {len(state.feature_columns)} features for {state.asset_id} "
        f"(from {prefix_rows} normal rows), threshold={state.threshold:.4f}"
        f" (p{params.percentile:g})"
    )


if __name__ == "__main__":
    main()
