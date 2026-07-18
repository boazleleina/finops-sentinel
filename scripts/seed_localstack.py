#!/usr/bin/env python3
"""Script to seed LocalStack with mock AWS resources for FinOps Sentinel testing."""

import os
import sys
import time
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
    
    # 4. Create 2 old EBS snapshots
    print("Creating EBS snapshots...")
    snap1 = ec2.create_snapshot(
        VolumeId=vol1['VolumeId'],
        Description="Old snapshot 1"
    )
    print(f"  Created snapshot: {snap1['SnapshotId']}")
    
    snap2 = ec2.create_snapshot(
        VolumeId=vol1['VolumeId'],
        Description="Old snapshot 2"
    )
    print(f"  Created snapshot: {snap2['SnapshotId']}")

    print("Seeding complete!")

if __name__ == "__main__":
    try:
        seed()
    except ClientError as e:
        print(f"AWS API Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
