from abc import ABC, abstractmethod
from typing import Any


class CloudGateway(ABC):
    """
    Port for interacting with cloud provider APIs.
    The domain only knows about these abstract operations.
    """

    @property
    @abstractmethod
    def account_id(self) -> str:
        """The account these credentials belong to.

        Needed to build resource ARNs that are actually addressable. An ARN
        with a made-up account segment reads fine in a Slack message and is
        worthless everywhere else — IAM policies, cross-account links, support
        tickets — so the account is asked for once, from the credentials in
        hand, rather than guessed.
        """
        ...  # pragma: no cover

    @abstractmethod
    def describe_ebs_volumes(self) -> list[dict[str, Any]]:
        ...  # pragma: no cover

    @abstractmethod
    def describe_elastic_ips(self) -> list[dict[str, Any]]:
        ...  # pragma: no cover

    @abstractmethod
    def describe_ec2_instances(self) -> list[dict[str, Any]]:
        ...  # pragma: no cover

    @abstractmethod
    def describe_ebs_snapshots(self) -> list[dict[str, Any]]:
        ...  # pragma: no cover

    @abstractmethod
    def describe_running_ec2_instances(self) -> list[dict[str, Any]]:
        """Instances in the running state — the candidates for idleness checks.

        Separate from describe_ec2_instances(), which returns only STOPPED
        instances for the abandoned-instance rule.
        """
        ...  # pragma: no cover

    @abstractmethod
    def describe_rds_instances(self) -> list[dict[str, Any]]:
        """Every RDS instance in the region, whatever its state.

        Unlike EC2 there is no state-filtered sibling: DescribeDBInstances has
        no server-side status filter, so callers filter on DBInstanceStatus
        themselves. Returning every state also keeps the inventory complete —
        an instance mid-backup must not vanish from the inventory and get swept
        up as DELETED.
        """
        ...  # pragma: no cover

    @abstractmethod
    def describe_s3_buckets(self) -> list[dict[str, Any]]:
        """Buckets homed in this gateway's region, with their configuration.

        Bucket names are global but each bucket lives in one region, so the
        adapter filters to its own — otherwise a multi-region scan would report
        every bucket once per region.

        Each entry carries the bucket's Name, CreationDate, Tags, LifecycleRules
        (empty list when none is configured), and Versioning status, so a
        scanner needs one call rather than four per bucket.
        """
        ...  # pragma: no cover

    @abstractmethod
    def get_incomplete_multipart_uploads(self, bucket: str) -> list[dict[str, Any]]:
        """In-progress multipart uploads, with the byte size of their parts.

        Orphaned uploads are invisible in the console and bill at full storage
        rate forever: no object exists, so nothing lists them, but the parts
        occupy space. Each entry carries Key, UploadId, Initiated, and SizeBytes.
        """
        ...  # pragma: no cover

    @abstractmethod
    def get_metric_averages(
        self,
        namespace: str,
        dimensions: dict[str, str],
        metric_name: str,
        days: int,
        period_seconds: int = 3600,
    ) -> list[float]:
        """
        Per-period averages for one CloudWatch metric over the trailing `days`,
        oldest first.

        Namespace and dimensions are caller-supplied so a single method serves
        every service rather than growing one near-identical method per
        resource kind:

            AWS/EC2  {"InstanceId": "i-0abc..."}              CPUUtilization
            AWS/RDS  {"DBInstanceIdentifier": "prod-db"}      DatabaseConnections
            AWS/S3   {"BucketName": ..., "StorageType": ...}  BucketSizeBytes

        `dimensions` is a map rather than a single name/value pair because
        CloudWatch matches dimension sets EXACTLY: a metric published against
        two dimensions is invisible to a query naming only one. S3 bucket size
        is the case in point — querying it by BucketName alone returns nothing
        at all against real AWS.

        Returns an empty list when the provider has no data. Callers must treat
        a short series as "unknown" rather than as a verdict — see the
        *_min_datapoints settings.
        """
        ...  # pragma: no cover

    @abstractmethod
    def execute(self, playbook: str, resource_id: str, dry_run: bool) -> dict[str, Any]:
        """
        Execute a named remediation playbook against a resource.

        The playbook name must come from the domain's PLAYBOOK_ALLOWLIST —
        adapters raise ValueError for unknown playbooks. When dry_run is True
        the adapter must only log what it would do and return
        {"dry_run": True}. Returns a result detail dict (e.g. the snapshot_id
        created before a volume deletion).
        """
        ...  # pragma: no cover
