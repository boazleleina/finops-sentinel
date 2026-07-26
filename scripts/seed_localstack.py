#!/usr/bin/env python3
"""Script to seed LocalStack with mock AWS resources for FinOps Sentinel testing."""

import os
import sys
import time
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

# Set dummy AWS credentials if not present
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")

print(f"Connecting to AWS at {ENDPOINT_URL}")

try:
    ec2 = boto3.client("ec2", endpoint_url=ENDPOINT_URL)
except Exception as e:
    print(f"Failed to initialize boto3 client: {e}")
    sys.exit(1)

def seed():
    print("Seeding LocalStack resources...")
    
    # 1. Create 2 unattached EBS volumes (one protected)
    print("Creating unattached EBS volumes...")
    vol1 = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=10,
        VolumeType="gp3"
    )
    print(f"  Created volume: {vol1['VolumeId']}")
    
    vol2 = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=20,
        VolumeType="gp3",
        TagSpecifications=[
            {
                'ResourceType': 'volume',
                'Tags': [{'Key': 'finops:protected', 'Value': 'true'}]
            }
        ]
    )
    print(f"  Created protected volume: {vol2['VolumeId']}")
    
    # 2. Create 1 orphaned Elastic IP
    print("Creating orphaned Elastic IP...")
    eip = ec2.allocate_address(Domain="vpc")
    print(f"  Created EIP: {eip['PublicIp']} ({eip['AllocationId']})")
    
    # 3. Create 1 stopped EC2 instance
    print("Creating stopped EC2 instance...")
    # Need to run an instance and then stop it
    reservation = ec2.run_instances(
        ImageId="ami-0c55b159cbfafe1f0", # Dummy AMI
        InstanceType="t3.micro",
        MinCount=1,
        MaxCount=1
    )
    instance_id = reservation['Instances'][0]['InstanceId']
    print(f"  Created instance: {instance_id}")
    
    # Wait a moment for instance to be created before stopping
    time.sleep(2)
    ec2.stop_instances(InstanceIds=[instance_id])
    print(f"  Stopped instance: {instance_id}")
    
    # 4. Create 2 orphaned EBS snapshots (from a third volume that gets deleted)
    print("Creating orphaned EBS snapshots...")
    vol3 = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=5,
        VolumeType="gp3"
    )
    print(f"  Created temporary volume: {vol3['VolumeId']}")
    
    time.sleep(2)
    
    snap1 = ec2.create_snapshot(
        VolumeId=vol3['VolumeId'],
        Description="Orphaned snapshot 1"
    )
    print(f"  Created snapshot: {snap1['SnapshotId']}")
    
    snap2 = ec2.create_snapshot(
        VolumeId=vol3['VolumeId'],
        Description="Orphaned snapshot 2"
    )
    print(f"  Created snapshot: {snap2['SnapshotId']}")

    time.sleep(2)
    ec2.delete_volume(VolumeId=vol3['VolumeId'])
    print(f"  Deleted temporary volume: {vol3['VolumeId']}")

    # 5. Create a RUNNING but idle EC2 instance, plus the flat CloudWatch
    # series the ec2_idle rule needs. LocalStack has no real instance metrics,
    # so they are published explicitly.
    print("Creating idle running EC2 instance...")
    idle_reservation = ec2.run_instances(
        ImageId="ami-0c55b159cbfafe1f0",
        InstanceType="m5.large",
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                'ResourceType': 'instance',
                'Tags': [{'Key': 'Name', 'Value': 'idle-demo-worker'}]
            }
        ]
    )
    idle_id = idle_reservation['Instances'][0]['InstanceId']
    print(f"  Created running instance: {idle_id}")

    publish_idle_metrics(idle_id)

    print("Seeding complete!")


def publish_idle_metrics(instance_id, days=14, interval_hours=6):
    """Publish near-zero CPU/network so ec2_idle has enough datapoints.

    The scanner refuses to judge a series shorter than its min_datapoints
    (default 24), so this must span the whole observation window. Points are
    spaced every interval_hours rather than hourly: LocalStack's query-protocol
    parser rejects large PutMetricData bodies, and 56 points per metric already
    clears the floor.
    """
    cloudwatch = boto3.client("cloudwatch", endpoint_url=ENDPOINT_URL)
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

    print(f"  Published {published} idle datapoints over {days} days")

if __name__ == "__main__":
    try:
        seed()
    except ClientError as e:
        print(f"AWS API Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
