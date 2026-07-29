"""Right-sizing decisions: pure domain, fakes only, no AWS.

The tests that matter here are the ones about *not* suggesting. A right-sizing
tool that shrinks a spiky production box is worse than no tool, so silence on
thin or bursty data is the behaviour under test, not an edge case.
"""
from decimal import Decimal

from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.domain.models import RightsizingCandidate
from finops_sentinel.domain.rightsizing import (
    rank_suggestions,
    suggest_rightsizing,
    total_saving,
)

FLAT_IDLE = [3.0] * 336  # 14 days of hourly datapoints, never busy


def _candidates() -> list[RightsizingCandidate]:
    """Fresh objects per call: a suggestion holds the candidate it was handed,
    so a module-level list would let one test's edit reach another's."""
    return [
        RightsizingCandidate(
            instance_type="m6g.large",
            monthly_cost_usd=Decimal("56.21"),
            monthly_saving_usd=Decimal("83.95"),
        ),
        RightsizingCandidate(
            instance_type="m5.large",
            monthly_cost_usd=Decimal("70.08"),
            monthly_saving_usd=Decimal("70.08"),
        ),
    ]


def _suggest(cpu_series, candidates=None, **kwargs):
    if candidates is None:
        candidates = _candidates()
    return suggest_rightsizing(
        resource_id="i-0abc",
        region="us-east-1",
        instance_type="m5.xlarge",
        cpu_series=cpu_series,
        current_monthly_cost_usd=Decimal("140.16"),
        candidates=candidates,
        observation_days=14,
        **kwargs,
    )


def test_over_provisioned_instance_suggests_the_biggest_saving_target():
    suggestion = _suggest(FLAT_IDLE)

    assert suggestion is not None
    assert suggestion.candidate.instance_type == "m6g.large"
    assert suggestion.monthly_saving_usd == Decimal("83.95")
    assert suggestion.current_instance_type == "m5.xlarge"
    assert suggestion.datapoints == 336


def test_evidence_carries_the_peak_that_drove_the_call():
    """An operator has to be able to disagree, and only the peak lets them."""
    suggestion = _suggest([1.0] * 300 + [30.0])

    assert suggestion is not None
    assert suggestion.max_cpu_percent == 30.0
    assert suggestion.avg_cpu_percent < 2.0


def test_spiky_instance_is_not_suggested():
    """Mean 4%, peak 90% — correctly sized. Averaging is how this rule kills
    production, so the one datapoint above the threshold has to be enough."""
    spiky = [1.0] * 335 + [90.0]

    assert _suggest(spiky) is None


def test_instance_at_exactly_the_headroom_is_not_suggested():
    assert _suggest([40.0] * 336, cpu_headroom_percent=40.0) is None


def test_thin_metric_series_gets_no_verdict():
    """A freshly launched instance has no history; that is not evidence."""
    assert _suggest([1.0] * 5) is None
    assert _suggest([]) is None


def test_instance_with_no_cheaper_candidate_is_not_suggested():
    assert _suggest(FLAT_IDLE, candidates=[]) is None


def test_candidates_that_save_nothing_are_ignored():
    """A price change can invert a candidate; the domain must not trust it."""
    worthless = [
        RightsizingCandidate(
            instance_type="m5.large",
            monthly_cost_usd=Decimal("140.16"),
            monthly_saving_usd=Decimal(0),
        )
    ]

    assert _suggest(FLAT_IDLE, candidates=worthless) is None


def test_suggestions_are_ordered_by_saving_and_capped():
    # Distinct savings, deliberately out of order.
    suggestions = [
        _suggest(
            FLAT_IDLE,
            candidates=[
                RightsizingCandidate(
                    instance_type="m6g.large",
                    monthly_cost_usd=Decimal("56.21"),
                    monthly_saving_usd=Decimal(str(saving)),
                )
            ],
        )
        for saving in (10, 30, 20, 10, 20)
    ]

    ranked = rank_suggestions(suggestions, max_items=3)

    assert len(ranked) == 3
    savings = [s.monthly_saving_usd for s in ranked]
    assert savings == sorted(savings, reverse=True)
    assert savings[0] == Decimal(30)


def test_zero_cap_returns_nothing():
    assert rank_suggestions([_suggest(FLAT_IDLE)], max_items=0) == []


def test_total_saving_sums_in_decimal():
    assert total_saving([]) == Decimal(0)
    assert total_saving([_suggest(FLAT_IDLE), _suggest(FLAT_IDLE)]) == Decimal("167.90")


# --------------------------------------------------------------------------
# The price side: what is cheaper, never whether to recommend it
# --------------------------------------------------------------------------


def test_pricing_offers_downsize_and_graviton_targets():
    candidates = StaticPricing().rightsizing_candidates("m5.xlarge", "us-east-1")

    types = [c.instance_type for c in candidates]
    assert set(types) == {"m5.large", "m6g.xlarge", "m6g.large"}
    # Biggest saving first — the domain re-sorts anyway, but the port promises it.
    assert [c.monthly_saving_usd for c in candidates] == sorted(
        (c.monthly_saving_usd for c in candidates), reverse=True
    )


def test_pricing_savings_are_the_difference_between_the_two_monthly_costs():
    pricing = StaticPricing()
    current = pricing.ec2_instance_monthly("m5.xlarge", "us-east-1")

    for candidate in pricing.rightsizing_candidates("m5.xlarge", "us-east-1"):
        assert candidate.monthly_saving_usd == current - candidate.monthly_cost_usd
        assert candidate.monthly_cost_usd < current


def test_unknown_instance_type_gets_no_candidates():
    """Pricing an unknown m7i.48xlarge at the t3.medium default and then
    'saving' money by downsizing it would invent the entire suggestion."""
    assert StaticPricing().rightsizing_candidates("m7i.48xlarge", "us-east-1") == []


def test_smallest_type_in_a_family_gets_only_the_graviton_swap():
    candidates = StaticPricing().rightsizing_candidates("m5.large", "us-east-1")

    assert [c.instance_type for c in candidates] == ["m6g.large"]
