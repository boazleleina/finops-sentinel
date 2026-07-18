import pytest
from datetime import datetime, UTC
from decimal import Decimal
from pydantic import ValidationError

from finops_sentinel.domain.models import (
    Finding, Resource, FindingStatus, ResourceLifecycle, ResourceType, TRANSITIONS
)

def test_finding_state_transitions():
    """Verify that the TRANSITIONS dictionary correctly defines the state machine."""
    # OPEN can only go to NOTIFIED
    assert FindingStatus.NOTIFIED in TRANSITIONS[FindingStatus.OPEN]
    assert FindingStatus.APPROVED not in TRANSITIONS[FindingStatus.OPEN]
    
    # NOTIFIED can go to APPROVED, DENIED, or EXPIRED
    assert FindingStatus.APPROVED in TRANSITIONS[FindingStatus.NOTIFIED]
    assert FindingStatus.DENIED in TRANSITIONS[FindingStatus.NOTIFIED]
    assert FindingStatus.EXPIRED in TRANSITIONS[FindingStatus.NOTIFIED]
    
    # APPROVED can go to REMEDIATED or FAILED
    assert FindingStatus.REMEDIATED in TRANSITIONS[FindingStatus.APPROVED]
    assert FindingStatus.FAILED in TRANSITIONS[FindingStatus.APPROVED]

def test_create_valid_resource():
    """Verify Pydantic model creation for Resource."""
    now = datetime.now(UTC)
    res = Resource(
        id="res-1",
        resource_id="vol-123",
        resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn:aws:ec2:us-east-1:123:volume/vol-123",
        region="us-east-1",
        current_tags={"env": "prod"},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now
    )
    assert res.resource_id == "vol-123"
    assert res.lifecycle == ResourceLifecycle.ACTIVE

def test_invalid_resource_type_fails():
    """Verify Pydantic validation catches invalid enums."""
    with pytest.raises(ValidationError):
        Resource(
            id="res-1",
            resource_id="vol-123",
            resource_type="invalid_type", # Should fail
            resource_arn="arn",
            region="us-east-1",
            current_tags={},
            lifecycle=ResourceLifecycle.ACTIVE,
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC)
        )

def test_create_finding():
    """Verify Pydantic model creation for Finding."""
    now = datetime.now(UTC)
    f = Finding(
        id="f-1",
        resource_ref="res-1",
        rule="ebs_unattached",
        evidence={"state": "available"},
        tags_at_detection={"env": "prod"},
        est_monthly_cost_usd=Decimal("1.50"),
        status=FindingStatus.OPEN,
        protected=False,
        detected_at=now,
        last_seen_at=now
    )
    assert f.status == FindingStatus.OPEN
    assert f.est_monthly_cost_usd == Decimal("1.50")
