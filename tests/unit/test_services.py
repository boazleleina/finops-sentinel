from datetime import datetime, UTC
from decimal import Decimal

from finops_sentinel.domain.models import Resource, Finding, ResourceLifecycle, ResourceType, FindingStatus
from finops_sentinel.domain.services import run_scan
from finops_sentinel.ports.scanner import Scanner

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
