"""Offline training for the team3 z-score detector (ADR-0012 D2/D3).

``train_artifact`` is a pure function (frame in, ``Team3Artifact`` out, no I/O) so
the port suite can build a hermetic in-memory artifact on synthetic data. The
``main`` CLI is the one-time producer of the committed artifact.

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

from iotshield.detect.team3._features import build_multivariate_features
from iotshield.detect.team3._zscore import fit_robust_baseline, score_robust_z
from iotshield.detect.team3.artifact import FftConfig, Team3Artifact

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
_DEFAULT_LAGS = (1, 5, 10)
_DEFAULT_WINDOW = 60
_DEFAULT_PERCENTILE = 99.0
_DEFAULT_MIN_MAD = 1e-6  # below this a feature's spread is at the MAD floor -> degenerate
_METHOD = "team3_zscore"  # the detector name; also the registry key for this adapter

# Repo layout: src/iotshield/detect/team3/train.py -> parents[4] is the iotshield
# repo root; the read-only reference tree sits beside it. We train on the normal
# prefix of an abrupt-anomaly valve run (ADR-0012 D3).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_SOURCE = (
    _REPO_ROOT.parent
    / "reference"
    / "team3-telemetry-ad"
    / "Datasets"
    / "SKAB"
    / "valve1"
    / "13.csv"
)
_DEFAULT_OUT = Path(__file__).resolve().parent / "artifacts" / "skab_zscore.json"


def train_artifact(
    frame: pd.DataFrame,
    *,
    channels: Sequence[str],
    window_size: int = _DEFAULT_WINDOW,
    lag_steps: Sequence[int] = _DEFAULT_LAGS,
    fft_enabled: bool = False,
    fft_include_dc: bool = False,
    percentile: float = _DEFAULT_PERCENTILE,
    min_mad: float = _DEFAULT_MIN_MAD,
    provenance: str = "team3-telemetry-ad robust z-score (ADR-0012)",
) -> Team3Artifact:
    """Fit the robust baseline + threshold on ``frame`` (assumed all-normal)."""
    channels = list(channels)
    matrix = frame.reindex(columns=channels)
    features = build_multivariate_features(
        matrix,
        window=window_size,
        lag_steps=lag_steps,
        fft_enabled=fft_enabled,
        fft_include_dc=fft_include_dc,
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
    threshold = float(np.percentile(scores, percentile))

    return Team3Artifact(
        method=_METHOD,
        provenance=provenance,
        window_size=window_size,
        lag_steps=list(lag_steps),
        fft=FftConfig(enabled=fft_enabled, include_dc=fft_include_dc),
        channels=channels,
        feature_columns=feature_columns,
        median=median.tolist(),
        mad=mad.tolist(),
        threshold=threshold,
        percentile=percentile,
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
    parser.add_argument("--window", type=int, default=_DEFAULT_WINDOW)
    parser.add_argument("--percentile", type=float, default=_DEFAULT_PERCENTILE)
    args = parser.parse_args(argv)

    frame, prefix_rows, total_rows = load_normal_frame(args.source, SKAB_CHANNELS)
    source_label = f"{args.source.parent.name}/{args.source.name}"
    artifact = train_artifact(
        frame,
        channels=SKAB_CHANNELS,
        window_size=args.window,
        percentile=args.percentile,
        provenance=(
            f"team3-telemetry-ad robust z-score; trained on the normal prefix "
            f"({prefix_rows}/{total_rows} rows) of SKAB {source_label} (ADR-0012)"
        ),
    )
    args.out.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"wrote {args.out} — {len(artifact.feature_columns)} features "
        f"(from {prefix_rows} normal rows), threshold={artifact.threshold:.4f}"
        f" (p{artifact.percentile:g})"
    )


if __name__ == "__main__":
    main()
