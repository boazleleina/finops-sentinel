from datetime import datetime, UTC
from decimal import Decimal

from finops_sentinel.domain.models import Resource, Finding, ResourceLifecycle, ResourceType, FindingStatus
from finops_sentinel.domain.services import run_scan, approve_finding, deny_finding, expire_stale
from finops_sentinel.ports.scanner import Scanner
from finops_sentinel.ports.cloud import CloudGateway

class MockScanner(Scanner):
    def discover(self, gateway):
        # Return dummy resource
        res = Resource(
            id="res-mock",
            resource_id="mock-123",
            resource_type=ResourceType.EBS_VOLUME,
            resource_arn="arn",
            region="us-east-1",
            current_tags={},
            lifecycle=ResourceLifecycle.ACTIVE,
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC)
        )
        return [(res, {"mock_data": True})]

    def evaluate(self, discover_results):
        res = discover_results[0][0]
        finding = Finding(
            id="f-mock",
            resource_ref=res.id,
            rule="mock_rule",
            evidence={},
            tags_at_detection={},
            est_monthly_cost_usd=Decimal("1.00"),
            status=FindingStatus.OPEN,
            protected=False,
            detected_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC)
        )
        return [finding]

def test_run_scan_orchestrator(repository):
    """Test that run_scan executes the two-pass logic properly."""
    # We don't need a real gateway for the mock scanner
    gateway = None
    scanners = [MockScanner()]
    
    findings = run_scan(gateway, repository, scanners)
    
    assert len(findings) == 1
    assert findings[0].rule == "mock_rule"
    
    # Verify persistence
    db_resources = repository.get_all_resources()
    assert len(db_resources) == 1
    assert db_resources[0].resource_id == "mock-123"
    
    db_findings = repository.get_findings()
    assert len(db_findings) == 1
    assert db_findings[0].id == "f-mock"

class FakeCloudGateway(CloudGateway):
    def __init__(self):
        self.snapshots = []
        self.deleted_volumes = []
        self.released_eips = []
        self.terminated_instances = []

    def describe_ebs_volumes(self): return []
    def describe_elastic_ips(self): return []
    def describe_ec2_instances(self): return []

    def release_elastic_ip(self, allocation_id: str) -> None:
        self.released_eips.append(allocation_id)

    def terminate_instance(self, instance_id: str) -> None:
        self.terminated_instances.append(instance_id)

    def snapshot_and_delete_volume(self, volume_id: str) -> None:
        self.snapshots.append(f"snap-{volume_id}")
        self.deleted_volumes.append(volume_id)

def test_approve_finding(repository):
    gateway = FakeCloudGateway()
    
    # Setup open finding
    res = Resource(
        id="res-mock", resource_id="vol-123", resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn", region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC)
    )
    repository.upsert_resource(res)
    
    finding = Finding(
        id="f-mock", resource_ref="res-mock", rule="ebs", evidence={},
        tags_at_detection={}, est_monthly_cost_usd=Decimal("1.00"),
        status=FindingStatus.NOTIFIED, protected=False,
        detected_at=datetime.now(UTC), last_seen_at=datetime.now(UTC)
    )
    repository.save_finding(finding)

    # Execute Approval
    success = approve_finding("f-mock", repository, gateway)
    assert success is True
    
    # Assert remediation happened
    assert "vol-123" in gateway.deleted_volumes
    
    # Assert state changed
    f_db = repository.get_finding_by_id("f-mock")
    assert f_db.status == FindingStatus.REMEDIATED

def test_deny_finding(repository):
    res = Resource(
        id="res-mock", resource_id="vol-123", resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn", region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC)
    )
    repository.upsert_resource(res)
    
    finding = Finding(
        id="f-mock", resource_ref="res-mock", rule="ebs", evidence={},
        tags_at_detection={}, est_monthly_cost_usd=Decimal("1.00"),
        status=FindingStatus.NOTIFIED, protected=False,
        detected_at=datetime.now(UTC), last_seen_at=datetime.now(UTC)
    )
    repository.save_finding(finding)

    success = deny_finding("f-mock", repository)
    assert success is True
    
    f_db = repository.get_finding_by_id("f-mock")
    assert f_db.status == FindingStatus.DENIED

def test_expire_stale(repository):
    # Setup stale finding (4 days old)
    import datetime as dt
    stale_time = dt.datetime.now(UTC) - dt.timedelta(days=4)
    
    finding = Finding(
        id="f-stale", resource_ref="res-mock", rule="ebs", evidence={},
        tags_at_detection={}, est_monthly_cost_usd=Decimal("1.00"),
        status=FindingStatus.NOTIFIED, protected=False,
        detected_at=stale_time, last_seen_at=stale_time
    )
    repository.save_finding(finding)
    
    # Setup fresh finding (1 hour old)
    fresh_time = dt.datetime.now(UTC) - dt.timedelta(hours=1)
    finding2 = Finding(
        id="f-fresh", resource_ref="res-mock", rule="ebs", evidence={},
        tags_at_detection={}, est_monthly_cost_usd=Decimal("1.00"),
        status=FindingStatus.NOTIFIED, protected=False,
        detected_at=fresh_time, last_seen_at=fresh_time
    )
    repository.save_finding(finding2)
    
    expire_stale(repository)
    
    stale_db = repository.get_finding_by_id("f-stale")
    assert stale_db.status == FindingStatus.EXPIRED
    
    fresh_db = repository.get_finding_by_id("f-fresh")
    assert fresh_db.status == FindingStatus.NOTIFIED
