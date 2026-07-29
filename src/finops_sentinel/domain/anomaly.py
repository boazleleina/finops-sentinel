"""Spend-anomaly detection. Pure stdlib statistics — no pandas, no LLM.

Two deliberate constraints, both load-bearing:

*   **The maths is deterministic and lives here.** The Advisor only narrates
    the result (see summaries._narrate_spend_anomaly). A model that could
    change the z-score would make the alert unfalsifiable; one that can only
    phrase it cannot lie about whether today was unusual.
*   **stdlib, not pandas.** The architecture contract is literally named
    "Domain is pure Python (pydantic only)". A rolling mean/stdev/z-score is
    the twenty lines below; taking a 60MB dependency to avoid writing them
    would make the README's claim about this layer false.

Every guard returns None rather than a weak verdict. An anomaly alert that
fires on three days of history trains people to ignore it, which costs more
than the alert it replaced.
"""
import statistics
from decimal import Decimal

from finops_sentinel.domain.models import SpendAnomaly, SpendSnapshot


def detect_anomaly(
    snapshots: list[SpendSnapshot],
    window_days: int = 14,
    min_history_days: int = 7,
    z_threshold: float = 2.0,
) -> SpendAnomaly | None:
    """Flag the most recent snapshot if it sits z_threshold sigma off its own past.

    The candidate is the newest snapshot; the baseline is the `window_days`
    snapshots *before* it. Excluding the candidate from its own mean and stdev
    matters at these sample sizes: with 7 points, a spike included in its own
    baseline pulls the mean up and the stdev out far enough to hide itself.

    Returns None — no verdict — when:

    - fewer than min_history_days baseline points exist. Two days of history
      cannot say what normal looks like.
    - the baseline stdev is zero. That is both a division by zero and a
      genuinely meaningless question: a perfectly flat series has no
      distribution to be an outlier in. It is also the common case early on,
      when a dev account's findings do not change day to day.
    """
    if len(snapshots) < 2:
        return None

    ordered = sorted(snapshots, key=lambda s: s.snapshot_date)
    latest = ordered[-1]
    baseline = ordered[-(window_days + 1) : -1]

    if len(baseline) < min_history_days:
        return None

    values = [float(s.total_estimated_monthly_usd) for s in baseline]
    mean = statistics.fmean(values)
    # Sample stdev, not population: the baseline is a sample of the account's
    # behaviour, not the whole of it. Needs >= 2 points, which min_history_days
    # already guarantees for any sane setting.
    stdev = statistics.stdev(values)

    if stdev == 0:
        return None

    value = float(latest.total_estimated_monthly_usd)
    z_score = (value - mean) / stdev

    if abs(z_score) < z_threshold:
        return None

    return SpendAnomaly(
        date=latest.snapshot_date,
        value=latest.total_estimated_monthly_usd,
        mean=Decimal(str(round(mean, 2))),
        stdev=Decimal(str(round(stdev, 2))),
        z_score=round(z_score, 2),
        direction="increase" if z_score > 0 else "decrease",
        window_days=len(baseline),
    )
