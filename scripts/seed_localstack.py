#!/usr/bin/env python3
"""Seed LocalStack with mock AWS resources for FinOps Sentinel testing.

Seeds every region --regions names, defaulting to whatever the scan itself
will cover, so a multi-region scan has something to find in each. Resource
sizes differ per region on purpose: identical findings everywhere would hide
whether the region actually survived the round trip into the alert.

Regions and endpoint come from finops_sentinel.config, the same settings the
scan reads. Parsing the environment separately here is what made
`python scripts/seed_localstack.py` seed one region while `sentinel scan`
looked in three: this script never loaded .env, the app always did.
"""

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

from finops_sentinel.config import settings

# Set dummy AWS credentials if not present
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", settings.aws_region)

ENDPOINT_URL = settings.aws_endpoint_url or "http://localhost:4566"

# Sizes and instance types per region, so the Slack alerts are visibly
# different and the per-region savings table has something to rank.
REGION_PROFILE = {
    "default": {"volume_gb": 10, "protected_gb": 20, "snapshot_gb": 5, "idle_type": "m5.large"},
    "us-east-1": {"volume_gb": 10, "protected_gb": 20, "snapshot_gb": 5, "idle_type": "m5.large"},
    "eu-west-1": {"volume_gb": 200, "protected_gb": 40, "snapshot_gb": 60, "idle_type": "m5.2xlarge"},
    "ap-southeast-2": {"volume_gb": 75, "protected_gb": 15, "snapshot_gb": 25, "idle_type": "c5.xlarge"},
}


def resolve_regions(cli_regions: str | None) -> tuple[list[str], str]:
    """Regions to seed, plus where they came from (printed, so a one-region
    seed against a three-region scan is obvious rather than mysterious)."""
    if cli_regions:
        ordered = dict.fromkeys(r.strip() for r in cli_regions.split(",") if r.strip())
        return list(ordered), "--regions"

    if settings.scans_all_regions:
        raise SystemExit(
            "AWS_REGIONS=all cannot be seeded — LocalStack has no notion of "
            "enabled regions. Name them explicitly: "
            "--regions us-east-1,eu-west-1,ap-southeast-2"
        )

    source = "AWS_REGIONS" if settings.aws_regions.strip() else "AWS_REGION"
    return settings.configured_regions, source


def seed_region(region: str) -> None:
    profile = REGION_PROFILE.get(region, REGION_PROFILE["default"])
    availability_zone = f"{region}a"
    ec2 = boto3.client("ec2", region_name=region, endpoint_url=ENDPOINT_URL)

    print(f"\n=== Seeding {region} ===")

    # 1. Two unattached EBS volumes (one protected)
    print("Creating unattached EBS volumes...")
    vol1 = ec2.create_volume(
        AvailabilityZone=availability_zone,
        Size=profile["volume_gb"],
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [{"Key": "Name", "Value": f"orphan-data-{region}"}],
            }
        ],
    )
    print(f"  Created volume: {vol1['VolumeId']} ({profile['volume_gb']} GB)")

    vol2 = ec2.create_volume(
        AvailabilityZone=availability_zone,
        Size=profile["protected_gb"],
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "finops:protected", "Value": "true"},
                    {"Key": "Name", "Value": f"protected-data-{region}"},
                ],
            }
        ],
    )
    print(f"  Created protected volume: {vol2['VolumeId']}")

    # 2. One orphaned Elastic IP
    print("Creating orphaned Elastic IP...")
    eip = ec2.allocate_address(Domain="vpc")
    print(f"  Created EIP: {eip['PublicIp']} ({eip['AllocationId']})")

    # 3. One stopped EC2 instance
    print("Creating stopped EC2 instance...")
    reservation = ec2.run_instances(
        ImageId="ami-0c55b159cbfafe1f0",  # Dummy AMI
        InstanceType="t3.micro",
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": f"abandoned-box-{region}"}],
            }
        ],
    )
    instance_id = reservation["Instances"][0]["InstanceId"]
    print(f"  Created instance: {instance_id}")

    # Wait a moment for instance to be created before stopping
    time.sleep(2)
    ec2.stop_instances(InstanceIds=[instance_id])
    print(f"  Stopped instance: {instance_id}")

    # 4. Two orphaned EBS snapshots (from a third volume that gets deleted)
    print("Creating orphaned EBS snapshots...")
    vol3 = ec2.create_volume(
        AvailabilityZone=availability_zone,
        Size=profile["snapshot_gb"],
        VolumeType="gp3",
    )
    print(f"  Created temporary volume: {vol3['VolumeId']}")

    time.sleep(2)

    snap1 = ec2.create_snapshot(
        VolumeId=vol3["VolumeId"], Description=f"Orphaned snapshot 1 ({region})"
    )
    print(f"  Created snapshot: {snap1['SnapshotId']}")

    snap2 = ec2.create_snapshot(
        VolumeId=vol3["VolumeId"], Description=f"Orphaned snapshot 2 ({region})"
    )
    print(f"  Created snapshot: {snap2['SnapshotId']}")

    time.sleep(2)
    ec2.delete_volume(VolumeId=vol3["VolumeId"])
    print(f"  Deleted temporary volume: {vol3['VolumeId']}")

    # 5. A RUNNING but idle EC2 instance, plus the flat CloudWatch series the
    # ec2_idle rule needs. LocalStack has no real instance metrics, so they are
    # published explicitly.
    print("Creating idle running EC2 instance...")
    idle_reservation = ec2.run_instances(
        ImageId="ami-0c55b159cbfafe1f0",
        InstanceType=profile["idle_type"],
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": f"idle-demo-worker-{region}"}],
            }
        ],
    )
    idle_id = idle_reservation["Instances"][0]["InstanceId"]
    print(f"  Created running instance: {idle_id} ({profile['idle_type']})")

    publish_idle_metrics(idle_id, region)


def publish_idle_metrics(instance_id, region, days=14, interval_hours=6):
    """Publish near-zero CPU/network so ec2_idle has enough datapoints.

    The scanner refuses to judge a series shorter than its min_datapoints
    (default 24), so this must span the whole observation window. Points are
    spaced every interval_hours rather than hourly: LocalStack's query-protocol
    parser rejects large PutMetricData bodies, and 56 points per metric already
    clears the floor.

    Metrics are region-scoped in CloudWatch exactly as they are in AWS, so this
    must target the same region the instance lives in.
    """
    cloudwatch = boto3.client("cloudwatch", region_name=region, endpoint_url=ENDPOINT_URL)
    now = datetime.now(UTC)
    metrics = {"CPUUtilization": (0.4, "Percent"),
               "NetworkIn": (900.0, "Bytes"),
               "NetworkOut": (700.0, "Bytes")}

    published = 0
    for metric_name, (value, unit) in metrics.items():
        batch = []
        for step in range((days * 24) // interval_hours):
            batch.append({
                "MetricName": metric_name,
                "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                "Timestamp": now - timedelta(hours=step * interval_hours),
                "Value": value,
                "Unit": unit,
            })
            if len(batch) == 20:
                cloudwatch.put_metric_data(Namespace="AWS/EC2", MetricData=batch)
                published += len(batch)
                batch = []
        if batch:
            cloudwatch.put_metric_data(Namespace="AWS/EC2", MetricData=batch)
            published += len(batch)

    print(f"  Published {published} idle datapoints over {days} days in {region}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regions",
        help="Comma-separated regions to seed. Defaults to the same regions "
             "`sentinel scan` will cover (AWS_REGIONS, else AWS_REGION).",
    )
    args = parser.parse_args()

    regions, source = resolve_regions(args.regions)
    print(f"Connecting to AWS at {ENDPOINT_URL}")
    print(f"Seeding {len(regions)} region(s) from {source}: {', '.join(regions)}")

    for region in regions:
        seed_region(region)

    print(f"\nSeeding complete across {len(regions)} region(s)!")


if __name__ == "__main__":
    try:
        main()
    except ClientError as e:
        print(f"AWS API Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
