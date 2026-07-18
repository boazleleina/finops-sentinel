from datetime import datetime, UTC, timedelta
from decimal import Decimal

from finops_sentinel.domain.models import (
    Finding, Resource, FindingStatus, ResourceLifecycle, ResourceType
)

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
    
    # Insert
    repository.upsert_resource(res)
    db_res = repository.get_resource_by_id("res-1")
    assert db_res is not None
    assert db_res.current_tags == {"env": "dev"}
    
    # Update
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
        id="res-1",
        resource_id="vol-123",
        resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn",
        region="us-east-1",
        current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now
    )
    res_stale = Resource(
        id="res-2",
        resource_id="vol-456",
        resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn",
        region="us-east-1",
        current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=old,
        last_seen_at=old
    )
    
    repository.upsert_resource(res_active)
    repository.upsert_resource(res_stale)
    
    cutoff = now - timedelta(days=1)
    repository.mark_unseen_resources_deleted(cutoff)
    
    db_active = repository.get_resource_by_id("res-1")
    db_stale = repository.get_resource_by_id("res-2")
    
    assert db_active.lifecycle == ResourceLifecycle.ACTIVE
    assert db_stale.lifecycle == ResourceLifecycle.DELETED

def test_save_finding_cas_logic(repository):
    """Test atomic CAS (Compare-And-Swap) logic for findings."""
    now = datetime.now(UTC)
    finding = Finding(
        id="f-1",
        resource_ref="res-1",
        rule="test_rule",
        evidence={},
        tags_at_detection={},
        est_monthly_cost_usd=Decimal("10.00"),
        status=FindingStatus.OPEN,
        protected=False,
        detected_at=now,
        last_seen_at=now
    )
    
    # Insert new finding
    success = repository.save_finding(finding)
    assert success is True
    
    # Simple update (status hasn't changed)
    later = now + timedelta(days=1)
    finding.last_seen_at = later
    success = repository.save_finding(finding)
    assert success is True
    
    # Status transition
    finding.status = FindingStatus.NOTIFIED
    success = repository.save_finding(finding)
    assert success is True
    
    db_findings = repository.get_findings()
    assert len(db_findings) == 1
    assert db_findings[0].status == FindingStatus.NOTIFIED
