"""End-to-end integration test against LocalStack, through the real adapters:
seed → scan → approve via the real API → resource actually gone → audit trail
complete. Skipped automatically when LocalStack is not running.
"""
import urllib.request

import boto3
import pytest
from fastapi.testclient import TestClient

from finops_sentinel.adapters.inbound import fastapi_app
from finops_sentinel.adapters.persistence.sqlalchemy_repo import Base, SqlAlchemyRepository
from finops_sentinel.bootstrap import get_notifier, get_scan_targets
from finops_sentinel.config import settings
from finops_sentinel.domain.models import FindingStatus
from finops_sentinel.domain.services import notify_open_findings, run_scan

LOCALSTACK_URL = "http://localhost:4566"


def localstack_running() -> bool:
    try:
        urllib.request.urlopen(f"{LOCALSTACK_URL}/_localstack/health", timeout=2)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not localstack_running(), reason="LocalStack is not running on localhost:4566"
)


@pytest.fixture
def localstack_env(tmp_path):
    """Point settings at LocalStack + a fresh temp DB, live (non-dry-run) mode."""
    settings.aws_endpoint_url = LOCALSTACK_URL
    settings.aws_regions = "us-east-1"     # pin: a developer .env may list more
    settings.aws_access_key_id = "test"
    settings.aws_secret_access_key = "test"
    settings.sentinel_db_path = str(tmp_path / "integration.db")
    settings.dry_run = False
    settings.slack_webhook_url = None      # console notifier — no real Slack in CI
    settings.slack_signing_secret = None

    repo = SqlAlchemyRepository(f"sqlite:///{settings.sentinel_db_path}")
    Base.metadata.create_all(repo.engine)

    ec2 = boto3.client(
        "ec2",
        region_name="us-east-1",
        endpoint_url=LOCALSTACK_URL,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    yield repo, ec2
    repo.engine.dispose()


def test_rds_scanners_degrade_without_taking_the_scan_down(localstack_env):
    """The documented RDS coverage gap, asserted rather than assumed.

    LocalStack's RDS support is Pro-tier, so DescribeDBInstances fails here the
    same way it would against an account whose IAM policy omits it. That makes
    this the live proof of per-scanner isolation: the RDS scanners are reported
    as failed, and every other scanner still returns its findings.

    The RDS scanners themselves have no end-to-end coverage anywhere — moto
    unit tests are the whole safety net for their logic. Revisited in Phase 6
    against a real account in read-only DRY_RUN mode.
    """
    repo, _ = localstack_env

    result = run_scan(get_scan_targets(), repo)

    failed = set(result.scanners_failed)
    assert "us-east-1/IdleRDSScanner" in failed
    assert "us-east-1/StoppedRDSScanner" in failed
    # The point of the isolation: the region still scanned.
    assert result.regions_scanned == ["us-east-1"]
    assert result.regions_failed == {}
    assert any(
        e.event == "scanner_failed" for e in repo.get_audit_events()
    )


def test_full_loop_scan_approve_deleted_audited(localstack_env):
    repo, ec2 = localstack_env

    volume_id = ec2.create_volume(AvailabilityZone="us-east-1a", Size=1, VolumeType="gp3")[
        "VolumeId"
    ]
    try:
        # Scan through the real gateway + scanners, notify via console notifier
        findings = run_scan(get_scan_targets(), repo).findings
        assert any(f.id == f"ebs_unattached|{volume_id}" for f in findings)
        notify_open_findings(repo, get_notifier())
        finding_id = f"ebs_unattached|{volume_id}"
        assert repo.get_finding_by_id(finding_id).status == FindingStatus.NOTIFIED

        # Approve through the real API
        client = TestClient(fastapi_app.app)
        response = client.post(
            f"/decisions/{finding_id}", json={"action": "approve", "actor": "integration-test"}
        )
        assert response.status_code == 200, response.text

        # Resource actually gone
        volumes = ec2.describe_volumes(
            Filters=[{"Name": "volume-id", "Values": [volume_id]}]
        )["Volumes"]
        assert volumes == []

        # Snapshot-before-delete happened
        snapshots = ec2.describe_snapshots(
            Filters=[{"Name": "volume-id", "Values": [volume_id]}]
        )["Snapshots"]
        assert len(snapshots) >= 1

        # State machine landed on REMEDIATED, audit trail complete
        assert repo.get_finding_by_id(finding_id).status == FindingStatus.REMEDIATED
        events = {e.event for e in repo.get_audit_events(finding_id)}
        assert {"finding_notified", "finding_approved", "remediation_executed"} <= events
    finally:
        # Best-effort cleanup if an assertion failed before deletion
        try:
            ec2.delete_volume(VolumeId=volume_id)
        except Exception:
            pass
        # Remove the snapshot debris this test created
        try:
            for snap in ec2.describe_snapshots(
                Filters=[{"Name": "volume-id", "Values": [volume_id]}]
            )["Snapshots"]:
                ec2.delete_snapshot(SnapshotId=snap["SnapshotId"])
        except Exception:
            pass
