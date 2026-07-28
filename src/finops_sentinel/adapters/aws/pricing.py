"""The single source of truth for AWS prices.

Every rate the system uses lives in this file. Scanners take the Pricing port
and never hold prices of their own, so updating a rate is a one-line edit here
rather than a hunt through scanner modules.

All rates are **us-east-1 on-demand list prices**, and that carries three
caveats worth stating plainly, because they bound how much the reported
savings can be trusted:

1. Other regions cost more. The static table applies us-east-1 rates
   everywhere and logs a warning when asked for anything else.
2. List price is not your price. Reserved Instances, Savings Plans, and
   enterprise discounts all reduce what you actually pay — and if an instance
   is already covered by a commitment, deleting it saves nothing at all.
3. Snapshots bill on *incremental* changed blocks, not the full volume size,
   so snapshot estimates are an upper bound.

Replacing this with real numbers means writing a second Pricing adapter
against the AWS Price List API (list prices, free, needs caching) or Cost and
Usage Reports (actuals, needs S3 + Athena) and registering it in bootstrap.
No scanner changes.
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from finops_sentinel.ports.pricing import Pricing

logger = logging.getLogger(__name__)

PRICED_REGION = "us-east-1"
HOURS_PER_MONTH = Decimal(730)
CENTS = Decimal("0.01")

# EBS storage, $/GB-month. Source: https://aws.amazon.com/ebs/pricing/
EBS_GB_MONTH: dict[str, Decimal] = {
    "gp3": Decimal("0.08"),
    "gp2": Decimal("0.10"),
    "io1": Decimal("0.125"),
    "io2": Decimal("0.125"),
    "st1": Decimal("0.045"),
    "sc1": Decimal("0.015"),
    "standard": Decimal("0.05"),
}
# gp2 is the safe default: it is the priciest of the common general-purpose
# types, so an unknown type is never under-estimated into looking harmless.
DEFAULT_EBS_GB_MONTH = EBS_GB_MONTH["gp2"]

# Snapshot storage, $/GB-month. Source: https://aws.amazon.com/ebs/pricing/
SNAPSHOT_GB_MONTH = Decimal("0.05")

# Idle public IPv4, $/hour. Source: https://aws.amazon.com/vpc/pricing/
ELASTIC_IP_HOURLY = Decimal("0.005")

# EC2 on-demand Linux, $/hour.
# Source: https://aws.amazon.com/ec2/pricing/on-demand/
EC2_HOURLY: dict[str, Decimal] = {
    "t3.micro": Decimal("0.0104"),
    "t3.small": Decimal("0.0208"),
    "t3.medium": Decimal("0.0416"),
    "t3.large": Decimal("0.0832"),
    "t3.xlarge": Decimal("0.1664"),
    "m5.large": Decimal("0.096"),
    "m5.xlarge": Decimal("0.192"),
    "m5.2xlarge": Decimal("0.384"),
    "c5.large": Decimal("0.085"),
    "c5.xlarge": Decimal("0.17"),
    "r5.large": Decimal("0.126"),
    "r5.xlarge": Decimal("0.252"),
}
# Mid-range rather than cheapest: an unknown type should not be dismissed as
# negligible, but must not manufacture savings that dwarf the real findings.
DEFAULT_EC2_HOURLY = EC2_HOURLY["t3.medium"]

# A stopped instance bills nothing for compute but keeps paying for its root
# volume. Used only when the volume itself could not be found in the scan
# inventory — the default AWS root volume for most Linux AMIs.
ASSUMED_ROOT_VOLUME_GB = 8
ASSUMED_ROOT_VOLUME_TYPE = "gp3"

# RDS on-demand single-AZ, $/hour.
# Source: https://aws.amazon.com/rds/postgresql/pricing/
#
# One table for PostgreSQL/MySQL/MariaDB, which is what this table's rates are.
# Commercial engines cost dramatically more per hour on identical hardware
# (SQL Server and Oracle carry license fees), so `engine` is multiplied through
# ENGINE_MULTIPLIER below rather than ignored — an under-estimate on a SQL
# Server instance would bury the single most expensive finding in the report.
RDS_HOURLY: dict[str, Decimal] = {
    "db.t3.micro": Decimal("0.018"),
    "db.t3.small": Decimal("0.036"),
    "db.t3.medium": Decimal("0.072"),
    "db.t3.large": Decimal("0.145"),
    "db.t4g.micro": Decimal("0.016"),
    "db.t4g.small": Decimal("0.032"),
    "db.t4g.medium": Decimal("0.065"),
    "db.t4g.large": Decimal("0.129"),
    "db.m5.large": Decimal("0.178"),
    "db.m5.xlarge": Decimal("0.356"),
    "db.m5.2xlarge": Decimal("0.712"),
    "db.m6g.large": Decimal("0.159"),
    "db.m6g.xlarge": Decimal("0.318"),
    "db.r5.large": Decimal("0.240"),
    "db.r5.xlarge": Decimal("0.480"),
    "db.r6g.large": Decimal("0.214"),
}
# Same reasoning as DEFAULT_EC2_HOURLY: mid-range, so an unrecognised class is
# neither dismissed as free nor inflated past the real findings.
DEFAULT_RDS_HOURLY = RDS_HOURLY["db.m5.large"]

# Rough license uplift over the open-source engines, applied to the hourly
# rate. Approximate by nature — the point is that a SQL Server instance must
# not be priced as if it were PostgreSQL.
ENGINE_MULTIPLIER: dict[str, Decimal] = {
    "sqlserver-ex": Decimal("1.0"),   # Express edition is license-free
    "sqlserver-web": Decimal("1.6"),
    "sqlserver-se": Decimal("3.5"),
    "sqlserver-ee": Decimal("6.0"),
    "oracle-se2": Decimal("2.5"),
    "oracle-ee": Decimal("5.0"),
}
DEFAULT_ENGINE_MULTIPLIER = Decimal("1.0")

# RDS storage, $/GB-month. Source: https://aws.amazon.com/rds/postgresql/pricing/
# Pricier than plain EBS: RDS storage includes the managed-service premium.
RDS_STORAGE_GB_MONTH: dict[str, Decimal] = {
    "gp2": Decimal("0.115"),
    "gp3": Decimal("0.115"),
    "io1": Decimal("0.125"),
    "io2": Decimal("0.125"),
    "standard": Decimal("0.10"),
}
DEFAULT_RDS_STORAGE_GB_MONTH = RDS_STORAGE_GB_MONTH["gp2"]


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


class StaticPricing(Pricing):
    """Prices from the tables above. No network calls, no failure modes."""

    def __init__(self, priced_region: str = PRICED_REGION):
        self.priced_region = priced_region
        self._warned_regions: set[str] = set()

    def _check_region(self, region: str) -> None:
        if region == self.priced_region or region in self._warned_regions:
            return
        self._warned_regions.add(region)
        logger.warning(
            "Pricing %s resources with %s list prices — estimates will be low. "
            "Swap in a live pricing adapter for multi-region accuracy.",
            region,
            self.priced_region,
        )

    def ebs_volume_monthly(self, volume_type: str, size_gb: int, region: str) -> Decimal:
        self._check_region(region)
        rate = EBS_GB_MONTH.get(volume_type, DEFAULT_EBS_GB_MONTH)
        return _round(rate * Decimal(size_gb))

    def ebs_snapshot_monthly(self, size_gb: int, region: str) -> Decimal:
        self._check_region(region)
        return _round(SNAPSHOT_GB_MONTH * Decimal(size_gb))

    def elastic_ip_monthly(self, region: str) -> Decimal:
        self._check_region(region)
        return _round(ELASTIC_IP_HOURLY * HOURS_PER_MONTH)

    def ec2_instance_monthly(self, instance_type: str, region: str) -> Decimal:
        self._check_region(region)
        rate = EC2_HOURLY.get(instance_type, DEFAULT_EC2_HOURLY)
        return _round(rate * HOURS_PER_MONTH)

    def rds_instance_monthly(self, instance_class: str, engine: str, region: str) -> Decimal:
        self._check_region(region)
        rate = RDS_HOURLY.get(instance_class, DEFAULT_RDS_HOURLY)
        multiplier = ENGINE_MULTIPLIER.get(engine, DEFAULT_ENGINE_MULTIPLIER)
        return _round(rate * multiplier * HOURS_PER_MONTH)

    def rds_storage_monthly(self, size_gb: int, storage_type: str, region: str) -> Decimal:
        self._check_region(region)
        rate = RDS_STORAGE_GB_MONTH.get(storage_type, DEFAULT_RDS_STORAGE_GB_MONTH)
        return _round(rate * Decimal(size_gb))
