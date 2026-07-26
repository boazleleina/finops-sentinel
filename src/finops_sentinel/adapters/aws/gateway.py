import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from finops_sentinel.ports.cloud import CloudGateway

logger = logging.getLogger(__name__)


class Boto3Gateway(CloudGateway):
    """
    AWS Adapter implementing the CloudGateway port using boto3.
    Honors AWS_ENDPOINT_URL so LocalStack and real AWS are interchangeable.
    """

    def __init__(
        self,
        region: str,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        self.client = boto3.client(
            "ec2",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self.cloudwatch = boto3.client(
            "cloudwatch",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def describe_ebs_volumes(self) -> list[dict[str, Any]]:
        volumes: list[dict[str, Any]] = []
        paginator = self.client.get_paginator("describe_volumes")
        page_iterator = paginator.paginate()
        for page in page_iterator:
            volumes.extend(page.get("Volumes", []))
        return volumes

    def describe_elastic_ips(self) -> list[dict[str, Any]]:
        response = self.client.describe_addresses()
        addresses: list[dict[str, Any]] = response.get("Addresses", [])
        return addresses

    def describe_ec2_instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        paginator = self.client.get_paginator("describe_instances")
        page_iterator = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
        )
        for page in page_iterator:
            for reservation in page.get("Reservations", []):
                instances.extend(reservation.get("Instances", []))
        return instances

    def describe_ebs_snapshots(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        paginator = self.client.get_paginator("describe_snapshots")
        page_iterator = paginator.paginate(OwnerIds=['self'])
        for page in page_iterator:
            snapshots.extend(page.get("Snapshots", []))
        return snapshots

    def describe_running_ec2_instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        paginator = self.client.get_paginator("describe_instances")
        page_iterator = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        for page in page_iterator:
            for reservation in page.get("Reservations", []):
                instances.extend(reservation.get("Instances", []))
        return instances

    def get_instance_metric_averages(
        self, instance_id: str, metric_name: str, days: int, period_seconds: int = 3600
    ) -> list[float]:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        try:
            response = self.cloudwatch.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName=metric_name,
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start,
                EndTime=end,
                Period=period_seconds,
                Statistics=["Average"],
            )
        except ClientError as exc:
            # A metrics outage must not fail the whole scan; an empty series
            # reads as "unknown" downstream, which suppresses the finding.
            logger.warning(
                "CloudWatch %s lookup failed for %s: %s", metric_name, instance_id, exc
            )
            return []

        datapoints = sorted(
            response.get("Datapoints", []), key=lambda point: point["Timestamp"]
        )
        return [float(point["Average"]) for point in datapoints]

    def execute(self, playbook: str, resource_id: str, dry_run: bool) -> dict[str, Any]:
        playbooks = {
            "release_eip": self._release_eip,
            "terminate_stopped_instance": self._terminate_stopped_instance,
            "snapshot_then_delete_volume": self._snapshot_then_delete_volume,
            "delete_ebs_snapshot": self._delete_ebs_snapshot,
        }
        impl = playbooks.get(playbook)
        if impl is None:
            raise ValueError(f"Unknown playbook: {playbook}")

        if dry_run:
            logger.info("[DRY RUN] Would execute playbook %s on %s", playbook, resource_id)
            return {"dry_run": True}

        return impl(resource_id)

    def _release_eip(self, allocation_id: str) -> dict[str, Any]:
        logger.info("Releasing Elastic IP: %s", allocation_id)
        self.client.release_address(AllocationId=allocation_id)
        return {"released": allocation_id}

    def _terminate_stopped_instance(self, instance_id: str) -> dict[str, Any]:
        logger.info("Terminating EC2 instance: %s", instance_id)
        self.client.terminate_instances(InstanceIds=[instance_id])
        return {"terminated": instance_id}

    def _snapshot_then_delete_volume(self, volume_id: str) -> dict[str, Any]:
        logger.info("Creating snapshot for EBS volume: %s", volume_id)
        response = self.client.create_snapshot(
            VolumeId=volume_id,
            Description="Snapshot created by FinOps Sentinel before deletion",
        )
        snapshot_id = response["SnapshotId"]

        waiter = self.client.get_waiter("snapshot_completed")
        logger.info("Waiting for snapshot %s to complete...", snapshot_id)
        waiter.wait(SnapshotIds=[snapshot_id], WaiterConfig={"Delay": 15, "MaxAttempts": 40})

        logger.info("Snapshot %s complete. Deleting volume: %s", snapshot_id, volume_id)
        self.client.delete_volume(VolumeId=volume_id)
        return {"snapshot_id": snapshot_id, "deleted_volume": volume_id}

    def _delete_ebs_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        logger.info("Deleting EBS snapshot: %s", snapshot_id)
        self.client.delete_snapshot(SnapshotId=snapshot_id)
        return {"deleted_snapshot": snapshot_id}
