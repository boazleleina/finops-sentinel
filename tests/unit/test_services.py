from datetime import datetime, UTC, timedelta
from decimal import Decimal

import pytest

from finops_sentinel.domain.models import (
    Resource, Finding, ResourceLifecycle, ResourceType, FindingStatus
)
from finops_sentinel.domain.services import (
    run_scan, notify_open_findings, approve_finding, deny_finding, expire_stale
)
from finops_sentinel.ports.scanner import Scanner
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier


def make_resource(res_id="res-mock", resource_id="vol-123",
                  resource_type=ResourceType.EBS_VOLUME, tags=None):
    now = datetime.now(UTC)
    return Resource(
        id=res_id, resource_id=resource_id, resource_type=resource_type,
        resource_arn="arn", region="us-east-1", current_tags=tags or {},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now
    )


def make_finding(finding_id="f-mock", status=FindingStatus.NOTIFIED,
                 resource_ref="res-mock", protected=False):
    now = datetime.now(UTC)
    return Finding(
        id=finding_id, resource_ref=resource_ref, rule="ebs", evidence={},
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
         resource_type=ResourceType.EBS_VOLUME):
    repository.upsert_resource(make_resource(tags=tags, resource_type=resource_type))
    repository.save_finding(make_finding(status=finding_status, protected=protected))


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
