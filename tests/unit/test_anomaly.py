"""Spend-anomaly maths. Pure stdlib, no repository, no clock.

Half of these assert that nothing fires. An anomaly detector's failure mode is
not missing a spike — it is crying wolf on seven days of noise until people
mute the channel, at which point it has also lost them the spike.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from finops_sentinel.domain.anomaly import detect_anomaly
from finops_sentinel.domain.models import SpendSnapshot

START = date(2026, 7, 1)


def _series(values: list[str]) -> list[SpendSnapshot]:
    """One snapshot per consecutive day, oldest first."""
    return [
        SpendSnapshot(
            snapshot_date=START + timedelta(days=offset),
            total_estimated_monthly_usd=Decimal(value),
            open_findings=3,
            active_resources=30,
            captured_at=datetime.now(UTC),
        )
        for offset, value in enumerate(values)
    ]


def test_clear_spike_is_detected_with_its_z_score():
    snapshots = _series(["100", "102", "98", "101", "99", "103", "97", "400"])

    anomaly = detect_anomaly(snapshots, min_history_days=7)

    assert anomaly is not None
    assert anomaly.date == START + timedelta(days=7)
    assert anomaly.value == Decimal(400)
    assert anomaly.direction == "increase"
    assert anomaly.z_score > 2.0
    assert anomaly.mean == Decimal("100.00")
    assert anomaly.window_days == 7


def test_drop_is_detected_with_a_negative_direction():
    """A cleanup that worked, or a scanner that silently stopped seeing a
    region. Both are worth a look, which is why decreases are reported."""
    snapshots = _series(["100", "102", "98", "101", "99", "103", "97", "10"])

    anomaly = detect_anomaly(snapshots, min_history_days=7)

    assert anomaly is not None
    assert anomaly.direction == "decrease"
    assert anomaly.z_score < -2.0


def test_ordinary_variation_is_not_an_anomaly():
    snapshots = _series(["100", "102", "98", "101", "99", "103", "97", "104"])

    assert detect_anomaly(snapshots, min_history_days=7) is None


def test_flat_series_has_no_anomaly():
    """Zero stdev is a division by zero AND a meaningless question — a
    perfectly flat series has no distribution to be an outlier in."""
    snapshots = _series(["100"] * 8)

    assert detect_anomaly(snapshots, min_history_days=7) is None


def test_flat_history_with_a_jump_still_declines_to_judge():
    """The common early case: a dev account whose findings do not move for a
    week, then one new volume. Real, but unprovable from a zero-variance
    baseline, so it stays silent rather than reporting an infinite z."""
    snapshots = _series(["100", "100", "100", "100", "100", "100", "100", "500"])

    assert detect_anomaly(snapshots, min_history_days=7) is None


def test_short_history_gets_no_verdict():
    snapshots = _series(["100", "102", "500"])

    assert detect_anomaly(snapshots, min_history_days=7) is None


def test_a_single_snapshot_gets_no_verdict():
    assert detect_anomaly(_series(["100"]), min_history_days=1) is None
    assert detect_anomaly([], min_history_days=1) is None


def test_the_candidate_day_is_excluded_from_its_own_baseline():
    """With seven baseline points a spike included in its own mean and stdev
    inflates both enough to hide itself. This is the arithmetic that makes
    detection work at these sample sizes, so it is pinned."""
    snapshots = _series(["100", "102", "98", "101", "99", "103", "97", "400"])

    anomaly = detect_anomaly(snapshots, min_history_days=7)

    assert anomaly is not None
    # Mean of the first seven only; 400 would drag it to ~137.
    assert anomaly.mean == Decimal("100.00")


def test_window_days_bounds_the_baseline():
    """A 3-day window looks only at the three days before today, so an older
    regime shift does not stay in the baseline forever."""
    snapshots = _series(["10", "10", "10", "100", "101", "99", "300"])

    anomaly = detect_anomaly(snapshots, window_days=3, min_history_days=3)

    assert anomaly is not None
    assert anomaly.window_days == 3
    assert anomaly.mean == Decimal("100.00")


def test_threshold_is_respected():
    """Same day, same baseline (mean 100, stdev ~21.6), z ≈ 1.71 — reported
    only by the looser threshold."""
    snapshots = _series(["80", "120", "90", "110", "100", "130", "70", "137"])

    assert detect_anomaly(snapshots, min_history_days=7, z_threshold=2.0) is None
    assert detect_anomaly(snapshots, min_history_days=7, z_threshold=1.5) is not None


def test_snapshots_out_of_order_are_sorted_before_judging():
    snapshots = _series(["100", "102", "98", "101", "99", "103", "97", "400"])
    shuffled = [snapshots[3], snapshots[7], snapshots[0], *snapshots[1:3], *snapshots[4:7]]

    anomaly = detect_anomaly(shuffled, min_history_days=7)

    assert anomaly is not None
    assert anomaly.value == Decimal(400)
