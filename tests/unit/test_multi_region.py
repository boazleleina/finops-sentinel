"""Multi-region scanning: config resolution, per-region isolation, and
region-correct remediation.

The behaviours worth pinning down are the failure modes, not the happy path.
A region that errors must not (a) end the scan, (b) get its inventory marked
DELETED, or (c) vanish silently into a "clean account" report.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from finops_sentinel import bootstrap
from finops_sentinel.config import Settings, settings
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.services import ScanTarget, approve_finding, run_scan
from finops_sentinel.ports.scanner import Scanner
from tests.fakes import FakeGatewayBase
from tests.fakes import make_resource as base_make_resource


def make_resource(resource_id, region, resource_type=ResourceType.EBS_VOLUME, seen=None):
    """This module's defaults over the shared constructor in tests.fakes."""
    return base_make_resource(
        res_id=f"res-{resource_id}",
        resource_id=resource_id,
        resource_type=resource_type,
        region=region,
        seen=seen,
    )


class RegionScanner(Scanner):
    """Discovers one volume in its region; records the inventory it evaluates."""

    def __init__(self, region, resource_id, fail=False):
        self.region = region
        self.resource_id = resource_id
        self.fail = fail
        self.evaluated_regions = []

    def discover(self, gateway):
        if self.fail:
            raise RuntimeError(f"{self.region} is unreachable")
        return [(make_resource(self.resource_id, self.region), {"State": "available"})]

    def evaluate(self, resources):
        self.evaluated_regions = sorted({res.region for res, _ in resources})
        now = datetime.now(UTC)
        return [
            Finding(
                id=f"ebs_unattached|{res.resource_id}",
                resource_ref=res.id,
                rule="ebs_unattached",
                evidence={},
                tags_at_detection={},
                est_monthly_cost_usd=Decimal("1.00"),
                status=FindingStatus.OPEN,
                protected=False,
                detected_at=now,
                last_seen_at=now,
            )
            for res, _ in resources
        ]


class RecordingGateway(FakeGatewayBase):
    """Knows which region it was built for; records what it executed."""

    def __init__(self, region):
        self.region = region
        self.executed = []

    def describe_ebs_volumes(self): return []
    def describe_elastic_ips(self): return []
    def describe_ec2_instances(self): return []
    def describe_ebs_snapshots(self): return []
    def describe_running_ec2_instances(self): return []

    def get_metric_averages(
        self, namespace, dimensions, metric_name, days, period_seconds=3600
    ):
        return []

    def execute(self, playbook, resource_id, dry_run):
        self.executed.append((playbook, resource_id, dry_run))
        return {"dry_run": dry_run}


# --------------------------------------------------------------------------
# Config resolution
# --------------------------------------------------------------------------


def test_unset_aws_regions_falls_back_to_home_region():
    assert Settings(aws_region="eu-west-2", aws_regions="").configured_regions == ["eu-west-2"]


def test_aws_regions_is_parsed_deduped_and_order_preserving():
    parsed = Settings(aws_regions=" us-east-1 , eu-west-1,us-east-1 ,, ap-south-1").configured_regions

    assert parsed == ["us-east-1", "eu-west-1", "ap-south-1"]


def test_all_sentinel_is_detected_case_insensitively():
    assert Settings(aws_regions="ALL").scans_all_regions is True
    assert Settings(aws_regions="us-east-1,eu-west-1").scans_all_regions is False


def test_get_regions_returns_explicit_list_without_calling_aws(monkeypatch):
    monkeypatch.setattr(settings, "aws_regions", "eu-west-1,ap-southeast-2")

    def explode(*args, **kwargs):
        raise AssertionError("explicit regions must not trigger discovery")

    monkeypatch.setattr(bootstrap, "list_enabled_regions", explode)

    assert bootstrap.get_regions() == ["eu-west-1", "ap-southeast-2"]


def test_get_regions_all_discovers_enabled_regions(monkeypatch):
    monkeypatch.setattr(settings, "aws_regions", "all")

    with mock_aws():
        discovered = bootstrap.get_regions()

    assert "us-east-1" in discovered
    assert "eu-west-1" in discovered
    assert len(discovered) > 5


def test_all_regions_excludes_regions_the_account_never_opted_into(monkeypatch):
    """Every call against a not-opted-in region fails with AuthFailure — a scan
    of one is pure noise."""
    monkeypatch.setattr(settings, "aws_regions", "all")

    with mock_aws():
        client = boto3.client("ec2", region_name="us-east-1")
        all_names = {
            entry["RegionName"] for entry in client.describe_regions(AllRegions=True)["Regions"]
        }
        opted_out = {
            entry["RegionName"]
            for entry in client.describe_regions(AllRegions=True)["Regions"]
            if entry.get("OptInStatus") == "not-opted-in"
        }
        discovered = set(bootstrap.get_regions())

    assert discovered <= all_names
    assert discovered.isdisjoint(opted_out)


def test_failed_region_discovery_degrades_to_home_region_loudly(monkeypatch, caplog):
    """Silently scanning one region when asked for all looks identical to an
    account with nothing to find."""
    monkeypatch.setattr(settings, "aws_regions", "all")
    monkeypatch.setattr(settings, "aws_region", "us-east-1")

    def denied(*args, **kwargs):
        raise RuntimeError("AccessDenied: ec2:DescribeRegions")

    monkeypatch.setattr(bootstrap, "list_enabled_regions", denied)

    with caplog.at_level("ERROR"):
        assert bootstrap.get_regions() == ["us-east-1"]

    assert "DescribeRegions" in caplog.text


def test_scan_targets_get_their_own_scanner_instances(monkeypatch):
    """Sharing scanners across regions would stamp the wrong region on
    resources and let IdleEC2Scanner's cached metrics leak between regions."""
    monkeypatch.setattr(settings, "aws_regions", "us-east-1,eu-west-1")
    monkeypatch.setattr(settings, "aws_endpoint_url", None)

    with mock_aws():
        targets = bootstrap.get_scan_targets()

    assert [t.region for t in targets] == ["us-east-1", "eu-west-1"]
    assert {s.region for s in targets[0].scanners} == {"us-east-1"}
    assert {s.region for s in targets[1].scanners} == {"eu-west-1"}
    east_ids = {id(s) for s in targets[0].scanners}
    assert east_ids.isdisjoint({id(s) for s in targets[1].scanners})


# --------------------------------------------------------------------------
# Scanning across regions
# --------------------------------------------------------------------------


def test_scan_covers_every_region(repository):
    targets = [
        ScanTarget("us-east-1", None, [RegionScanner("us-east-1", "vol-east")]),
        ScanTarget("eu-west-1", None, [RegionScanner("eu-west-1", "vol-west")]),
    ]

    result = run_scan(targets, repository)

    assert result.regions_scanned == ["eu-west-1", "us-east-1"]
    assert {f.id for f in result.findings} == {
        "ebs_unattached|vol-east",
        "ebs_unattached|vol-west",
    }
    stored = {res.resource_id: res.region for res in repository.get_all_resources()}
    assert stored == {"vol-east": "us-east-1", "vol-west": "eu-west-1"}


def test_evaluate_sees_only_its_own_regions_inventory(repository):
    """Cross-region pooling would let a volume in one region vouch for a
    snapshot in another, and hand region-A resources to region-B scanners."""
    east = RegionScanner("us-east-1", "vol-east")
    west = RegionScanner("eu-west-1", "vol-west")

    run_scan(
        [ScanTarget("us-east-1", None, [east]), ScanTarget("eu-west-1", None, [west])],
        repository,
    )

    assert east.evaluated_regions == ["us-east-1"]
    assert west.evaluated_regions == ["eu-west-1"]


def test_parallel_discovery_produces_the_same_result(repository):
    targets = [
        ScanTarget(f"region-{i}", None, [RegionScanner(f"region-{i}", f"vol-{i}")])
        for i in range(6)
    ]

    result = run_scan(targets, repository, max_workers=6)

    assert len(result.findings) == 6
    assert len(result.regions_scanned) == 6


def test_one_failing_region_does_not_end_the_scan(repository):
    targets = [
        ScanTarget("us-east-1", None, [RegionScanner("us-east-1", "vol-east")]),
        ScanTarget("eu-west-1", None, [RegionScanner("eu-west-1", "vol-west", fail=True)]),
    ]

    result = run_scan(targets, repository, max_workers=2)

    assert [f.id for f in result.findings] == ["ebs_unattached|vol-east"]
    assert result.regions_scanned == ["us-east-1"]
    assert "eu-west-1" in result.regions_failed
    assert "unreachable" in result.regions_failed["eu-west-1"]
    assert any(
        event.event == "region_scan_failed" and event.detail["region"] == "eu-west-1"
        for event in repository.get_audit_events()
    )


def test_failed_region_keeps_its_inventory_active(repository):
    """DELETED resources are refused by approve_finding. A transient API error
    in one region must not disarm every finding it owns."""
    stale = datetime.now(UTC) - timedelta(days=1)
    repository.upsert_resource(make_resource("vol-west", "eu-west-1", seen=stale))
    repository.upsert_resource(make_resource("vol-gone", "us-east-1", seen=stale))

    run_scan(
        [
            ScanTarget("us-east-1", None, [RegionScanner("us-east-1", "vol-east")]),
            ScanTarget("eu-west-1", None, [RegionScanner("eu-west-1", "vol-west", fail=True)]),
        ],
        repository,
    )

    lifecycles = {res.resource_id: res.lifecycle for res in repository.get_all_resources()}
    # us-east-1 was scanned and did not see vol-gone: it really is gone.
    assert lifecycles["vol-gone"] == ResourceLifecycle.DELETED
    # eu-west-1 was never reached, so its inventory stays trustworthy.
    assert lifecycles["vol-west"] == ResourceLifecycle.ACTIVE


def test_unscanned_regions_are_never_swept(repository):
    """Dropping a region from AWS_REGIONS must not mass-delete its inventory."""
    stale = datetime.now(UTC) - timedelta(days=1)
    repository.upsert_resource(make_resource("vol-south", "ap-south-1", seen=stale))

    run_scan([ScanTarget("us-east-1", None, [RegionScanner("us-east-1", "vol-east")])], repository)

    stored = {res.resource_id: res.lifecycle for res in repository.get_all_resources()}
    assert stored["vol-south"] == ResourceLifecycle.ACTIVE


def test_total_failure_raises_instead_of_reporting_a_clean_account(repository):
    targets = [
        ScanTarget("us-east-1", None, [RegionScanner("us-east-1", "vol-east", fail=True)]),
        ScanTarget("eu-west-1", None, [RegionScanner("eu-west-1", "vol-west", fail=True)]),
    ]

    with pytest.raises(RuntimeError, match="Every region failed"):
        run_scan(targets, repository, max_workers=2)

    # The failures are still on the record for whoever investigates.
    failures = [e for e in repository.get_audit_events() if e.event == "region_scan_failed"]
    assert {e.detail["region"] for e in failures} == {"us-east-1", "eu-west-1"}


def test_end_to_end_two_regions_through_real_adapters(repository, monkeypatch):
    """The whole path: real Boto3Gateway per region, real scanners, one scan."""
    monkeypatch.setattr(settings, "aws_regions", "us-east-1,eu-west-1")
    monkeypatch.setattr(settings, "aws_endpoint_url", None)

    with mock_aws():
        east = boto3.client("ec2", region_name="us-east-1")
        west = boto3.client("ec2", region_name="eu-west-1")
        east_vol = east.create_volume(
            AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3"
        )["VolumeId"]
        west_vol = west.create_volume(
            AvailabilityZone="eu-west-1a", Size=50, VolumeType="gp2"
        )["VolumeId"]

        result = run_scan(bootstrap.get_scan_targets(), repository, max_workers=2)

    found = {f.id for f in result.findings}
    assert f"ebs_unattached|{east_vol}" in found
    assert f"ebs_unattached|{west_vol}" in found

    stored = {res.resource_id: res for res in repository.get_all_resources()}
    assert stored[east_vol].region == "us-east-1"
    assert stored[west_vol].region == "eu-west-1"
    # The region has to reach the ARN too — it is what an operator pastes into
    # the console, and what a future cross-account gateway would parse.
    assert stored[west_vol].resource_arn.startswith("arn:aws:ec2:eu-west-1:")


# --------------------------------------------------------------------------
# Remediation
# --------------------------------------------------------------------------


def test_remediation_uses_the_gateway_for_the_findings_own_region(repository):
    """An eu-west-1 volume deleted through the us-east-1 endpoint fails with
    InvalidVolume.NotFound, which reads as "already gone" rather than
    "wrong region"."""
    now = datetime.now(UTC)
    repository.upsert_resource(make_resource("vol-west", "eu-west-1"))
    repository.save_finding(
        Finding(
            id="f-west",
            resource_ref="res-vol-west",
            rule="ebs_unattached",
            evidence={},
            tags_at_detection={},
            est_monthly_cost_usd=Decimal("1.00"),
            status=FindingStatus.NOTIFIED,
            protected=False,
            detected_at=now,
            last_seen_at=now,
        )
    )

    gateways = {region: RecordingGateway(region) for region in ("us-east-1", "eu-west-1")}
    requested = []

    def resolve(plan):
        requested.append(plan.region)
        return gateways[plan.region]

    approved = approve_finding(
        "f-west", repository, resolve, actor="boaz", channel="api", dry_run=False
    )

    assert approved is True
    assert requested == ["eu-west-1"]
    assert gateways["eu-west-1"].executed == [
        ("snapshot_then_delete_volume", "vol-west", False)
    ]
    assert gateways["us-east-1"].executed == []


def test_unresolvable_region_is_recorded_as_a_failed_remediation(repository):
    """Otherwise the finding strands in APPROVED with no trace of why."""
    now = datetime.now(UTC)
    repository.upsert_resource(make_resource("vol-west", "eu-west-1"))
    repository.save_finding(
        Finding(
            id="f-west",
            resource_ref="res-vol-west",
            rule="ebs_unattached",
            evidence={},
            tags_at_detection={},
            est_monthly_cost_usd=Decimal("1.00"),
            status=FindingStatus.NOTIFIED,
            protected=False,
            detected_at=now,
            last_seen_at=now,
        )
    )

    def resolve(plan):
        raise RuntimeError(f"no credentials for {plan.region}")

    with pytest.raises(RuntimeError):
        approve_finding(
            "f-west", repository, resolve, actor="boaz", channel="api", dry_run=False
        )

    assert repository.get_finding_by_id("f-west").status == FindingStatus.FAILED
    assert any(
        e.event == "remediation_failed" for e in repository.get_audit_events("f-west")
    )
