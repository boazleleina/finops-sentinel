from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.adapters.aws.scanners.ebs import UnattachedEBSScanner
from finops_sentinel.adapters.aws.scanners.ebs_snapshots import OldEbsSnapshotScanner
from finops_sentinel.adapters.aws.scanners.ec2 import StoppedEC2Scanner, parse_stop_time
from finops_sentinel.adapters.aws.scanners.eip import OrphanedEIPScanner
from finops_sentinel.domain.models import Resource, ResourceLifecycle, ResourceType


def test_ebs_scanner(mock_aws_env):
    """Test EBS scanner discover and evaluate passes."""
    ec2 = mock_aws_env
    
    # Create an unattached GP2 volume
    vol_gp2 = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50, VolumeType="gp2")
    # Create an unattached GP3 volume
    vol_gp3 = ec2.create_volume(AvailabilityZone="us-east-1a", Size=100, VolumeType="gp3")
    
    gateway = Boto3Gateway(region="us-east-1")
    scanner = UnattachedEBSScanner(region="us-east-1", pricing=StaticPricing())
    
    # Pass 1: Discover
    resources = scanner.discover(gateway)
    assert len(resources) == 2
    
    res_gp2 = next(r[0] for r in resources if r[0].resource_id == vol_gp2["VolumeId"])
    res_gp3 = next(r[0] for r in resources if r[0].resource_id == vol_gp3["VolumeId"])
    
    assert res_gp2.resource_type == ResourceType.EBS_VOLUME
    
    # Pass 2: Evaluate
    findings = scanner.evaluate(resources)
    assert len(findings) == 2
    
    finding_gp2 = next(f for f in findings if f.resource_ref == res_gp2.id)
    finding_gp3 = next(f for f in findings if f.resource_ref == res_gp3.id)
    
    # GP2: 50 * 0.10 = 5.00
    assert finding_gp2.est_monthly_cost_usd == Decimal("5.00")
    # GP3: 100 * 0.08 = 8.00
    assert finding_gp3.est_monthly_cost_usd == Decimal("8.00")

def test_eip_scanner(mock_aws_env):
    """Test EIP scanner discover and evaluate passes."""
    ec2 = mock_aws_env
    
    # Create unattached EIP
    eip = ec2.allocate_address(Domain="vpc")
    
    # Create attached EIP (need VPC, Subnet, Instance to attach properly in moto)
    # We will just test the unattached one for now.
    
    gateway = Boto3Gateway(region="us-east-1")
    scanner = OrphanedEIPScanner(region="us-east-1", pricing=StaticPricing())
    
    resources = scanner.discover(gateway)
    assert len(resources) == 1
    
    res_eip = resources[0][0]
    assert res_eip.resource_id == eip["AllocationId"]
    assert res_eip.resource_type == ResourceType.ELASTIC_IP
    
    findings = scanner.evaluate(resources)
    assert len(findings) == 1
    assert findings[0].est_monthly_cost_usd == Decimal("3.65")
    assert findings[0].resource_ref == res_eip.id

def test_ec2_scanner(mock_aws_env):
    """Test EC2 scanner discover and evaluate passes."""
    ec2 = mock_aws_env
    
    # Create a running instance (not stopped, should be ignored by scanner logic usually, 
    # but discover_instances only looks for stopped in gateway!)
    
    # Moto setup to run instances
    res = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1, InstanceType="t2.micro")
    instance_id = res["Instances"][0]["InstanceId"]
    
    # Stop the instance so it's picked up
    ec2.stop_instances(InstanceIds=[instance_id])
    
    gateway = Boto3Gateway(region="us-east-1")
    # threshold_days=0 so the just-stopped moto instance still qualifies
    scanner = StoppedEC2Scanner(region="us-east-1", pricing=StaticPricing(), threshold_days=0)

    resources = scanner.discover(gateway)
    assert len(resources) == 1

    res_ec2 = resources[0][0]
    assert res_ec2.resource_id == instance_id
    assert res_ec2.resource_type == ResourceType.EC2_INSTANCE

    findings = scanner.evaluate(resources)
    assert len(findings) == 1
    # Cost is now the instance's real attached EBS volumes, not a flat placeholder.
    assert findings[0].est_monthly_cost_usd > Decimal(0)
    assert findings[0].evidence["cost_basis"] in {"attached EBS volumes", "assumed root volume"}


def test_ec2_scanner_threshold(mock_aws_env):
    """Instances stopped less than threshold_days ago are not flagged."""
    def instance_tuple(instance_id, stopped_at, state="stopped"):
        now = datetime.now(UTC)
        resource = Resource(
            id=f"res-{instance_id}", resource_id=instance_id,
            resource_type=ResourceType.EC2_INSTANCE, resource_arn="arn",
            region="us-east-1", current_tags={},
            lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now,
        )
        reason = (
            f"User initiated ({stopped_at.strftime('%Y-%m-%d %H:%M:%S')} GMT)"
            if stopped_at else ""
        )
        return resource, {
            "InstanceId": instance_id,
            "StateTransitionReason": reason,
            "State": {"Name": state},
        }

    now = datetime.now(UTC)
    scanner = StoppedEC2Scanner(region="us-east-1", pricing=StaticPricing(), threshold_days=7)
    findings = scanner.evaluate([
        instance_tuple("i-fresh", now - timedelta(days=1)),    # under threshold: skipped
        instance_tuple("i-old", now - timedelta(days=30)),     # over threshold: flagged
        instance_tuple("i-unknown", None),                     # unknown stop time: flagged
        # run_scan hands every scanner the combined inventory, so a RUNNING
        # instance discovered by IdleEC2Scanner reaches this evaluate() too.
        # It has no StateTransitionReason, which must NOT read as "unknown
        # stop time, flag it anyway".
        instance_tuple("i-running", None, state="running"),
    ])

    flagged = {f.evidence["InstanceId"] for f in findings}
    assert flagged == {"i-old", "i-unknown"}

    # parse_stop_time handles the AWS format and garbage
    assert parse_stop_time("User initiated (2026-07-01 12:00:00 GMT)") is not None
    assert parse_stop_time("") is None
    assert parse_stop_time("weird string") is None

def test_ebs_snapshot_scanner(mock_aws_env):
    ec2 = mock_aws_env
    
    # 1. Live volume with fresh snapshot
    vol_live = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
    snap_live_fresh = ec2.create_snapshot(VolumeId=vol_live["VolumeId"])
    
    # 2. Deleted volume with fresh snapshot (orphaned)
    vol_del = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
    snap_orphaned = ec2.create_snapshot(VolumeId=vol_del["VolumeId"])
    ec2.delete_volume(VolumeId=vol_del["VolumeId"])
    
    # 3. Live volume with protected fresh snapshot (orphaned)
    vol_prot = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
    snap_protected = ec2.create_snapshot(
        VolumeId=vol_prot["VolumeId"],
        TagSpecifications=[{'ResourceType': 'snapshot', 'Tags': [{'Key': 'finops:protected', 'Value': 'true'}]}]
    )
    ec2.delete_volume(VolumeId=vol_prot["VolumeId"])
    
    gateway = Boto3Gateway(region="us-east-1")
    scanner = OldEbsSnapshotScanner(
        region="us-east-1", pricing=StaticPricing(), age_threshold_days=30
    )
    
    vol_scanner = UnattachedEBSScanner(region="us-east-1", pricing=StaticPricing())
    vol_resources = vol_scanner.discover(gateway)
    
    snap_resources = scanner.discover(gateway)
    
    # Force one snapshot to be old
    old_snap_id = snap_live_fresh["SnapshotId"]
    for res, raw in snap_resources:
        if raw["SnapshotId"] == old_snap_id:
            raw["StartTime"] = datetime.now(UTC) - timedelta(days=40)
            
    all_resources = vol_resources + snap_resources
    findings = scanner.evaluate(all_resources)
    
    assert len(findings) == 3
    
    flagged_snap_ids = {f.evidence["SnapshotId"]: f for f in findings}
    
    assert old_snap_id in flagged_snap_ids
    assert not flagged_snap_ids[old_snap_id].protected
    
    assert snap_orphaned["SnapshotId"] in flagged_snap_ids
    assert not flagged_snap_ids[snap_orphaned["SnapshotId"]].protected
    
    assert snap_protected["SnapshotId"] in flagged_snap_ids
    assert flagged_snap_ids[snap_protected["SnapshotId"]].protected


def test_gateway_describe_running_instances_excludes_stopped(mock_aws_env):
    """The two EC2 rules need disjoint views: stopped vs running."""
    ec2 = mock_aws_env
    running = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1)
    stopped = ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1)
    stopped_id = stopped["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[stopped_id])

    gateway = Boto3Gateway(region="us-east-1")

    running_ids = {i["InstanceId"] for i in gateway.describe_running_ec2_instances()}
    stopped_ids = {i["InstanceId"] for i in gateway.describe_ec2_instances()}

    assert running_ids == {running["Instances"][0]["InstanceId"]}
    assert stopped_ids == {stopped_id}
    assert running_ids.isdisjoint(stopped_ids)


def test_gateway_metric_averages_are_ordered_oldest_first(mock_aws_env):
    import boto3

    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    now = datetime.now(UTC)
    # Publish out of chronological order to prove the adapter sorts.
    for offset_hours, value in ((1, 3.0), (5, 1.0), (3, 2.0)):
        cloudwatch.put_metric_data(
            Namespace="AWS/EC2",
            MetricData=[{
                "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-metrics"}],
                "Timestamp": now - timedelta(hours=offset_hours),
                "Value": value,
                "Unit": "Percent",
            }],
        )

    gateway = Boto3Gateway(region="us-east-1")
    averages = gateway.get_metric_averages(
        namespace="AWS/EC2",
        dimension_name="InstanceId",
        dimension_value="i-metrics",
        metric_name="CPUUtilization",
        days=1,
    )

    assert averages == [1.0, 2.0, 3.0]


def test_gateway_metric_averages_empty_for_unknown_instance(mock_aws_env):
    """No data must read as 'unknown' (empty), never as 'idle' (zeros)."""
    gateway = Boto3Gateway(region="us-east-1")

    assert (
        gateway.get_metric_averages(
            namespace="AWS/EC2",
            dimension_name="InstanceId",
            dimension_value="i-nothing",
            metric_name="CPUUtilization",
            days=14,
        )
        == []
    )


def test_gateway_metric_averages_reads_any_namespace(mock_aws_env):
    """One method serves every service — the RDS/S3 scanners depend on this."""
    import boto3

    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    cloudwatch.put_metric_data(
        Namespace="AWS/RDS",
        MetricData=[{
            "MetricName": "DatabaseConnections",
            "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "db-quiet"}],
            "Timestamp": datetime.now(UTC) - timedelta(hours=2),
            "Value": 0.0,
        }],
    )

    gateway = Boto3Gateway(region="us-east-1")

    assert gateway.get_metric_averages(
        namespace="AWS/RDS",
        dimension_name="DBInstanceIdentifier",
        dimension_value="db-quiet",
        metric_name="DatabaseConnections",
        days=1,
    ) == [0.0]


def _stopped_pair(instance_id, volume_ids):
    """A stopped instance plus the Resource wrapper run_scan would produce."""
    now = datetime.now(UTC)
    resource = Resource(
        id=f"res-{instance_id}", resource_id=instance_id,
        resource_type=ResourceType.EC2_INSTANCE, resource_arn="arn",
        region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now,
    )
    raw = {
        "InstanceId": instance_id,
        "State": {"Name": "stopped"},
        "StateTransitionReason": "",
        "BlockDeviceMappings": [{"Ebs": {"VolumeId": v}} for v in volume_ids],
    }
    return resource, raw


def _volume_pair(volume_id, size_gb, volume_type="gp3"):
    now = datetime.now(UTC)
    resource = Resource(
        id=f"res-{volume_id}", resource_id=volume_id,
        resource_type=ResourceType.EBS_VOLUME, resource_arn="arn",
        region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now,
    )
    return resource, {"VolumeId": volume_id, "Size": size_gb,
                      "VolumeType": volume_type, "State": "in-use"}


def test_stopped_instance_priced_from_its_real_attached_volumes():
    """Replaces the old flat $5.00 placeholder."""
    scanner = StoppedEC2Scanner(
        region="us-east-1", pricing=StaticPricing(), threshold_days=0
    )

    findings = scanner.evaluate([
        _stopped_pair("i-1", ["vol-root", "vol-data"]),
        _volume_pair("vol-root", 100, "gp3"),   # 100 * 0.08 = 8.00
        _volume_pair("vol-data", 50, "gp2"),    #  50 * 0.10 = 5.00
    ])

    assert len(findings) == 1
    assert findings[0].est_monthly_cost_usd == Decimal("13.00")
    assert findings[0].evidence["cost_basis"] == "attached EBS volumes"


def test_stopped_instance_falls_back_when_volumes_are_not_in_inventory():
    """No volume data must not mean $0 — that reads as 'free to keep'."""
    scanner = StoppedEC2Scanner(
        region="us-east-1", pricing=StaticPricing(), threshold_days=0
    )

    findings = scanner.evaluate([_stopped_pair("i-2", ["vol-missing"])])

    assert len(findings) == 1
    # Assumed 8 GB gp3 root volume: 8 * 0.08 = 0.64
    assert findings[0].est_monthly_cost_usd == Decimal("0.64")
    assert findings[0].evidence["cost_basis"] == "assumed root volume"
