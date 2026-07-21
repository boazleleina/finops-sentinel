import boto3
import time
import logging
from typing import List, Dict, Any, Optional
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.config import settings

logger = logging.getLogger(__name__)

class Boto3Gateway(CloudGateway):
    """
    AWS Adapter implementing the CloudGateway port using boto3.
    """
    def __init__(self, region: str, endpoint_url: Optional[str] = None, aws_access_key_id: Optional[str] = None, aws_secret_access_key: Optional[str] = None):
        self.client = boto3.client(
            'ec2', 
            region_name=region, 
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key
        )

    def describe_ebs_volumes(self) -> List[Dict[str, Any]]:
        volumes = []
        paginator = self.client.get_paginator('describe_volumes')
        page_iterator = paginator.paginate(Filters=[{'Name': 'status', 'Values': ['available']}])
        for page in page_iterator:
            volumes.extend(page.get('Volumes', []))
        return volumes

    def describe_elastic_ips(self) -> List[Dict[str, Any]]:
        response = self.client.describe_addresses()
        return response.get('Addresses', [])

    def describe_ec2_instances(self) -> List[Dict[str, Any]]:
        instances = []
        paginator = self.client.get_paginator('describe_instances')
        page_iterator = paginator.paginate(Filters=[{'Name': 'instance-state-name', 'Values': ['stopped']}])
        for page in page_iterator:
            for reservation in page.get('Reservations', []):
                instances.extend(reservation.get('Instances', []))
        return instances

    def release_elastic_ip(self, allocation_id: str) -> None:
        if settings.dry_run:
            logger.info(f"[DRY RUN] Would release Elastic IP: {allocation_id}")
            return
            
        logger.info(f"Releasing Elastic IP: {allocation_id}")
        self.client.release_address(AllocationId=allocation_id)

    def terminate_instance(self, instance_id: str) -> None:
        if settings.dry_run:
            logger.info(f"[DRY RUN] Would terminate EC2 instance: {instance_id}")
            return
            
        logger.info(f"Terminating EC2 instance: {instance_id}")
        self.client.terminate_instances(InstanceIds=[instance_id])

    def snapshot_and_delete_volume(self, volume_id: str) -> None:
        if settings.dry_run:
            logger.info(f"[DRY RUN] Would snapshot and delete EBS volume: {volume_id}")
            return
            
        logger.info(f"Creating snapshot for EBS volume: {volume_id}")
        response = self.client.create_snapshot(
            VolumeId=volume_id,
            Description=f"Snapshot created by FinOps Sentinel before deletion"
        )
        snapshot_id = response['SnapshotId']
        
        # Poll until snapshot is complete
        waiter = self.client.get_waiter('snapshot_completed')
        logger.info(f"Waiting for snapshot {snapshot_id} to complete...")
        waiter.wait(
            SnapshotIds=[snapshot_id],
            WaiterConfig={'Delay': 15, 'MaxAttempts': 40}
        )
        
        logger.info(f"Snapshot {snapshot_id} complete. Deleting volume: {volume_id}")
        self.client.delete_volume(VolumeId=volume_id)
