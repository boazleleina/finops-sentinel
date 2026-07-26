from abc import ABC, abstractmethod
from typing import Any


class CloudGateway(ABC):
    """
    Port for interacting with cloud provider APIs.
    The domain only knows about these abstract operations.
    """

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
    def get_instance_metric_averages(
        self, instance_id: str, metric_name: str, days: int, period_seconds: int = 3600
    ) -> list[float]:
        """
        Per-period averages for one instance metric (e.g. "CPUUtilization",
        "NetworkIn") over the trailing `days`, oldest first.

        Returns an empty list when the provider has no data. Callers must
        treat a short series as "unknown", not as "idle" — see
        ec2_idle_min_datapoints.
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
