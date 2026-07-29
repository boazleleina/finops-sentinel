"""Right-sizing decisions. Pure domain logic — no I/O, no framework imports.

The split here is the point of the module: the Pricing port supplies *what is
cheaper*, this file decides *whether to suggest it*. Prices change weekly and
live in one table; the judgement is stable and lives in the domain, where it is
testable with a list of floats and no AWS at all.

Nothing here remediates. A right-sizing change means a stop/start and a
different instance type — it is a deployment decision, not a cleanup, and the
digest that carries these suggestions is button-free by contract.
"""
from decimal import Decimal

from finops_sentinel.domain.models import RightsizingCandidate, RightsizingSuggestion


def suggest_rightsizing(
    resource_id: str,
    region: str,
    instance_type: str,
    cpu_series: list[float],
    current_monthly_cost_usd: Decimal,
    candidates: list[RightsizingCandidate],
    observation_days: int,
    cpu_headroom_percent: float = 40.0,
    min_datapoints: int = 24,
) -> RightsizingSuggestion | None:
    """Decide whether one instance is over-provisioned. None means "no verdict".

    The rule is **peak** CPU below cpu_headroom_percent, not mean. A box that
    idles all day and pegs 90% once an hour is correctly sized; its mean is 4%
    and would recommend halving the machine that carries its actual workload.
    Averages are how right-sizing tools produce advice that takes production
    down, so this one refuses to look at them.

    The 40% default pairs with a candidate list that only ever steps down one
    size: halving vCPU roughly doubles utilisation, so a 40% peak lands near
    80% on the target — tight but not saturated. Raising the threshold without
    shortening the candidate list is how that stops being true.

    A series shorter than min_datapoints is silence, not a verdict: a freshly
    launched instance has no history, and a CloudWatch gap is not evidence of
    idleness.
    """
    if len(cpu_series) < min_datapoints:
        return None

    max_cpu = max(cpu_series)
    if max_cpu >= cpu_headroom_percent:
        return None

    # Only candidates that actually save money, biggest saving first. The
    # Pricing port already filters and orders these; re-doing it here keeps the
    # domain's guarantee independent of any one adapter's diligence.
    affordable = [c for c in candidates if c.monthly_saving_usd > 0]
    if not affordable:
        # Nothing cheaper exists (smallest in its family, or an instance type
        # the price table does not know). No suggestion beats a wrong one.
        return None

    best = max(affordable, key=lambda c: c.monthly_saving_usd)

    return RightsizingSuggestion(
        resource_id=resource_id,
        region=region,
        current_instance_type=instance_type,
        current_monthly_cost_usd=current_monthly_cost_usd,
        candidate=best,
        max_cpu_percent=round(max_cpu, 3),
        avg_cpu_percent=round(sum(cpu_series) / len(cpu_series), 3),
        datapoints=len(cpu_series),
        observation_days=observation_days,
    )


def rank_suggestions(
    suggestions: list[RightsizingSuggestion], max_items: int
) -> list[RightsizingSuggestion]:
    """Biggest saving first, capped.

    The cap is a readability guardrail, not a cost one: a digest listing four
    hundred instances is a digest nobody reads, and the ones worth acting on
    are at the top by construction.
    """
    ordered = sorted(suggestions, key=lambda s: s.monthly_saving_usd, reverse=True)
    return ordered[:max_items] if max_items > 0 else []


def total_saving(suggestions: list[RightsizingSuggestion]) -> Decimal:
    """What the listed suggestions would save if every one were taken."""
    return sum((s.monthly_saving_usd for s in suggestions), Decimal(0))
