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
    "default": {"volume_gb": 10, "protected_gb": 20, "snapshot_gb": 5, "idle_type": "m5.large", "bucket_gb": 800},
    "us-east-1": {"volume_gb": 10, "protected_gb": 20, "snapshot_gb": 5, "idle_type": "m5.large", "bucket_gb": 800},
    "eu-west-1": {"volume_gb": 200, "protected_gb": 40, "snapshot_gb": 60, "idle_type": "m5.2xlarge", "bucket_gb": 4000},
    "ap-southeast-2": {"volume_gb": 75, "protected_gb": 15, "snapshot_gb": 25, "idle_type": "c5.xlarge", "bucket_gb": 150},
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

    seed_buckets(region, profile)


def seed_buckets(region, profile):
    """Four buckets covering every branch of the S3 rules.

    LocalStack does not publish the AWS/S3 BucketSizeBytes metric, and the
    scanner reads size from CloudWatch only — deliberately, since the
    alternative is an unbounded ListObjectsV2 walk against real buckets. So the
    sizes are published here as synthetic datapoints, exactly as
    publish_idle_metrics already does for AWS/EC2.
    """
    s3 = boto3.client("s3", region_name=region, endpoint_url=ENDPOINT_URL)
    suffix = region.replace("_", "-")
    size_gb = profile["bucket_gb"]

    print("Creating S3 buckets...")
    create_args = {} if region == "us-east-1" else {
        "CreateBucketConfiguration": {"LocationConstraint": region}
    }

    # 1. Large, no lifecycle policy, versioning on — the full s3_no_lifecycle case.
    unmanaged = f"finops-demo-unmanaged-{suffix}"
    s3.create_bucket(Bucket=unmanaged, **create_args)
    s3.put_bucket_versioning(
        Bucket=unmanaged, VersioningConfiguration={"Status": "Enabled"}
    )
    publish_bucket_size(unmanaged, region, size_gb)
    print(f"  Created unmanaged bucket: {unmanaged} ({size_gb} GB, versioned)")

    # 2. Same size, but has a policy — must NOT be flagged.
    managed = f"finops-demo-managed-{suffix}"
    s3.create_bucket(Bucket=managed, **create_args)
    s3.put_bucket_lifecycle_configuration(
        Bucket=managed,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-old-versions",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ]
        },
    )
    publish_bucket_size(managed, region, size_gb)
    print(f"  Created managed bucket: {managed} (has lifecycle policy)")

    # 3. Protected by tag — the domain must exclude it despite being unmanaged.
    protected = f"finops-demo-protected-{suffix}"
    s3.create_bucket(Bucket=protected, **create_args)
    s3.put_bucket_tagging(
        Bucket=protected,
        Tagging={"TagSet": [{"Key": "finops:protected", "Value": "true"}]},
    )
    publish_bucket_size(protected, region, size_gb)
    print(f"  Created protected bucket: {protected}")

    # 4. An abandoned multipart upload — the one remediable S3 finding.
    uploads = f"finops-demo-uploads-{suffix}"
    s3.create_bucket(Bucket=uploads, **create_args)
    s3.put_bucket_lifecycle_configuration(
        Bucket=uploads,
        LifecycleConfiguration={
            "Rules": [
                {"ID": "keep-quiet", "Status": "Enabled", "Filter": {"Prefix": ""},
                 "Expiration": {"Days": 365}}
            ]
        },
    )
    upload_id = s3.create_multipart_upload(Bucket=uploads, Key="abandoned-backup.tar")[
        "UploadId"
    ]
    s3.upload_part(
        Bucket=uploads, Key="abandoned-backup.tar", UploadId=upload_id,
        PartNumber=1, Body=b"x" * (5 * 1024 * 1024),
    )
    print(f"  Created abandoned multipart upload in {uploads} (5 MB part)")
    # Initiated is stamped server-side, so a freshly seeded upload is minutes
    # old and the default 7-day threshold correctly ignores it. Say so, or the
    # one remediable S3 finding looks broken rather than newly created.
    print(
        "    (age is 0d — run with S3_INCOMPLETE_MPU_AGE_DAYS=0 to see the "
        "s3_incomplete_multipart finding)"
    )


def publish_bucket_size(bucket, region, size_gb, days=3):
    """Synthetic BucketSizeBytes, the daily metric AWS publishes and LocalStack does not."""
    cloudwatch = boto3.client("cloudwatch", region_name=region, endpoint_url=ENDPOINT_URL)
    now = datetime.now(UTC)
    cloudwatch.put_metric_data(
        Namespace="AWS/S3",
        MetricData=[
            {
                "MetricName": "BucketSizeBytes",
                "Dimensions": [
                    {"Name": "BucketName", "Value": bucket},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                # Backdated: CloudWatch buckets by period and the one covering
                # "now" is still open, so a point stamped now reads back empty.
                "Timestamp": now - timedelta(days=day),
                "Value": float(size_gb) * 1024**3,
                "Unit": "Bytes",
            }
            for day in range(1, days + 1)
        ],
    )


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
