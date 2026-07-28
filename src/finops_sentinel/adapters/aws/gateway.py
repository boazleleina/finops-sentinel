import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from finops_sentinel.ports.cloud import CloudGateway

logger = logging.getLogger(__name__)


def list_enabled_regions(
    region: str,
    endpoint_url: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
) -> list[str]:
    """Every region this account can actually call, newest opt-ins included.

    Opt-in regions (ap-east-1, me-south-1, ...) that the account never enabled
    are excluded: every API call against one fails with AuthFailure, which
    would turn a full-account scan into a wall of failed regions.

    Needs the ec2:DescribeRegions permission. `region` is only the endpoint the
    question is asked through — the answer is account-wide.
    """
    client = boto3.client(
        "ec2",
        region_name=region,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    response = client.describe_regions(AllRegions=False)
    return sorted(
        entry["RegionName"]
        for entry in response.get("Regions", [])
        if entry.get("OptInStatus") != "not-opted-in"
    )


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
        mpu_age_days: int = 7,
    ):
        # Only used by the abort playbook, which re-checks upload age at
        # execution time rather than trusting the age recorded at detection.
        self.mpu_age_days = mpu_age_days
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
        self.rds = boto3.client(
            "rds",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self.s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self.region = region

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

    def describe_rds_instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        paginator = self.rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            instances.extend(page.get("DBInstances", []))
        return instances

    def _bucket_region(self, bucket: str) -> str:
        """us-east-1 reports itself as None — a documented S3 API quirk."""
        location = self.s3.get_bucket_location(Bucket=bucket)
        constraint: str | None = location.get("LocationConstraint")
        return constraint or "us-east-1"

    def _bucket_detail(self, bucket: str) -> dict[str, Any]:
        """Lifecycle, versioning, and tags for one bucket.

        Every lookup here 404s on a bucket that simply has none configured —
        that is S3's normal way of saying "not set", not an error, so each is
        caught individually rather than failing the bucket.
        """
        try:
            lifecycle = self.s3.get_bucket_lifecycle_configuration(Bucket=bucket)
            rules: list[dict[str, Any]] = lifecycle.get("Rules", [])
        except ClientError:
            rules = []

        try:
            versioning = self.s3.get_bucket_versioning(Bucket=bucket).get("Status", "")
        except ClientError:
            versioning = ""

        try:
            tag_set = self.s3.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
        except ClientError:
            tag_set = []

        return {"LifecycleRules": rules, "Versioning": versioning, "Tags": tag_set}

    def describe_s3_buckets(self) -> list[dict[str, Any]]:
        buckets: list[dict[str, Any]] = []
        for bucket in self.s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            try:
                if self._bucket_region(name) != self.region:
                    continue
                detail = self._bucket_detail(name)
            except ClientError as exc:
                # A bucket this credential cannot read is not a reason to lose
                # every other bucket's finding.
                logger.warning("Skipping S3 bucket %s: %s", name, exc)
                continue

            buckets.append(
                {"Name": name, "CreationDate": bucket.get("CreationDate"), **detail}
            )
        return buckets

    def get_incomplete_multipart_uploads(self, bucket: str) -> list[dict[str, Any]]:
        uploads: list[dict[str, Any]] = []
        try:
            paginator = self.s3.get_paginator("list_multipart_uploads")
            for page in paginator.paginate(Bucket=bucket):
                for upload in page.get("Uploads", []):
                    uploads.append(
                        {
                            "Key": upload["Key"],
                            "UploadId": upload["UploadId"],
                            "Initiated": upload["Initiated"],
                            "SizeBytes": self._upload_size(
                                bucket, upload["Key"], upload["UploadId"]
                            ),
                        }
                    )
        except ClientError as exc:
            logger.warning("Multipart upload listing failed for %s: %s", bucket, exc)
            return []
        return uploads

    def _upload_size(self, bucket: str, key: str, upload_id: str) -> int:
        """Sum the parts already uploaded. This is what is actually billing."""
        total = 0
        try:
            paginator = self.s3.get_paginator("list_parts")
            for page in paginator.paginate(Bucket=bucket, Key=key, UploadId=upload_id):
                total += sum(int(part.get("Size", 0)) for part in page.get("Parts", []))
        except ClientError as exc:
            logger.warning("Part listing failed for %s/%s: %s", bucket, key, exc)
        return total

    def get_metric_averages(
        self,
        namespace: str,
        dimensions: dict[str, str],
        metric_name: str,
        days: int,
        period_seconds: int = 3600,
    ) -> list[float]:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        try:
            response = self.cloudwatch.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
                StartTime=start,
                EndTime=end,
                Period=period_seconds,
                Statistics=["Average"],
            )
        except ClientError as exc:
            # A metrics outage must not fail the whole scan; an empty series
            # reads as "unknown" downstream, which suppresses the finding.
            logger.warning(
                "CloudWatch %s/%s lookup failed for %s: %s",
                namespace,
                metric_name,
                dimensions,
                exc,
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
            "abort_incomplete_multipart_uploads": self._abort_incomplete_multipart_uploads,
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

    def _abort_incomplete_multipart_uploads(self, bucket: str) -> dict[str, Any]:
        """Discard multipart uploads abandoned longer than the age threshold.

        This deletes no object: an incomplete upload never became one. It
        discards orphaned parts that bill at full storage rate and that nothing
        in the console will ever show you.

        The age is re-checked HERE, not just at detection. An approval can sit
        in Slack for hours, and between detection and the click a client may
        have started a legitimate large upload. Anything younger than the
        threshold at execution time is left alone and reported as skipped.
        """
        cutoff = datetime.now(UTC) - timedelta(days=self.mpu_age_days)
        aborted, reclaimed, skipped = 0, 0, 0

        for upload in self.get_incomplete_multipart_uploads(bucket):
            initiated = upload["Initiated"]
            if initiated.tzinfo is None:
                initiated = initiated.replace(tzinfo=UTC)
            if initiated > cutoff:
                skipped += 1
                continue

            logger.info(
                "Aborting multipart upload %s of %s/%s",
                upload["UploadId"],
                bucket,
                upload["Key"],
            )
            self.s3.abort_multipart_upload(
                Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
            aborted += 1
            reclaimed += int(upload.get("SizeBytes", 0))

        return {
            "bucket": bucket,
            "aborted": aborted,
            "bytes_reclaimed": reclaimed,
            "skipped_too_recent": skipped,
        }
