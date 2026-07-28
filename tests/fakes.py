"""Shared in-memory test doubles.

Deliberately not in conftest.py: pytest imports conftest itself, so a test
module importing from it can end up with the module loaded twice under two
names — and two distinct copies of the same class, which makes isinstance
checks and identity comparisons fail in ways that are painful to diagnose.

Equally deliberately not left in test_services.py, where several of these
started. Importing helpers from one test module into another couples their
collection order and makes deleting a test a cross-file concern.
"""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.ports.advisor import Advisor
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier


def make_resource(
    res_id: str = "res-mock",
    resource_id: str = "vol-123",
    resource_type: ResourceType = ResourceType.EBS_VOLUME,
    tags: dict[str, Any] | None = None,
) -> Resource:
    now = datetime.now(UTC)
    return Resource(
        id=res_id,
        resource_id=resource_id,
        resource_type=resource_type,
        resource_arn="arn",
        region="us-east-1",
        current_tags=tags or {},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
    )


def make_finding(
    finding_id: str = "f-mock",
    status: FindingStatus = FindingStatus.NOTIFIED,
    resource_ref: str = "res-mock",
    protected: bool = False,
    rule: str = "ebs",
) -> Finding:
    now = datetime.now(UTC)
    return Finding(
        id=finding_id,
        resource_ref=resource_ref,
        rule=rule,
        evidence={},
        tags_at_detection={},
        est_monthly_cost_usd=Decimal("1.00"),
        status=status,
        protected=protected,
        detected_at=now,
        last_seen_at=now,
    )


class FakeGatewayBase(CloudGateway):
    """Every CloudGateway method, each refusing to be called.

    Test fakes subclass this and override only what their scenario needs, so a
    scanner that reaches for a port it should not touch fails loudly instead of
    quietly receiving an empty list. It also means adding a method to the port
    updates every fake in one place rather than N.
    """

    def describe_ebs_volumes(self):  # pragma: no cover
        raise AssertionError("describe_ebs_volumes not stubbed for this test")

    def describe_elastic_ips(self):  # pragma: no cover
        raise AssertionError("describe_elastic_ips not stubbed for this test")

    def describe_ec2_instances(self):  # pragma: no cover
        raise AssertionError("describe_ec2_instances not stubbed for this test")

    def describe_ebs_snapshots(self):  # pragma: no cover
        raise AssertionError("describe_ebs_snapshots not stubbed for this test")

    def describe_running_ec2_instances(self):  # pragma: no cover
        raise AssertionError("describe_running_ec2_instances not stubbed for this test")

    def describe_rds_instances(self):  # pragma: no cover
        raise AssertionError("describe_rds_instances not stubbed for this test")

    def describe_s3_buckets(self):  # pragma: no cover
        raise AssertionError("describe_s3_buckets not stubbed for this test")

    def get_incomplete_multipart_uploads(self, bucket):  # pragma: no cover
        raise AssertionError("get_incomplete_multipart_uploads not stubbed for this test")

    def get_metric_averages(
        self, namespace, dimensions, metric_name, days, period_seconds=3600
    ):  # pragma: no cover
        raise AssertionError("get_metric_averages not stubbed for this test")

    def execute(self, playbook, resource_id, dry_run):  # pragma: no cover
        raise AssertionError("execute not stubbed for this test")


class FakeCloudGateway(FakeGatewayBase):
    """In-memory gateway: records executed playbooks, can be told to fail."""

    def __init__(self, fail: bool = False):
        self.executed: list[tuple[str, str, bool]] = []
        self.fail = fail

    def describe_ebs_volumes(self): return []
    def describe_elastic_ips(self): return []
    def describe_ec2_instances(self): return []
    def describe_ebs_snapshots(self): return []
    def describe_running_ec2_instances(self): return []

    def get_metric_averages(
        self, namespace, dimensions, metric_name, days, period_seconds=3600
    ):
        return []

    def execute(self, playbook, resource_id, dry_run):
        if self.fail:
            raise RuntimeError("cloud exploded")
        self.executed.append((playbook, resource_id, dry_run))
        return {"snapshot_id": f"snap-{resource_id}"} if not dry_run else {"dry_run": True}


def resolver(gateway):
    """approve_finding takes a region -> gateway resolver, not a gateway."""
    return lambda _region: gateway


class FakeNotifier(Notifier):
    """Records what was sent. Zero Slack, zero HTTP."""

    def __init__(self):
        self.alerts: list[tuple[str, str]] = []
        self.digests: list[tuple[str, list[str]]] = []

    @property
    def channel_name(self):
        return "fake"

    def send_finding_alert(self, finding, resource):
        self.alerts.append((finding.id, resource.resource_id))
        return f"msg-{len(self.alerts)}"

    def send_digest(self, title, sections):
        self.digests.append((title, sections))
        return f"digest-{len(self.digests)}"

    def parse_callback(self, raw_body, headers):
        raise ValueError("not supported")

    def confirm_decision(self, reply_context, text):
        pass


class FakeAdvisor(Advisor):
    """Records what it was asked, returns deterministic prose."""

    def __init__(self):
        self.calls: list[str] = []
        self.narrations: list[tuple[str, dict[str, Any]]] = []

    def summarize(self, finding, resource):
        self.calls.append(finding.id)
        return f"advice for {finding.rule} on {resource.resource_id}"

    def narrate(self, topic, facts):
        self.narrations.append((topic, facts))
        return f"narrated {topic}"
