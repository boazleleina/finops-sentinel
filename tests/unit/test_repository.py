from datetime import datetime, UTC, timedelta
from decimal import Decimal

from finops_sentinel.domain.models import (
    AuditEvent, Decision, Finding, Resource, FindingStatus, ResourceLifecycle, ResourceType
)


def make_finding(finding_id="f-1", status=FindingStatus.OPEN, **overrides):
    now = datetime.now(UTC)
    defaults = dict(
        id=finding_id,
        resource_ref="res-1",
        rule="test_rule",
        evidence={},
        tags_at_detection={},
        est_monthly_cost_usd=Decimal("10.00"),
        status=status,
        protected=False,
        detected_at=now,
        last_seen_at=now,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_upsert_resource(repository):
    """Test creating and updating a resource."""
    now = datetime.now(UTC)
    res = Resource(
        id="res-1",
        resource_id="vol-123",
        resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn",
        region="us-east-1",
        current_tags={"env": "dev"},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now
    )

    repository.upsert_resource(res)
    db_res = repository.get_resource_by_id("res-1")
    assert db_res is not None
    assert db_res.current_tags == {"env": "dev"}

    later = now + timedelta(days=1)
    res.current_tags = {"env": "prod"}
    res.last_seen_at = later
    repository.upsert_resource(res)

    db_res_updated = repository.get_resource_by_id("res-1")
    assert db_res_updated.current_tags == {"env": "prod"}
    assert db_res_updated.last_seen_at == later


def test_mark_unseen_resources_deleted(repository):
    """Test marking stale resources as deleted."""
    now = datetime.now(UTC)
    old = now - timedelta(days=2)

    res_active = Resource(
        id="res-1", resource_id="vol-123", resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn", region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now
    )
    res_stale = Resource(
        id="res-2", resource_id="vol-456", resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn", region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=old, last_seen_at=old
    )

    repository.upsert_resource(res_active)
    repository.upsert_resource(res_stale)

    repository.mark_unseen_resources_deleted(now - timedelta(days=1))

    assert repository.get_resource_by_id("res-1").lifecycle == ResourceLifecycle.ACTIVE
    assert repository.get_resource_by_id("res-2").lifecycle == ResourceLifecycle.DELETED


def test_save_finding_never_changes_status(repository):
    """A re-scan upsert must not move status — terminal states survive."""
    repository.save_finding(make_finding(status=FindingStatus.OPEN))
    assert repository.transition_finding("f-1", FindingStatus.OPEN, FindingStatus.NOTIFIED)
    assert repository.transition_finding("f-1", FindingStatus.NOTIFIED, FindingStatus.DENIED)

    # Rule fires again on the next scan: same id, incoming status OPEN.
    rescan = make_finding(status=FindingStatus.OPEN, evidence={"seen": "again"})
    assert repository.save_finding(rescan) is True

    db_f = repository.get_finding_by_id("f-1")
    assert db_f.status == FindingStatus.DENIED          # NOT resurrected
    assert db_f.evidence == {"seen": "again"}           # but evidence refreshed


def test_transition_finding_cas(repository):
    """CAS succeeds once and only from the expected status."""
    repository.save_finding(make_finding(status=FindingStatus.NOTIFIED))

    # Two racing approvers: both saw NOTIFIED. Exactly one wins.
    first = repository.transition_finding("f-1", FindingStatus.NOTIFIED, FindingStatus.APPROVED)
    second = repository.transition_finding("f-1", FindingStatus.NOTIFIED, FindingStatus.APPROVED)
    assert first is True
    assert second is False

    # Wrong expected status never transitions.
    assert repository.transition_finding("f-1", FindingStatus.OPEN, FindingStatus.DENIED) is False
    assert repository.get_finding_by_id("f-1").status == FindingStatus.APPROVED


def test_get_findings_filter_by_status(repository):
    repository.save_finding(make_finding("f-open", status=FindingStatus.OPEN))
    repository.save_finding(make_finding("f-notified", status=FindingStatus.NOTIFIED))

    assert {f.id for f in repository.get_findings()} == {"f-open", "f-notified"}
    only_open = repository.get_findings(status=FindingStatus.OPEN)
    assert [f.id for f in only_open] == ["f-open"]


def test_record_and_read_audit_and_decisions(repository):
    now = datetime.now(UTC)
    repository.record_audit(AuditEvent(ts=now, event="scan_completed", finding_id=None, detail={"n": 1}))
    repository.record_audit(AuditEvent(ts=now, event="finding_approved", finding_id="f-1", detail={}))
    repository.record_decision(Decision(
        finding_id="f-1", actor="boaz", action="approve", decided_at=now, channel="slack"
    ))

    all_events = repository.get_audit_events()
    assert [e.event for e in all_events] == ["scan_completed", "finding_approved"]
    scoped = repository.get_audit_events(finding_id="f-1")
    assert len(scoped) == 1 and scoped[0].event == "finding_approved"


def test_notifications_latest_time(repository):
    now = datetime.now(UTC)
    assert repository.get_latest_notification_time("f-1") is None
    repository.record_notification("f-1", "slack", None, now - timedelta(hours=2))
    repository.record_notification("f-1", "slack", None, now)
    latest = repository.get_latest_notification_time("f-1")
    assert abs((latest - now).total_seconds()) < 1


def test_record_remediation(repository):
    now = datetime.now(UTC)
    repository.record_remediation(
        finding_id="f-1",
        playbook="snapshot_then_delete_volume",
        dry_run=False,
        result="success",
        detail={"snapshot_id": "snap-123"},
        started_at=now,
        finished_at=now,
    )
    # No read port for remediations yet — assert via raw table.
    from finops_sentinel.adapters.persistence.sqlalchemy_repo import RemediationModel
    with repository.SessionLocal() as db:
        rows = db.query(RemediationModel).all()
        assert len(rows) == 1
        assert rows[0].result == "success"
        assert "snap-123" in rows[0].detail
