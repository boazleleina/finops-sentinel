from abc import ABC, abstractmethod
from decimal import Decimal

from finops_sentinel.domain.models import RightsizingCandidate


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
    def rightsizing_candidates(
        self, instance_type: str, region: str
    ) -> list[RightsizingCandidate]:
        """Cheaper instance types this one could be replaced by, biggest saving first.

        Strictly a price question. This port supplies *what is cheaper* and
        never *whether to recommend it* — the utilisation judgement lives in
        domain.rightsizing, so a price-table edit can never change what counts
        as over-provisioned.

        Returns an empty list when nothing cheaper is known: the smallest type
        in its family, or a type the implementation has no price for. Guessing
        a target from a name pattern would produce advice about hardware the
        implementation cannot price.
        """
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
    def s3_storage_monthly(self, size_gb: float, storage_class: str, region: str) -> Decimal:
        """Monthly cost of objects held in a bucket at the given storage class.

        Takes a float because bucket sizes come from CloudWatch in bytes and
        rarely land on whole gigabytes; rounding to int first would price a
        700MB bucket at zero, which the port forbids.
        """
        ...  # pragma: no cover

    @abstractmethod
    def rds_storage_monthly(self, size_gb: int, storage_type: str, region: str) -> Decimal:
        """Monthly cost of an RDS instance's allocated storage.

        This is what a *stopped* instance still costs: AWS bills allocated
        storage whether the engine is running or not.
        """
        ...  # pragma: no cover
