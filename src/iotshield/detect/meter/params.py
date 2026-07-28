"""The meter detector's HYPERPARAMETERS — the half a human chooses (ADR-0022).

Everything here is settable from a config's ``params:``, reported by ``/modules``, and
sweepable by the evaluation harness. Nothing here is learned; its counterpart is
``artifact.MeterState``, which holds only what ``fit()`` computes.

The one worth understanding is ``fp_budget``. It is *not* a score threshold — a score
threshold is a calibrated value, and typing one in is how a detector ends up either mute or
screaming. It is the **false-alarm budget**: the fraction of known-clean hours an operator
is willing to see alarm. Fitting turns it into an absolute score threshold on that meter's
own clean history. So the human chooses the thing a human actually knows (how many pages
per month are tolerable) and the data computes the thing only data can know.

That division is also what settled a disagreement with the upstream author, who declined
to expose a threshold parameter on the grounds that it is calibrated rather than free. He
is right, and this is the shape that agrees with him: no threshold input, and the
calibration stays reproducible on our side from a budget plus clean history.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from iotshield.detect.meter._features import WEEK_HOURS
from iotshield.detect.meter._iforest import (
    DEFAULT_MAX_SAMPLES,
    DEFAULT_N_ESTIMATORS,
    DEFAULT_RANDOM_STATE,
)

METHOD = "meter_iforest"
"""The judging method stamped on every detection; also this adapter's registry key."""

DEFAULT_FP_BUDGET = 0.0014
"""~1 false alarm per meter-month (730 hours), the upstream project's operating budget."""


class MeterParams(BaseModel):
    """Feature construction, then the model, then the decision rule — in that order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_window_hours: int = Field(
        default=WEEK_HOURS,
        ge=24,
        description="Rolling-statistics window in hours (168 = one week)",
    )
    n_estimators: int = Field(
        default=DEFAULT_N_ESTIMATORS, ge=10, description="Trees in the forest"
    )
    max_samples: int = Field(
        default=DEFAULT_MAX_SAMPLES,
        ge=16,
        description="Rows subsampled per tree; capped at the training rows available",
    )
    fp_budget: float = Field(
        default=DEFAULT_FP_BUDGET,
        gt=0.0,
        lt=1.0,
        description="Tolerated false-alarm rate on clean hours; 0.0014 ~ 1 alarm/meter-month",
    )
    min_episode_hours: int = Field(
        default=1, ge=1, description="Shortest reportable run of anomalous hours"
    )
    min_train_hours: int = Field(
        default=WEEK_HOURS * 2,
        ge=WEEK_HOURS,
        description="Refuse to fit a meter with less clean history than this",
    )
    random_state: int = Field(
        default=DEFAULT_RANDOM_STATE, description="Seed, so a fit is reproducible"
    )

    @model_validator(mode="after")
    def _training_can_fill_the_window(self) -> Self:
        # A rolling window longer than the training history yields an all-NaN feature and
        # therefore zero usable rows. Caught here so it is a config error at construction
        # rather than an opaque "no feature rows" failure during a fit.
        if self.min_train_hours < self.profile_window_hours:
            raise ValueError(
                f"min_train_hours ({self.min_train_hours}) must be at least "
                f"profile_window_hours ({self.profile_window_hours}); a shorter history "
                "cannot fill the rolling window and would yield no usable feature rows"
            )
        return self
