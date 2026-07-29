"""Hermetic team3 detectors, built in memory — never the committed artifact.

Three suites need "a team3 detector that works": the adapter tests, the fit-protocol
tests, and the detector port suite. Each used to grow its own copy of the same twenty
lines, which is how they quietly drifted apart on window size and channel set.

Hermetic on purpose: a suite that loaded ``artifacts/skab_zscore.json`` would be testing
that file's numbers rather than the code, and would fail for the wrong reason the next
time the model is retrained. The committed artifact has its own test, which is the only
place its values are pinned.
"""

from collections.abc import Sequence

import numpy as np
import pandas as pd

from iotshield.detect.team3.artifact import Team3State
from iotshield.detect.team3.detector import Team3ZScoreDetector
from iotshield.detect.team3.params import Team3Params
from iotshield.detect.team3.train import train_state


def normal_frame(channels: Sequence[str], rows: int = 300, seed: int = 0) -> pd.DataFrame:
    """A quiet multi-channel recording: what "normal" looks like to this model."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame({channel: rng.normal(10.0, 0.5, rows) for channel in channels})


FIXTURE_ASSETS = ("pump-02", "skab-testbed")
"""The asset ids the suites build events for.

A model judges only the asset it was fitted for (ADR-0034), so a fixture detector has to
name its assets or every suite's events would be skipped. Two rather than one because the
port suite's generic ``event()`` says ``pump-02`` while the adapter tests' ``events_from``
says ``skab-testbed``, and unifying them would churn both suites to no benefit.
"""


def make_state(params: Team3Params, *, asset_id: str, rows: int = 300, seed: int = 0) -> Team3State:
    channels = params.channels or ("Pressure",)
    return train_state(
        normal_frame(channels, rows, seed),
        params,
        asset_id=asset_id,
        channels=channels,
        provenance="hermetic test fixture",
    )


def make_detector(
    channels: Sequence[str] = ("Pressure",),
    *,
    assets: Sequence[str] = FIXTURE_ASSETS,
    window: int = 5,
    lags: tuple[int, ...] = (1,),
    percentile: float = 99.0,
    min_segment_length: int = 1,
    seed: int = 0,
) -> Team3ZScoreDetector:
    """A fitted team3 detector over ``channels``, with a tiny window so cases stay small.

    The same baseline is filed under each of ``assets``. That is a fixture convenience, not
    a claim that one baseline suits several devices — real fits learn each asset separately
    (``Team3ZScoreDetector.fit``); here the point is only that the named assets are
    judgeable.

    Fitted once and re-filed rather than refitted per asset: the inputs are identical, so
    every extra ``train_state`` produced a byte-identical state under a different id (28
    duplicate fits, ~39 ms across the suite).
    """
    params = Team3Params(
        window_size=window,
        lag_steps=lags,
        percentile=percentile,
        channels=tuple(channels),
        min_segment_length=min_segment_length,
    )
    state = make_state(params, asset_id=assets[0], seed=seed)
    return Team3ZScoreDetector(
        params,
        {asset: state.model_copy(update={"asset_id": asset}) for asset in assets},
    )
