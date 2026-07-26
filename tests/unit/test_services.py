from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.services import (
    approve_finding,
    deny_finding,
    expire_stale,
    notify_open_findings,
    run_scan,
)
from finops_sentinel.ports.advisor import Advisor
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.scanner import Scanner


def make_resource(res_id="res-mock", resource_id="vol-123",
                  resource_type=ResourceType.EBS_VOLUME, tags=None):
    now = datetime.now(UTC)
    return Resource(
        id=res_id, resource_id=resource_id, resource_type=resource_type,
        resource_arn="arn", region="us-east-1", current_tags=tags or {},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now
    )


def make_finding(finding_id="f-mock", status=FindingStatus.NOTIFIED,
                 resource_ref="res-mock", protected=False, rule="ebs"):
    now = datetime.now(UTC)
    return Finding(
        id=finding_id, resource_ref=resource_ref, rule=rule, evidence={},
        tags_at_detection={}, est_monthly_cost_usd=Decimal("1.00"),
        status=status, protected=protected, detected_at=now, last_seen_at=now
    )


class MockScanner(Scanner):
    def discover(self, gateway):
        return [(make_resource(), {"mock_data": True})]

    def evaluate(self, discover_results):
        res = discover_results[0][0]
        return [make_finding(status=FindingStatus.OPEN, resource_ref=res.id)]


class FakeCloudGateway(CloudGateway):
    """In-memory gateway: records executed playbooks, can be told to fail."""

    def __init__(self, fail=False):
        self.executed = []
        self.fail = fail

    def describe_ebs_volumes(self): return []
    def describe_elastic_ips(self): return []
    def describe_ec2_instances(self): return []
    def describe_ebs_snapshots(self): return []
    def describe_running_ec2_instances(self): return []

    def get_instance_metric_averages(
        self, instance_id, metric_name, days, period_seconds=3600
    ):
        return []

    def execute(self, playbook, resource_id, dry_run):
        if self.fail:
            raise RuntimeError("cloud exploded")
        self.executed.append((playbook, resource_id, dry_run))
        return {"snapshot_id": f"snap-{resource_id}"} if not dry_run else {"dry_run": True}


class FakeNotifier(Notifier):
    """Appends alerts to a list. Zero Slack, zero HTTP."""

    def __init__(self):
        self.alerts = []

    @property
    def channel_name(self):
        return "fake"

    def send_finding_alert(self, finding, resource):
        self.alerts.append((finding.id, resource.resource_id))
        return f"msg-{len(self.alerts)}"

    def parse_callback(self, raw_body, headers):
        raise ValueError("not supported")

    def confirm_decision(self, reply_context, text):
        pass


def seed(repository, *, finding_status=FindingStatus.NOTIFIED, tags=None, protected=False,
         resource_type=ResourceType.EBS_VOLUME, rule="ebs"):
    repository.upsert_resource(make_resource(tags=tags, resource_type=resource_type))
    repository.save_finding(
        make_finding(status=finding_status, protected=protected, rule=rule)
    )


def test_run_scan_orchestrator(repository):
    findings = run_scan(None, repository, [MockScanner()])

    assert len(findings) == 1
    assert repository.get_all_resources()[0].resource_id == "vol-123"
    assert repository.get_findings()[0].id == "f-mock"
    # Scan itself is audited
    assert any(e.event == "scan_completed" for e in repository.get_audit_events())


def test_notify_open_findings(repository):
    seed(repository, finding_status=FindingStatus.OPEN)
    notifier = FakeNotifier()

    notified = notify_open_findings(repository, notifier)

    assert [f.id for f in notified] == ["f-mock"]
    assert notifier.alerts == [("f-mock", "vol-123")]
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.NOTIFIED
    assert repository.get_latest_notification_time("f-mock") is not None
    assert any(e.event == "finding_notified" for e in repository.get_audit_events("f-mock"))


def test_notify_skips_protected(repository):
    seed(repository, finding_status=FindingStatus.OPEN, protected=True)
    notifier = FakeNotifier()

    notified = notify_open_findings(repository, notifier)

    assert notified == []
    assert notifier.alerts == []
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.OPEN


def test_approve_finding_live(repository):
    seed(repository)
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is True

    assert gateway.executed == [("snapshot_then_delete_volume", "vol-123", False)]
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.REMEDIATED
    events = {e.event for e in repository.get_audit_events("f-mock")}
    assert {"finding_approved", "remediation_executed"} <= events


def test_approve_finding_dry_run(repository):
    seed(repository)
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=True) is True

    # Dry run: playbook invoked with dry_run flag, finding NOT marked remediated
    assert gateway.executed == [("snapshot_then_delete_volume", "vol-123", True)]
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.APPROVED
    events = {e.event for e in repository.get_audit_events("f-mock")}
    assert "remediation_dry_run" in events
    assert "remediation_executed" not in events


def test_approve_blocked_when_protected_flag(repository):
    seed(repository, protected=True)
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is False
    assert gateway.executed == []
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.NOTIFIED
    assert any(e.event == "approve_blocked_protected"
               for e in repository.get_audit_events("f-mock"))


def test_approve_blocked_when_resource_tagged_protected_after_detection(repository):
    # Finding was created unprotected, but the resource has since been tagged.
    seed(repository, tags={"finops:protected": "true"}, protected=False)
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is False
    assert gateway.executed == []


def test_approve_blocked_when_resource_gone(repository):
    # Inventory says the resource was deleted out-of-band since detection.
    res = make_resource()
    res.lifecycle = ResourceLifecycle.DELETED
    repository.upsert_resource(res)
    repository.save_finding(make_finding())
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is False
    assert gateway.executed == []
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.NOTIFIED
    assert any(e.event == "approve_blocked_resource_gone"
               for e in repository.get_audit_events("f-mock"))


def test_approve_from_open_is_illegal(repository):
    seed(repository, finding_status=FindingStatus.OPEN)
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is False
    assert gateway.executed == []


def test_double_approve_executes_once(repository):
    seed(repository)
    gateway = FakeCloudGateway()

    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is True
    assert approve_finding("f-mock", repository, gateway,
                           actor="boaz", channel="slack", dry_run=False) is False
    assert len(gateway.executed) == 1


def test_approve_playbook_failure_marks_failed(repository):
    seed(repository)
    gateway = FakeCloudGateway(fail=True)

    with pytest.raises(RuntimeError):
        approve_finding("f-mock", repository, gateway,
                        actor="boaz", channel="slack", dry_run=False)

    assert repository.get_finding_by_id("f-mock").status == FindingStatus.FAILED
    assert any(e.event == "remediation_failed"
               for e in repository.get_audit_events("f-mock"))


def test_deny_finding(repository):
    seed(repository)

    assert deny_finding("f-mock", repository, actor="boaz", channel="slack") is True
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.DENIED
    assert any(e.event == "finding_denied" for e in repository.get_audit_events("f-mock"))

    # Terminal: second deny is a no-op failure
    assert deny_finding("f-mock", repository, actor="boaz", channel="slack") is False


def test_expire_stale_uses_notification_time(repository):
    now = datetime.now(UTC)
    repository.upsert_resource(make_resource())

    repository.save_finding(make_finding("f-stale", status=FindingStatus.NOTIFIED))
    repository.record_notification("f-stale", "fake", None, now - timedelta(hours=100))

    repository.save_finding(make_finding("f-fresh", status=FindingStatus.NOTIFIED))
    repository.record_notification("f-fresh", "fake", None, now - timedelta(hours=1))

    expired = expire_stale(repository)

    assert expired == ["f-stale"]
    assert repository.get_finding_by_id("f-stale").status == FindingStatus.EXPIRED
    assert repository.get_finding_by_id("f-fresh").status == FindingStatus.NOTIFIED


def test_approve_blocked_for_notify_only_rule(repository):
    """A metric-inferred finding must never reach a playbook.

    ec2_idle sits on an EC2_INSTANCE, and PLAYBOOK_ALLOWLIST maps that type to
    terminate_stopped_instance — but the instance is RUNNING. Without the
    rule-level gate, approving here terminates a live host.
    """
    seed(repository, resource_type=ResourceType.EC2_INSTANCE, rule="ec2_idle")
    gateway = FakeCloudGateway()

    approved = approve_finding(
        "f-mock", repository, gateway, actor="boaz", channel="slack", dry_run=False
    )

    assert approved is False
    assert gateway.executed == []
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.NOTIFIED
    assert any(
        e.event == "approve_blocked_notify_only"
        for e in repository.get_audit_events("f-mock")
    )


def test_approve_still_works_for_state_based_ec2_rule(repository):
    """The gate is per-rule, not per-type: ec2_stopped stays remediable."""
    seed(repository, resource_type=ResourceType.EC2_INSTANCE, rule="ec2_stopped")
    gateway = FakeCloudGateway()

    approved = approve_finding(
        "f-mock", repository, gateway, actor="boaz", channel="slack", dry_run=False
    )

    assert approved is True
    assert gateway.executed == [("terminate_stopped_instance", "vol-123", False)]


class FakeAdvisor(Advisor):
    def __init__(self):
        self.calls = []

    def summarize(self, finding, resource):
        self.calls.append(finding.id)
        return f"advice for {finding.rule} on {resource.resource_id}"


def test_notify_attaches_and_persists_advisor_summary(repository):
    seed(repository, finding_status=FindingStatus.OPEN)
    notifier = FakeNotifier()
    advisor = FakeAdvisor()

    notified = notify_open_findings(repository, notifier, advisor)

    assert advisor.calls == ["f-mock"]
    assert notified[0].llm_summary == "advice for ebs on vol-123"
    # Persisted, so the API and later notifications see the same text.
    assert repository.get_finding_by_id("f-mock").llm_summary == "advice for ebs on vol-123"


def test_notify_without_advisor_leaves_summary_empty(repository):
    seed(repository, finding_status=FindingStatus.OPEN)

    notified = notify_open_findings(repository, FakeNotifier())

    assert notified[0].llm_summary is None


def test_advisor_failure_cannot_block_notification(repository):
    """The port forbids raising; a violating advisor must not strand findings."""
    class ExplodingAdvisor(Advisor):
        def summarize(self, finding, resource):
            raise RuntimeError("ollama is on fire")

    seed(repository, finding_status=FindingStatus.OPEN)

    with pytest.raises(RuntimeError):
        notify_open_findings(repository, FakeNotifier(), ExplodingAdvisor())

    # Documents the blast radius: the finding stays OPEN and the next scan
    # retries it, so nothing is silently lost.
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.OPEN


def test_advisor_budget_caps_llm_calls_and_templates_the_rest(repository):
    """Local inference costs seconds each; an unbudgeted scan over a big
    account would run for hours."""
    repository.upsert_resource(make_resource())
    for index, cost in enumerate([Decimal("100.00"), Decimal("50.00"), Decimal("1.00")]):
        finding = make_finding(f"f-{index}", status=FindingStatus.OPEN)
        finding.est_monthly_cost_usd = cost
        repository.save_finding(finding)

    advisor = FakeAdvisor()
    notify_open_findings(repository, FakeNotifier(), advisor, advisor_budget=2)

    # Budget goes to the two most expensive findings, in cost order.
    assert advisor.calls == ["f-0", "f-1"]
    # The cheapest still gets a summary — just the deterministic one.
    cheapest = repository.get_finding_by_id("f-2")
    assert cheapest.llm_summary is not None
    assert "advice for" not in cheapest.llm_summary


def test_zero_budget_skips_inference_entirely(repository):
    seed(repository, finding_status=FindingStatus.OPEN)
    advisor = FakeAdvisor()

    notify_open_findings(repository, FakeNotifier(), advisor, advisor_budget=0)

    assert advisor.calls == []
    assert repository.get_finding_by_id("f-mock").llm_summary is not None


def test_notifications_go_out_most_expensive_first(repository):
    repository.upsert_resource(make_resource())
    for index, cost in enumerate([Decimal("5.00"), Decimal("90.00"), Decimal("20.00")]):
        finding = make_finding(f"f-{index}", status=FindingStatus.OPEN)
        finding.est_monthly_cost_usd = cost
        repository.save_finding(finding)

    notifier = FakeNotifier()
    notify_open_findings(repository, notifier)

    assert [alert[0] for alert in notifier.alerts] == ["f-1", "f-2", "f-0"]


def test_saved_summary_is_not_erased_by_rescan(repository):
    seed(repository, finding_status=FindingStatus.OPEN)
    notify_open_findings(repository, FakeNotifier(), FakeAdvisor())

    # A re-detected finding arrives from the scanner with llm_summary=None.
    repository.save_finding(make_finding(status=FindingStatus.OPEN))

    assert repository.get_finding_by_id("f-mock").llm_summary == "advice for ebs on vol-123"
