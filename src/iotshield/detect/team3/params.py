"""The team3 z-score detector's HYPERPARAMETERS — the half a human chooses (ADR-0022).

Everything here is settable from a config's ``params:``, reported by ``/modules``, and
sweepable by the evaluation harness. Nothing here is learned. Its counterpart is
``artifact.Team3State``, which holds only what ``fit()`` computes.

Frozen because a detector must not be able to retune itself mid-run: the port's
determinism law and ``fit``'s return-a-new-detector rule both assume construction is
final.
"""

from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _sequence(value: Any) -> Any:
    """Accept a comma-separated string wherever a sequence is expected.

    A config file yields a real list, but the dashboard's generic param widgets are text
    boxes, so ``"1, 5, 10"`` has to mean the same thing as ``[1, 5, 10]``. Brackets and
    parens are tolerated so a value copied out of an error message or a rendered default
    round-trips. Everything else — including the per-item ``int``/``str`` coercion — is
    pydantic's job; this only handles the shape it cannot guess.

    On the model rather than the registry factory so it holds for *every* path into these
    hyperparameters: config, the artifact loader, and a test building params directly.
    """
    if isinstance(value, str):
        return [part.strip() for part in value.strip(" []()").split(",") if part.strip()]
    return value


Lags = Annotated[tuple[int, ...], BeforeValidator(_sequence)]
Channels = Annotated[tuple[str, ...], BeforeValidator(_sequence)]

METHOD = "team3_zscore"
"""The judging method stamped on every detection; also this adapter's registry key.

A constant rather than an artifact field: ``method`` names *what judged*, which is the
adapter, and is identical whether the baseline came off disk or from ``fit()``.
"""

DEFAULT_WINDOW = 60
DEFAULT_LAGS = (1, 5, 10)
DEFAULT_PERCENTILE = 99.0


class Team3Params(BaseModel):
    """Feature construction, then the decision rule — in that order.

    ``channels`` is the one that is *optionally* learned: pinning it restricts the
    detector to a fixed set (and refuses input missing any of them), while leaving it
    unset lets ``fit()`` take whatever channels the training data actually carries. That
    is the difference between "watch exactly these eight SKAB channels" and "learn this
    rig", and both are legitimate — so it is a hyperparameter with a learned default,
    the same shape as sklearn's ``feature_names_in_``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_size: int = Field(default=DEFAULT_WINDOW, ge=2, description="Rolling-feature window")
    lag_steps: Lags = DEFAULT_LAGS
    fft_enabled: bool = False
    fft_include_dc: bool = False
    percentile: float = Field(default=DEFAULT_PERCENTILE, gt=0.0, lt=100.0)
    channels: Channels | None = Field(
        default=None, description="Pin the channel set; unset = learn it from the training data"
    )
    min_segment_length: int = Field(default=1, ge=1, description="Shortest reportable episode")

    @model_validator(mode="after")
    def _lags_fit_the_window(self) -> Self:
        # A lag at or beyond the window would index outside the rolling frame; caught here
        # so it is a config error at construction, never a runtime shape mismatch.
        if any(lag >= self.window_size for lag in self.lag_steps):
            raise ValueError(
                f"every lag step must be smaller than window_size ({self.window_size}); "
                f"got {list(self.lag_steps)}"
            )
        return self
