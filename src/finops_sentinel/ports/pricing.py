from abc import ABC, abstractmethod
from decimal import Decimal


class Pricing(ABC):
    """
    Port for turning a resource's shape into an estimated monthly cost.

    Every method answers the same question: "what would this resource cost per
    month at the given region's rate?" Scanners subtract nothing and add
    nothing — the number a finding reports is the spend that STOPS if the
    resource goes away.

    Two deliberate constraints on implementations:

    - Never raise. An unknown instance type or a pricing API outage must fall
      back to a documented estimate, because a finding with an approximate
      cost is useful and a scan that dies is not.
    - Never return zero as an "unknown" marker. Zero reads as "free" in the
      savings total and would quietly hide waste.

    `region` is taken by every method so a live pricing adapter can be dropped
    in without touching a single scanner signature, even though the static
    implementation currently prices everything at one region's rates.
    """

    @abstractmethod
    def ebs_volume_monthly(self, volume_type: str, size_gb: int, region: str) -> Decimal:
        """Monthly cost of a provisioned EBS volume."""
        ...  # pragma: no cover

    @abstractmethod
    def ebs_snapshot_monthly(self, size_gb: int, region: str) -> Decimal:
        """Monthly cost of snapshot storage.

        size_gb is the source volume size; real snapshots bill on incremental
        blocks, so this is an upper bound. See the static adapter's notes.
        """
        ...  # pragma: no cover

    @abstractmethod
    def elastic_ip_monthly(self, region: str) -> Decimal:
        """Monthly cost of an idle public IPv4 address."""
        ...  # pragma: no cover

    @abstractmethod
    def ec2_instance_monthly(self, instance_type: str, region: str) -> Decimal:
        """Monthly on-demand cost of a running instance."""
        ...  # pragma: no cover

    @abstractmethod
    def rds_instance_monthly(self, instance_class: str, engine: str, region: str) -> Decimal:
        """Monthly on-demand cost of a running RDS instance's compute.

        Compute only — storage bills separately and keeps billing while the
        instance is stopped, so the two are never summed by this port. Callers
        add rds_storage_monthly when the whole instance is the waste.
        """
        ...  # pragma: no cover

    @abstractmethod
    def rds_storage_monthly(self, size_gb: int, storage_type: str, region: str) -> Decimal:
        """Monthly cost of an RDS instance's allocated storage.

        This is what a *stopped* instance still costs: AWS bills allocated
        storage whether the engine is running or not.
        """
        ...  # pragma: no cover
