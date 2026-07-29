"""The digest as a whole: metrics in, an advisory message out.

Two properties are load-bearing and asserted repeatedly here — the digest
carries no approve affordance, and nothing about the advisor can stop it going
out or change a number in it.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
    RightsizingCandidate,
    RightsizingSuggestion,
    SpendAnomaly,
    SpendSnapshot,
)
from finops_sentinel.domain.services import (
    MetricTarget,
    RightsizingReport,
    build_rightsizing_digest,
    compose_digest_sections,
    detect_spend_anomaly,
    record_spend_snapshot,
    send_digest,
)
from tests.fakes import FakeAdvisor, FakeGatewayBase, FakeNotifier

IDLE_CPU = [2.0] * 336
BUSY_CPU = [2.0] * 335 + [95.0]


class FakeMetricGateway(FakeGatewayBase):
    """Running instances plus a CPU series each. Nothing else is stubbed, so a
    digest that reaches for describe_ebs_volumes fails loudly."""

    def __init__(self, instances: list[dict[str, Any]], cpu: dict[str, list[float]]):
        self.instances = instances
        self.cpu = cpu
        self.metric_calls: list[dict[str, str]] = []

    def describe_running_ec2_instances(self):
        return self.instances

    def get_metric_averages(self, namespace, dimensions, metric_name, days, period_seconds=3600):
        self.metric_calls.append(dimensions)
        return self.cpu.get(dimensions["InstanceId"], [])


class ExplodingGateway(FakeGatewayBase):
    def describe_running_ec2_instances(self):
        raise RuntimeError("AccessDenied: ec2:DescribeInstances")


def _instance(instance_id: str, instance_type: str = "m5.xlarge", tags=None):
    return {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "Tags": tags if tags is not None else [],
    }


def _suggestion(resource_id="i-0abc", saving="83.95"):
    return RightsizingSuggestion(
        resource_id=resource_id,
        region="us-east-1",
        current_instance_type="m5.xlarge",
        current_monthly_cost_usd=Decimal("140.16"),
        candidate=RightsizingCandidate(
            instance_type="m6g.large",
            monthly_cost_usd=Decimal("56.21"),
            monthly_saving_usd=Decimal(saving),
        ),
        max_cpu_percent=3.2,
        avg_cpu_percent=2.0,
        datapoints=336,
        observation_days=14,
    )


def _report(suggestions, regions_failed=None, examined=None):
    return RightsizingReport(
        suggestions=suggestions,
        regions_failed=regions_failed or {},
        instances_examined=len(suggestions) if examined is None else examined,
    )


def _anomaly():
    return SpendAnomaly(
        date=date(2026, 7, 27),
        value=Decimal("412.50"),
        mean=Decimal("180.00"),
        stdev=Decimal("40.00"),
        z_score=5.81,
        direction="increase",
        window_days=14,
    )


# --------------------------------------------------------------------------
# Gathering the suggestions
# --------------------------------------------------------------------------


def test_digest_suggests_the_idle_instance_and_leaves_the_busy_one_alone():
    gateway = FakeMetricGateway(
        [_instance("i-idle"), _instance("i-busy")],
        {"i-idle": IDLE_CPU, "i-busy": BUSY_CPU},
    )

    report = build_rightsizing_digest([MetricTarget("us-east-1", gateway)], StaticPricing())

    assert [s.resource_id for s in report.suggestions] == ["i-idle"]
    assert report.suggestions[0].candidate.instance_type == "m6g.large"
    assert report.regions_failed == {}
    assert report.instances_examined == 2


def test_digest_queries_cloudwatch_by_instance_id_dimension():
    """Pins the dimension map: CloudWatch matches dimension sets exactly, and
    a wrong key returns empty, which is indistinguishable from 'no data'."""
    gateway = FakeMetricGateway([_instance("i-idle")], {"i-idle": IDLE_CPU})

    build_rightsizing_digest([MetricTarget("us-east-1", gateway)], StaticPricing())

    assert gateway.metric_calls == [{"InstanceId": "i-idle"}]


def test_protected_instances_are_never_suggested():
    """'You could shrink this' is still touching a resource the owner said
    not to touch."""
    gateway = FakeMetricGateway(
        [_instance("i-idle", tags=[{"Key": "finops:protected", "Value": "true"}])],
        {"i-idle": IDLE_CPU},
    )

    report = build_rightsizing_digest([MetricTarget("us-east-1", gateway)], StaticPricing())

    assert report.suggestions == []
    # Not even looked at, so an empty digest cannot be read as "checked and fine".
    assert report.instances_examined == 0


def test_a_failed_region_is_skipped_not_fatal():
    """Three regions out of four is still a digest worth sending."""
    good = FakeMetricGateway([_instance("i-idle")], {"i-idle": IDLE_CPU})

    report = build_rightsizing_digest(
        [MetricTarget("eu-west-1", ExplodingGateway()), MetricTarget("us-east-1", good)],
        StaticPricing(),
    )

    assert [s.resource_id for s in report.suggestions] == ["i-idle"]
    # Reported, not just logged: "nothing over-provisioned" and "eu-west-1 never
    # answered" produce the same list and mean opposite things.
    assert "eu-west-1" in report.regions_failed
    assert "AccessDenied" in report.regions_failed["eu-west-1"]


def test_a_metric_failure_skips_one_instance_not_the_digest():
    class HalfBrokenGateway(FakeMetricGateway):
        def get_metric_averages(self, namespace, dimensions, metric_name, days, period_seconds=3600):
            if dimensions["InstanceId"] == "i-broken":
                raise RuntimeError("Throttling: Rate exceeded")
            return super().get_metric_averages(
                namespace, dimensions, metric_name, days, period_seconds
            )

    gateway = HalfBrokenGateway(
        [_instance("i-broken"), _instance("i-idle")], {"i-idle": IDLE_CPU}
    )

    report = build_rightsizing_digest([MetricTarget("us-east-1", gateway)], StaticPricing())

    assert [s.resource_id for s in report.suggestions] == ["i-idle"]


def test_suggestions_are_capped_and_ordered_by_saving():
    instances = [_instance(f"i-{n}", "m5.2xlarge" if n == 3 else "t3.large") for n in range(5)]
    gateway = FakeMetricGateway(instances, {f"i-{n}": IDLE_CPU for n in range(5)})

    suggestions = build_rightsizing_digest(
        [MetricTarget("us-east-1", gateway)], StaticPricing(), max_items=2
    ).suggestions

    assert len(suggestions) == 2
    # The 2xlarge saves the most, so it leads regardless of discovery order.
    assert suggestions[0].resource_id == "i-3"
    savings = [s.monthly_saving_usd for s in suggestions]
    assert savings == sorted(savings, reverse=True)


# --------------------------------------------------------------------------
# Composing and sending
# --------------------------------------------------------------------------


def test_digest_sends_once_and_carries_no_approve_affordance(repository):
    notifier = FakeNotifier()

    send_digest(repository, notifier, _report([_suggestion()]), anomaly=_anomaly(), advisor=FakeAdvisor())

    assert len(notifier.digests) == 1
    assert notifier.alerts == []  # never routed through the interactive path
    _title, sections = notifier.digests[0]
    body = "\n".join(sections).lower()
    assert "approve" not in body
    assert "deny" not in body


def test_anomaly_section_leads_and_says_estimated_waste():
    sections = compose_digest_sections(_report([_suggestion()]), _anomaly(), advisor=FakeAdvisor())

    assert sections[0].startswith("*Spend anomaly*")
    # The honesty guardrail: this number is not billed spend, and every place
    # it surfaces has to say so.
    assert "not billed spend" in sections[0]
    assert "412.50" in sections[0]


def test_suggestions_render_with_the_peak_that_drove_them():
    sections = compose_digest_sections(_report([_suggestion()]), None, advisor=FakeAdvisor())

    rightsizing = sections[-1]
    assert "i-0abc" in rightsizing
    assert "m5.xlarge → m6g.large" in rightsizing
    assert "83.95" in rightsizing
    assert "peak CPU 3.2%" in rightsizing


def test_digest_with_nothing_to_report_still_says_so():
    sections = compose_digest_sections(_report([]), None)

    assert len(sections) == 1
    assert "Nothing looks over-provisioned" in sections[0]


def test_advisor_gets_the_fact_keys_its_narrators_expect():
    advisor = FakeAdvisor()

    compose_digest_sections(_report([_suggestion()]), _anomaly(), advisor=advisor)

    topics = dict(advisor.narrations)
    assert set(topics) == {"spend_anomaly", "rightsizing"}
    assert set(topics["rightsizing"]) == {"count", "window_days", "total_saving"}
    assert set(topics["spend_anomaly"]) == {
        "date",
        "value",
        "mean",
        "z_score",
        "window_days",
        "direction",
    }


def test_a_raising_advisor_still_produces_a_digest(repository):
    """The Advisor port forbids raising, but a third-party implementation
    breaking that contract must cost prose, not the whole message."""

    class BrokenAdvisor(FakeAdvisor):
        def narrate(self, topic, facts):
            raise RuntimeError("model server on fire")

    notifier = FakeNotifier()

    send_digest(repository, notifier, _report([_suggestion()]), anomaly=_anomaly(), advisor=BrokenAdvisor())

    _, sections = notifier.digests[0]
    # Fell through to the deterministic template, which still carries the numbers.
    assert "412.50" in sections[0]
    assert "1 instance(s)" in sections[1]


def test_digest_works_with_no_advisor_at_all():
    sections = compose_digest_sections(_report([_suggestion()]), _anomaly(), advisor=None)

    assert "412.50" in sections[0]
    assert "instance(s) look over-provisioned" in sections[1]


def test_sending_the_digest_is_audited(repository):
    send_digest(repository, FakeNotifier(), _report([_suggestion()]), anomaly=_anomaly())

    events = {event.event: event.detail for event in repository.get_audit_events()}
    assert events["digest_sent"]["suggestions"] == 1
    assert events["digest_sent"]["total_saving_usd"] == "83.95"
    assert events["digest_sent"]["anomaly"] == "2026-07-27"


# --------------------------------------------------------------------------
# Snapshots: the input the anomaly detector runs on
# --------------------------------------------------------------------------


def _resource(resource_id: str, lifecycle=ResourceLifecycle.ACTIVE) -> Resource:
    now = datetime.now(UTC)
    return Resource(
        id=f"res-{resource_id}",
        resource_id=resource_id,
        resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn",
        region="us-east-1",
        current_tags={},
        lifecycle=lifecycle,
        first_seen_at=now,
        last_seen_at=now,
    )


def _finding(finding_id: str, cost: str, status=FindingStatus.OPEN, protected=False) -> Finding:
    now = datetime.now(UTC)
    return Finding(
        id=finding_id,
        resource_ref="res-vol-1",
        rule="ebs_unattached",
        evidence={},
        tags_at_detection={},
        est_monthly_cost_usd=Decimal(cost),
        status=status,
        protected=protected,
        detected_at=now,
        last_seen_at=now,
    )


def test_snapshot_totals_only_live_unprotected_findings(repository):
    repository.upsert_resource(_resource("vol-1"))
    repository.upsert_resource(_resource("vol-2", lifecycle=ResourceLifecycle.DELETED))
    repository.save_finding(_finding("f-open", "10.00"))
    repository.save_finding(_finding("f-notified", "5.00", status=FindingStatus.NOTIFIED))
    repository.save_finding(_finding("f-denied", "99.00", status=FindingStatus.DENIED))
    repository.save_finding(_finding("f-remediated", "99.00", status=FindingStatus.REMEDIATED))
    repository.save_finding(_finding("f-protected", "99.00", protected=True))

    snapshot = record_spend_snapshot(repository)

    assert snapshot.total_estimated_monthly_usd == Decimal("15.00")
    assert snapshot.open_findings == 2
    assert snapshot.active_resources == 1


def test_snapshot_is_recorded_and_audited(repository):
    record_spend_snapshot(repository)

    stored = repository.get_spend_snapshots(datetime.now(UTC).date() - timedelta(days=1))
    assert len(stored) == 1
    assert [e.event for e in repository.get_audit_events()] == ["spend_snapshot_recorded"]


def test_second_scan_the_same_day_overwrites_rather_than_appends(repository):
    """Scan cadence must not decide how much a day weighs in the mean."""
    repository.upsert_resource(_resource("vol-1"))
    repository.save_finding(_finding("f-open", "10.00"))
    record_spend_snapshot(repository)

    repository.save_finding(_finding("f-two", "7.00"))
    record_spend_snapshot(repository)

    stored = repository.get_spend_snapshots(datetime.now(UTC).date() - timedelta(days=1))
    assert len(stored) == 1
    assert stored[0].total_estimated_monthly_usd == Decimal("17.00")


def test_detect_spend_anomaly_reads_the_window_from_the_repository(repository):
    today = datetime.now(UTC).date()
    for offset, value in enumerate(["100", "102", "98", "101", "99", "103", "97"]):
        repository.record_spend_snapshot(
            SpendSnapshot(
                snapshot_date=today - timedelta(days=7 - offset),
                total_estimated_monthly_usd=Decimal(value),
                open_findings=3,
                active_resources=30,
                captured_at=datetime.now(UTC),
            )
        )
    repository.upsert_resource(_resource("vol-1"))
    repository.save_finding(_finding("f-spike", "400.00"))
    record_spend_snapshot(repository)

    anomaly = detect_spend_anomaly(repository)

    assert anomaly is not None
    assert anomaly.value == Decimal("400.00")
    assert anomaly.direction == "increase"


def test_no_anomaly_without_enough_history(repository):
    record_spend_snapshot(repository)

    assert detect_spend_anomaly(repository) is None


# --------------------------------------------------------------------------
# Partial coverage: the empty digest that must not read as good news
# --------------------------------------------------------------------------


def test_an_empty_digest_over_failed_regions_says_so(repository):
    """The defect this pins was found by running the thing: with every region
    unreachable the digest still said "nothing looks over-provisioned", which
    is the one message an operator responds to by doing nothing."""
    notifier = FakeNotifier()
    report = _report([], regions_failed={"eu-west-1": "EndpointConnectionError: refused"}, examined=0)

    send_digest(repository, notifier, report)

    _title, sections = notifier.digests[0]
    body = "\n".join(sections)
    assert "Incomplete coverage" in body
    assert "eu-west-1" in body


def test_a_clean_digest_reports_how_much_was_examined():
    """"Nothing over-provisioned across 40 instances" and "nothing across 0"
    are the same sentence and opposite facts."""
    sections = compose_digest_sections(_report([], examined=40), None)

    assert "40 running instance(s) examined" in sections[0]
    assert "Incomplete coverage" not in "\n".join(sections)


def test_failed_regions_are_recorded_in_the_audit_trail(repository):
    report = _report([_suggestion()], regions_failed={"eu-west-1": "boom"}, examined=3)

    send_digest(repository, notifier=FakeNotifier(), report=report)

    detail = repository.get_audit_events()[0].detail
    assert detail["regions_failed"] == ["eu-west-1"]
    assert detail["instances_examined"] == 3
