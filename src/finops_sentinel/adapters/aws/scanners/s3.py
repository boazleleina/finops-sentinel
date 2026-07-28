"""S3 waste: a different shape from every other rule in this system.

Everywhere else a resource either is or is not waste. A bucket is neither —
buckets are free to exist, storage is not, and nobody wants the agent deleting
objects. So this scanner emits two rules with deliberately different powers:

- `s3_incomplete_multipart` is exactly measurable and remediable. An abandoned
  multipart upload bills at full storage rate for parts that never became an
  object, and nothing in the console lists them. Aborting one deletes no data,
  because no object was ever created.
- `s3_no_lifecycle` is advisory. A bucket without a lifecycle policy is not
  100% waste, and the only honest thing to report is a bounded fraction of its
  cost. It is listed in NOTIFY_ONLY_RULES, so the type-keyed playbook allowlist
  cannot hand it the abort playbook that belongs to its sibling rule.

Bucket size comes from the CloudWatch BucketSizeBytes daily metric — S3 has no
size API. A bucket with no datapoints yields NO finding: unknown size is not
evidence of anything, and guessing it by listing objects would mean an
unbounded walk of a production bucket.
"""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.rules import is_protected as tag_is_protected
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.pricing import Pricing
from finops_sentinel.ports.scanner import Scanner

S3_NAMESPACE = "AWS/S3"
# The storage class BucketSizeBytes is reported under for ordinary objects —
# which is exactly the class a bucket with no lifecycle policy stays in.
STANDARD_STORAGE = "StandardStorage"
BYTES_PER_GB = 1024**3

# BucketSizeBytes is published once a day, so a short window can miss it
# entirely on a quiet bucket.
SIZE_LOOKBACK_DAYS = 3
SIZE_PERIOD_SECONDS = 86400

# A lifecycle rule that expires old versions is the one that actually reclaims
# space in a versioned bucket; its absence is what makes versioning expensive.
NONCURRENT_EXPIRATION = "NoncurrentVersionExpiration"

# Same floor the pricing adapter applies: a real cost must never round to zero,
# because zero reads as "free" in the savings total.
MIN_REPORTED_COST = Decimal("0.01")


def _at_least_a_cent(amount: Decimal) -> Decimal:
    return max(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), MIN_REPORTED_COST)


def _initiated(upload: dict[str, Any]) -> datetime:
    """Upload start time, always timezone-aware.

    boto3 returns aware datetimes; hand-built test fixtures often do not, and a
    naive/aware comparison raises rather than misbehaving quietly.
    """
    started: datetime = upload["Initiated"]
    return started if started.tzinfo else started.replace(tzinfo=UTC)


class S3LifecycleScanner(Scanner):
    """Flags buckets with no lifecycle policy, and abandoned multipart uploads."""

    def __init__(
        self,
        region: str,
        pricing: Pricing,
        min_bucket_size_gb: float = 50.0,
        incomplete_mpu_age_days: int = 7,
        addressable_fraction: float = 0.20,
    ):
        self.region = region
        self.pricing = pricing
        self.min_bucket_size_gb = min_bucket_size_gb
        self.incomplete_mpu_age_days = incomplete_mpu_age_days
        self.addressable_fraction = addressable_fraction
        # Collected in discover() so evaluate() stays pure, keyed by bucket name.
        self._sizes_gb: dict[str, float | None] = {}
        self._uploads: dict[str, list[dict[str, Any]]] = {}

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
        discovered: list[tuple[Resource, dict[str, Any]]] = []
        now = datetime.now(UTC)
        self._sizes_gb = {}
        self._uploads = {}

        for bucket in gateway.describe_s3_buckets():
            name = bucket["Name"]

            series = gateway.get_metric_averages(
                namespace=S3_NAMESPACE,
                # BucketSizeBytes is published against BOTH dimensions, and
                # CloudWatch matches dimension sets exactly — querying by
                # BucketName alone returns nothing at all.
                dimensions={"BucketName": name, "StorageType": STANDARD_STORAGE},
                metric_name="BucketSizeBytes",
                days=SIZE_LOOKBACK_DAYS,
                period_seconds=SIZE_PERIOD_SECONDS,
            )
            # Most recent reading wins; no readings means unknown, not empty.
            self._sizes_gb[name] = series[-1] / BYTES_PER_GB if series else None
            self._uploads[name] = gateway.get_incomplete_multipart_uploads(name)

            tags = {tag["Key"]: tag["Value"] for tag in bucket.get("Tags", [])}
            discovered.append(
                (
                    Resource(
                        id=str(uuid.uuid4()),
                        resource_id=name,
                        resource_type=ResourceType.S3_BUCKET,
                        resource_arn=f"arn:aws:s3:::{name}",
                        region=self.region,
                        current_tags=tags,
                        lifecycle=ResourceLifecycle.ACTIVE,
                        first_seen_at=now,
                        last_seen_at=now,
                    ),
                    bucket,
                )
            )

        return discovered

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings: list[Finding] = []
        now = datetime.now(UTC)

        for resource, bucket in resources:
            if resource.resource_type != ResourceType.S3_BUCKET:
                continue

            protected = tag_is_protected(resource.current_tags)
            lifecycle_finding = self._evaluate_lifecycle(resource, bucket, protected, now)
            if lifecycle_finding is not None:
                findings.append(lifecycle_finding)

            mpu_finding = self._evaluate_multipart(resource, protected, now)
            if mpu_finding is not None:
                findings.append(mpu_finding)

        return findings

    def _evaluate_lifecycle(
        self,
        resource: Resource,
        bucket: dict[str, Any],
        protected: bool,
        now: datetime,
    ) -> Finding | None:
        if bucket.get("LifecycleRules"):
            return None

        size_gb = self._sizes_gb.get(resource.resource_id)
        if size_gb is None or size_gb < self.min_bucket_size_gb:
            # Unknown size, or too small for a policy to be worth the argument.
            return None

        full_cost = self.pricing.s3_storage_monthly(
            size_gb=size_gb, storage_class="STANDARD", region=resource.region
        )
        # Only a fraction of a bucket is ever cold enough to tier or expire.
        # Reporting the whole bucket as recoverable would let one large bucket
        # dominate the savings total and make every number in the report
        # untrustworthy. The inputs are in evidence so the estimate is auditable.
        addressable = full_cost * Decimal(str(self.addressable_fraction))

        versioning = bucket.get("Versioning", "")
        has_noncurrent_rule = any(
            NONCURRENT_EXPIRATION in rule for rule in bucket.get("LifecycleRules", [])
        )

        return Finding(
            id=f"s3_no_lifecycle|{resource.resource_id}",
            resource_ref=resource.id,
            rule="s3_no_lifecycle",
            evidence={
                "Bucket": resource.resource_id,
                "size_gb": round(size_gb, 2),
                "full_storage_cost_usd": str(full_cost),
                "addressable_fraction": self.addressable_fraction,
                "size_threshold_gb": self.min_bucket_size_gb,
                "versioning": versioning or "Disabled",
                # Versioning without an expiry rule means every overwrite keeps
                # billing forever — the expensive half of this finding.
                "noncurrent_versions_accumulating": versioning == "Enabled"
                and not has_noncurrent_rule,
                "notify_only": True,
            },
            tags_at_detection=resource.current_tags,
            est_monthly_cost_usd=_at_least_a_cent(addressable),
            status=FindingStatus.OPEN,
            protected=protected,
            detected_at=now,
            last_seen_at=now,
        )

    def _evaluate_multipart(
        self, resource: Resource, protected: bool, now: datetime
    ) -> Finding | None:
        uploads = self._uploads.get(resource.resource_id) or []
        cutoff = now - timedelta(days=self.incomplete_mpu_age_days)

        stale = [u for u in uploads if _initiated(u) <= cutoff]
        if not stale:
            return None

        total_bytes = sum(int(u.get("SizeBytes", 0)) for u in stale)
        size_gb = total_bytes / BYTES_PER_GB
        oldest = min(_initiated(u) for u in stale)

        return Finding(
            id=f"s3_incomplete_multipart|{resource.resource_id}",
            resource_ref=resource.id,
            rule="s3_incomplete_multipart",
            evidence={
                "Bucket": resource.resource_id,
                "upload_count": len(stale),
                "total_bytes": total_bytes,
                "size_gb": round(size_gb, 4),
                "oldest_initiated": oldest.isoformat(),
                "age_threshold_days": self.incomplete_mpu_age_days,
                # Stated because the playbook re-checks age at execution time,
                # so what runs may be a subset of what was detected.
                "aborts_only_uploads_older_than_threshold": True,
            },
            tags_at_detection=resource.current_tags,
            est_monthly_cost_usd=self.pricing.s3_storage_monthly(
                size_gb=size_gb, storage_class="STANDARD", region=resource.region
            ),
            status=FindingStatus.OPEN,
            protected=protected,
            detected_at=now,
            last_seen_at=now,
        )
