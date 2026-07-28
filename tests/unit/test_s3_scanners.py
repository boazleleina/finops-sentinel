"""S3 scanners: the advisory rule, the measurable one, and the abort playbook.

The two rules on S3_BUCKET have deliberately different powers, and most of
these tests exist to pin that asymmetry down: s3_incomplete_multipart can act,
s3_no_lifecycle cannot, and the type-keyed playbook allowlist must not let the
second borrow the first's playbook.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.adapters.aws.scanners.s3 import BYTES_PER_GB, S3LifecycleScanner
from finops_sentinel.domain.models import FindingStatus, ResourceType
from finops_sentinel.domain.rules import PLAYBOOK_ALLOWLIST, is_remediable
from finops_sentinel.domain.services import approve_finding
from tests.conftest import FakeGatewayBase

NOW = datetime.now(UTC)


class FakeS3Gateway(FakeGatewayBase):
    """Canned buckets, sizes and uploads; every other port refuses."""

    def __init__(self, buckets, sizes=None, uploads=None):
        self.buckets = buckets
        self.sizes = sizes or {}
        self.uploads = uploads or {}
        self.metric_dimensions = []

    def describe_s3_buckets(self):
        return self.buckets

    def get_incomplete_multipart_uploads(self, bucket):
        return self.uploads.get(bucket, [])

    def get_metric_averages(
        self, namespace, dimensions, metric_name, days, period_seconds=3600
    ):
        self.metric_dimensions.append(dimensions)
        size_gb = self.sizes.get(dimensions["BucketName"])
        # Absent means CloudWatch has no datapoints for this bucket.
        return [] if size_gb is None else [size_gb * BYTES_PER_GB]


def _bucket(name, *, rules=None, versioning="", tags=None):
    return {
        "Name": name,
        "CreationDate": NOW,
        "LifecycleRules": rules or [],
        "Versioning": versioning,
        "Tags": tags or [],
    }


def _upload(key, *, days_old, size_bytes=1024):
    return {
        "Key": key,
        "UploadId": f"upload-{key}",
        "Initiated": NOW - timedelta(days=days_old),
        "SizeBytes": size_bytes,
    }


def _scan(gateway, **kwargs):
    scanner = S3LifecycleScanner(region="us-east-1", pricing=StaticPricing(), **kwargs)
    return scanner.evaluate(scanner.discover(gateway))


def _rule_ids(findings):
    return {f.rule for f in findings}


# --------------------------------------------------------------------------
# s3_no_lifecycle
# --------------------------------------------------------------------------


def test_large_bucket_without_a_lifecycle_policy_is_flagged():
    findings = _scan(FakeS3Gateway([_bucket("data-lake")], sizes={"data-lake": 500.0}))

    assert _rule_ids(findings) == {"s3_no_lifecycle"}
    finding = findings[0]
    assert finding.evidence["size_gb"] == 500.0
    assert finding.evidence["notify_only"] is True
    # 500GB STANDARD = $11.50/mo full, of which 20% is treated as addressable.
    assert finding.evidence["full_storage_cost_usd"] == "11.50"
    assert finding.est_monthly_cost_usd == Decimal("2.30")


def test_bucket_with_a_lifecycle_policy_is_not_flagged():
    gateway = FakeS3Gateway(
        [_bucket("managed", rules=[{"ID": "expire-old", "Status": "Enabled"}])],
        sizes={"managed": 900.0},
    )

    assert _scan(gateway) == []


def test_small_bucket_is_not_worth_an_alert():
    gateway = FakeS3Gateway([_bucket("tiny")], sizes={"tiny": 2.0})

    assert _scan(gateway, min_bucket_size_gb=50.0) == []


def test_bucket_of_unknown_size_is_not_flagged():
    """No BucketSizeBytes datapoints means unknown, which is not evidence.

    S3 has no size API, so guessing by listing objects would mean an unbounded
    walk of a production bucket.
    """
    gateway = FakeS3Gateway([_bucket("silent")], sizes={})

    assert _scan(gateway) == []


def test_savings_are_a_fraction_of_the_bucket_not_the_whole_thing():
    """One large bucket must not dominate the savings total.

    A bucket without a policy is not 100% waste — only the cold part of it can
    ever be tiered or expired.
    """
    gateway = FakeS3Gateway([_bucket("huge")], sizes={"huge": 10_000.0})

    finding = _scan(gateway, addressable_fraction=0.20)[0]
    full = Decimal(finding.evidence["full_storage_cost_usd"])

    assert finding.est_monthly_cost_usd < full
    assert finding.est_monthly_cost_usd == (full * Decimal("0.20")).quantize(
        Decimal("0.01")
    )


def test_versioned_bucket_without_an_expiry_rule_is_called_out():
    """Versioning with no expiry means every overwrite bills forever."""
    gateway = FakeS3Gateway(
        [_bucket("versioned", versioning="Enabled")], sizes={"versioned": 200.0}
    )

    evidence = _scan(gateway)[0].evidence
    assert evidence["versioning"] == "Enabled"
    assert evidence["noncurrent_versions_accumulating"] is True


def test_unversioned_bucket_is_not_accused_of_hoarding_versions():
    gateway = FakeS3Gateway([_bucket("plain")], sizes={"plain": 200.0})

    evidence = _scan(gateway)[0].evidence
    assert evidence["versioning"] == "Disabled"
    assert evidence["noncurrent_versions_accumulating"] is False


def test_size_is_queried_with_both_cloudwatch_dimensions():
    """CloudWatch matches dimension sets exactly, not by subset.

    BucketSizeBytes is published against BucketName AND StorageType. Querying
    it by BucketName alone returns nothing at all against real AWS, so every
    bucket would silently read as unknown size and no finding would ever fire.
    """
    gateway = FakeS3Gateway([_bucket("data-lake")], sizes={"data-lake": 500.0})
    _scan(gateway)

    assert gateway.metric_dimensions == [
        {"BucketName": "data-lake", "StorageType": "StandardStorage"}
    ]


def test_protected_bucket_carries_the_flag():
    gateway = FakeS3Gateway(
        [_bucket("prot", tags=[{"Key": "finops:protected", "Value": "true"}])],
        sizes={"prot": 500.0},
    )

    assert _scan(gateway)[0].protected is True


# --------------------------------------------------------------------------
# s3_incomplete_multipart
# --------------------------------------------------------------------------


def test_abandoned_multipart_uploads_are_flagged():
    gateway = FakeS3Gateway(
        [_bucket("uploads", rules=[{"ID": "has-policy"}])],
        uploads={"uploads": [_upload("big.bin", days_old=30, size_bytes=5 * BYTES_PER_GB)]},
    )

    findings = _scan(gateway)

    assert _rule_ids(findings) == {"s3_incomplete_multipart"}
    evidence = findings[0].evidence
    assert evidence["upload_count"] == 1
    assert evidence["total_bytes"] == 5 * BYTES_PER_GB
    # 5GB STANDARD at $0.023 = $0.12/mo. Exact, unlike the lifecycle estimate.
    assert findings[0].est_monthly_cost_usd == Decimal("0.12")


def test_recent_multipart_uploads_are_left_alone():
    """A young upload is a client mid-transfer, not abandoned waste."""
    gateway = FakeS3Gateway(
        [_bucket("uploads", rules=[{"ID": "has-policy"}])],
        uploads={"uploads": [_upload("inflight.bin", days_old=0)]},
    )

    assert _scan(gateway, incomplete_mpu_age_days=7) == []


def test_only_the_stale_uploads_are_counted():
    gateway = FakeS3Gateway(
        [_bucket("mixed", rules=[{"ID": "has-policy"}])],
        uploads={
            "mixed": [
                _upload("old.bin", days_old=30, size_bytes=2048),
                _upload("older.bin", days_old=60, size_bytes=1024),
                _upload("fresh.bin", days_old=1, size_bytes=999_999),
            ]
        },
    )

    evidence = _scan(gateway, incomplete_mpu_age_days=7)[0].evidence

    assert evidence["upload_count"] == 2
    assert evidence["total_bytes"] == 3072


def test_a_bucket_can_raise_both_rules_at_once():
    """They describe different waste and are independently actionable."""
    gateway = FakeS3Gateway(
        [_bucket("busy")],
        sizes={"busy": 500.0},
        uploads={"busy": [_upload("stale.bin", days_old=30)]},
    )

    assert _rule_ids(_scan(gateway)) == {"s3_no_lifecycle", "s3_incomplete_multipart"}


# --------------------------------------------------------------------------
# The asymmetry: one rule may act, its sibling may not
# --------------------------------------------------------------------------


def test_multipart_rule_is_remediable_and_lifecycle_rule_is_not():
    assert is_remediable("s3_incomplete_multipart") is True
    assert is_remediable("s3_no_lifecycle") is False


def test_s3_bucket_type_has_exactly_one_playbook():
    assert PLAYBOOK_ALLOWLIST[ResourceType.S3_BUCKET] == "abort_incomplete_multipart_uploads"


def test_lifecycle_finding_cannot_borrow_its_siblings_playbook(repository):
    """The exact hole NOTIFY_ONLY_RULES exists to close.

    Both rules sit on S3_BUCKET, and the allowlist is keyed by type, so without
    the rule-level gate approving "no lifecycle policy" would abort uploads.
    """
    from tests.unit.test_services import (
        FakeCloudGateway,
        make_finding,
        make_resource,
        resolver,
    )

    repository.upsert_resource(
        make_resource(resource_id="data-lake", resource_type=ResourceType.S3_BUCKET)
    )
    repository.save_finding(make_finding(rule="s3_no_lifecycle"))
    gateway = FakeCloudGateway()

    approved = approve_finding(
        "f-mock", repository, resolver(gateway), actor="boaz", channel="slack",
        dry_run=False,
    )

    assert approved is False
    assert gateway.executed == []
    assert any(
        e.event == "approve_blocked_notify_only"
        for e in repository.get_audit_events("f-mock")
    )


def test_multipart_finding_is_approvable(repository):
    from tests.unit.test_services import (
        FakeCloudGateway,
        make_finding,
        make_resource,
        resolver,
    )

    repository.upsert_resource(
        make_resource(resource_id="uploads", resource_type=ResourceType.S3_BUCKET)
    )
    repository.save_finding(make_finding(rule="s3_incomplete_multipart"))
    gateway = FakeCloudGateway()

    approved = approve_finding(
        "f-mock", repository, resolver(gateway), actor="boaz", channel="slack",
        dry_run=False,
    )

    assert approved is True
    assert gateway.executed == [("abort_incomplete_multipart_uploads", "uploads", False)]
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.REMEDIATED


# --------------------------------------------------------------------------
# The real adapter and the abort playbook, against moto
# --------------------------------------------------------------------------


@pytest.fixture
def s3_env():
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


def _start_upload(client, bucket, key, *, parts=1):
    upload_id = client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    for number in range(1, parts + 1):
        client.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number,
            Body=b"x" * 1024,
        )
    return upload_id


def test_gateway_reports_lifecycle_versioning_and_tags(s3_env):
    s3_env.create_bucket(Bucket="configured")
    s3_env.put_bucket_versioning(
        Bucket="configured", VersioningConfiguration={"Status": "Enabled"}
    )
    s3_env.put_bucket_tagging(
        Bucket="configured", Tagging={"TagSet": [{"Key": "env", "Value": "prod"}]}
    )
    s3_env.put_bucket_lifecycle_configuration(
        Bucket="configured",
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": 30},
                }
            ]
        },
    )

    buckets = Boto3Gateway(region="us-east-1").describe_s3_buckets()
    bucket = next(b for b in buckets if b["Name"] == "configured")

    assert bucket["Versioning"] == "Enabled"
    assert bucket["Tags"] == [{"Key": "env", "Value": "prod"}]
    assert len(bucket["LifecycleRules"]) == 1


def test_gateway_treats_missing_configuration_as_absent_not_broken(s3_env):
    """S3 404s when a bucket simply has no policy — normal, not an error."""
    s3_env.create_bucket(Bucket="bare")

    bucket = next(
        b for b in Boto3Gateway(region="us-east-1").describe_s3_buckets()
        if b["Name"] == "bare"
    )

    assert bucket["LifecycleRules"] == []
    assert bucket["Versioning"] == ""
    assert bucket["Tags"] == []


def test_gateway_sums_the_parts_of_an_incomplete_upload(s3_env):
    s3_env.create_bucket(Bucket="parts")
    _start_upload(s3_env, "parts", "big.bin", parts=3)

    uploads = Boto3Gateway(region="us-east-1").get_incomplete_multipart_uploads("parts")

    assert len(uploads) == 1
    assert uploads[0]["SizeBytes"] == 3 * 1024


def test_abort_playbook_reclaims_only_the_aged_uploads(s3_env):
    """Age is re-checked at execution time, not trusted from detection.

    An approval can sit in Slack for hours; a client may legitimately start a
    large upload in the meantime.
    """
    s3_env.create_bucket(Bucket="mixed")
    _start_upload(s3_env, "mixed", "abandoned.bin")

    # moto stamps every upload with a fixed date in 2010, so the threshold is
    # what moves in this test rather than the upload's age. A threshold wider
    # than that gap makes the upload "too recent" and must leave it untouched.
    patient = Boto3Gateway(region="us-east-1", mpu_age_days=100_000)
    result = patient.execute(
        "abort_incomplete_multipart_uploads", "mixed", dry_run=False
    )

    assert result["aborted"] == 0
    assert result["skipped_too_recent"] == 1
    assert len(s3_env.list_multipart_uploads(Bucket="mixed").get("Uploads", [])) == 1

    # At the real threshold the same upload is long abandoned and gets reclaimed.
    gateway = Boto3Gateway(region="us-east-1", mpu_age_days=7)
    result = gateway.execute("abort_incomplete_multipart_uploads", "mixed", dry_run=False)

    assert result["aborted"] == 1
    assert result["bytes_reclaimed"] == 1024
    assert s3_env.list_multipart_uploads(Bucket="mixed").get("Uploads", []) == []


def test_abort_playbook_honors_dry_run(s3_env):
    s3_env.create_bucket(Bucket="untouched")
    _start_upload(s3_env, "untouched", "keep.bin")

    gateway = Boto3Gateway(region="us-east-1", mpu_age_days=0)
    result = gateway.execute(
        "abort_incomplete_multipart_uploads", "untouched", dry_run=True
    )

    assert result == {"dry_run": True}
    assert len(s3_env.list_multipart_uploads(Bucket="untouched").get("Uploads", [])) == 1


def test_end_to_end_scan_against_moto(s3_env):
    s3_env.create_bucket(Bucket="lake")
    _start_upload(s3_env, "lake", "abandoned.bin", parts=2)

    gateway = Boto3Gateway(region="us-east-1")
    scanner = S3LifecycleScanner(
        region="us-east-1", pricing=StaticPricing(), incomplete_mpu_age_days=0
    )
    resources = scanner.discover(gateway)
    findings = scanner.evaluate(resources)

    assert resources[0][0].resource_type == ResourceType.S3_BUCKET
    assert resources[0][0].resource_arn == "arn:aws:s3:::lake"
    # No BucketSizeBytes in moto, so only the exactly-measurable rule fires.
    assert _rule_ids(findings) == {"s3_incomplete_multipart"}
    assert findings[0].evidence["total_bytes"] == 2 * 1024
