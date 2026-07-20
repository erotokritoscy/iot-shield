"""Chart builder units: the timezone normalization that keeps the axis in UTC,
matching the tables beside it (golden rule 4)."""

from datetime import datetime, timedelta, timezone

from iotshield.dashboard.charts import _utc_wallclock, sweep_curve


def test_utc_wallclock_drops_tz_after_normalizing_to_utc() -> None:
    athens = datetime(2026, 7, 7, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    wall = _utc_wallclock(athens)
    assert wall.tzinfo is None  # naive, so Vega renders it without a local shift
    assert wall == datetime(2026, 7, 7, 12, 0, 0)  # ... showing the UTC wall-clock time


class TestSweepCurve:
    """A sweep gets a curve; anything else falls back to the bar chart (ADR-0022).

    The judgment being pinned: a swept hyperparameter is a *quantitative* axis, and a bar
    chart of `team3[window_size=20]`, `…=60`, `…=120` makes the reader decode the ordering
    out of the labels. The fallback conditions matter as much as the happy path — a chart
    that silently drew unrelated specs as a "curve" would invent a trend.
    """

    def _rows(self, *labels: str) -> list[dict]:
        return [{"spec": label, "event_f1": 0.5} for label in labels]

    def test_a_single_parameter_sweep_becomes_a_curve(self) -> None:
        rows = self._rows("team3[window_size=20]", "team3[window_size=60]")
        assert sweep_curve(rows) is not None

    def test_ordinary_head_to_head_labels_are_not_a_sweep(self) -> None:
        assert sweep_curve(self._rows("team3", "stub")) is None

    def test_a_mix_of_swept_and_plain_specs_is_not_a_sweep(self) -> None:
        assert sweep_curve(self._rows("team3[window_size=20]", "stub")) is None

    def test_two_swept_parameters_would_be_a_surface_not_a_line(self) -> None:
        rows = self._rows("t[window_size=20,percentile=99]", "t[window_size=60,percentile=99]")
        assert sweep_curve(rows) is None

    def test_a_non_numeric_sweep_has_no_meaningful_axis_order(self) -> None:
        assert sweep_curve(self._rows("log[preset=auth]", "log[preset=sev]")) is None

    def test_one_point_is_not_a_curve(self) -> None:
        assert sweep_curve(self._rows("team3[window_size=20]")) is None

    def test_undefined_scores_are_dropped_never_drawn_as_zero(self) -> None:
        # The ADR-0021 D2 rule the eval charts share: a bar at zero would claim a detector
        # scored nothing when the truth is that nothing was measurable.
        rows = [
            {"spec": "team3[window_size=20]", "event_f1": None},
            {"spec": "team3[window_size=60]", "event_f1": None},
        ]
        assert sweep_curve(rows) is None
