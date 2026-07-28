"""RDS scanners: idle-by-metrics and stopped-by-state.

Both rules are notify-only, and gated twice over:

- neither `rds_idle` nor `rds_stopped` is remediable, because both are listed
  in domain.rules.NOTIFY_ONLY_RULES; and
- ResourceType.RDS_INSTANCE has no entry in PLAYBOOK_ALLOWLIST at all.

Deleting a database — even with a final snapshot — is the highest blast-radius
action this system could take, and it is deliberately out of scope for v1. The
scanners report; a human acts out-of-band.

Both scanners discover every instance rather than only their own subset. The
upsert is keyed on resource_id so the duplicate is idempotent, and it keeps
each scanner independently correct: an instance in a transient state
(backing-up, modifying) still lands in the inventory instead of falling
through the gap between two filters and being swept up as DELETED.
"""
import uuid
from datetime import UTC, datetime
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

# CloudWatch coordinates for RDS metrics.
RDS_NAMESPACE = "AWS/RDS"
RDS_DIMENSION = "DBInstanceIdentifier"

# The states each rule speaks to. Anything else (creating, modifying,
# backing-up, deleting) is in motion and gets no verdict from either.
AVAILABLE = "available"
STOPPED = "stopped"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _tags(instance: dict[str, Any]) -> dict[str, Any]:
    """RDS returns tags as TagList, not the EC2-style Tags."""
    tag_list = instance.get("TagList") or []
    return {tag["Key"]: tag["Value"] for tag in tag_list if "Key" in tag}


def _to_resource(instance: dict[str, Any], region: str, now: datetime) -> Resource:
    identifier = instance["DBInstanceIdentifier"]
    return Resource(
        id=str(uuid.uuid4()),
        resource_id=identifier,
        resource_type=ResourceType.RDS_INSTANCE,
        resource_arn=instance.get("DBInstanceArn")
        or f"arn:aws:rds:{region}:account:db:{identifier}",
        region=region,
        current_tags=_tags(instance),
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
    )


class IdleRDSScanner(Scanner):
    """Flags available instances nothing has connected to for the window.

    DatabaseConnections is a far stronger idleness signal than CPU: a database
    with zero connections is serving nobody, whereas an idle-looking CPU can
    still be a replica or a nightly-batch target. It is still an inference,
    which is why the rule is notify-only.
    """

    def __init__(
        self,
        region: str,
        pricing: Pricing,
        observation_days: int = 14,
        max_connections: float = 0.0,
        min_datapoints: int = 24,
    ):
        self.region = region
        self.pricing = pricing
        self.observation_days = observation_days
        self.max_connections = max_connections
        self.min_datapoints = min_datapoints
        # discover() collects the series so evaluate() stays pure, keyed by
        # DBInstanceIdentifier.
        self._connections: dict[str, list[float]] = {}

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
        discovered: list[tuple[Resource, dict[str, Any]]] = []
        now = datetime.now(UTC)
        self._connections = {}

        for instance in gateway.describe_rds_instances():
            identifier = instance["DBInstanceIdentifier"]
            # Only available instances have meaningful connection metrics; a
            # stopped one reports nothing, which must not read as "idle".
            if instance.get("DBInstanceStatus") == AVAILABLE:
                self._connections[identifier] = gateway.get_metric_averages(
                    namespace=RDS_NAMESPACE,
                    dimension_name=RDS_DIMENSION,
                    dimension_value=identifier,
                    metric_name="DatabaseConnections",
                    days=self.observation_days,
                )
            discovered.append((_to_resource(instance, self.region, now), instance))

        return discovered

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings: list[Finding] = []
        now = datetime.now(UTC)

        for resource, instance in resources:
            if resource.resource_type != ResourceType.RDS_INSTANCE:
                continue
            if instance.get("DBInstanceStatus") != AVAILABLE:
                continue

            connections = self._connections.get(resource.resource_id)
            if connections is None or len(connections) < self.min_datapoints:
                # Too little history to call it idle — a just-restored instance
                # or a CloudWatch gap. Silence beats a false positive on a
                # production database.
                continue

            average = _mean(connections)
            if average > self.max_connections:
                continue

            instance_class = instance.get("DBInstanceClass", "")
            engine = instance.get("Engine", "")
            storage_gb = int(instance.get("AllocatedStorage") or 0)
            # Deleting an idle instance stops both meters, so the finding
            # reports compute plus storage.
            cost = self.pricing.rds_instance_monthly(
                instance_class=instance_class, engine=engine, region=resource.region
            ) + self.pricing.rds_storage_monthly(
                size_gb=storage_gb,
                storage_type=instance.get("StorageType", ""),
                region=resource.region,
            )

            findings.append(
                Finding(
                    id=f"rds_idle|{resource.resource_id}",
                    resource_ref=resource.id,
                    rule="rds_idle",
                    evidence={
                        "DBInstanceIdentifier": resource.resource_id,
                        "DBInstanceClass": instance_class,
                        "Engine": engine,
                        "MultiAZ": instance.get("MultiAZ", False),
                        "AllocatedStorageGB": storage_gb,
                        "avg_connections": round(average, 3),
                        "max_connections_seen": round(max(connections), 3),
                        "observation_days": self.observation_days,
                        "datapoints": len(connections),
                        "connection_threshold": self.max_connections,
                        "notify_only": True,
                    },
                    tags_at_detection=resource.current_tags,
                    est_monthly_cost_usd=cost,
                    status=FindingStatus.OPEN,
                    protected=tag_is_protected(resource.current_tags),
                    detected_at=now,
                    last_seen_at=now,
                )
            )

        return findings


class StoppedRDSScanner(Scanner):
    """Flags stopped instances, which are not the saving they look like.

    Two things make "I stopped it" a non-fix, and both are why this rule has no
    grace period:

    - allocated storage bills at the full rate while the engine is down; and
    - AWS restarts a stopped RDS instance automatically after 7 days, so the
      compute charge comes back on its own.

    There is also nothing to measure a grace period against: DescribeDBInstances
    exposes no stopped-since timestamp, so "stopped for N days" is not a
    question the API can answer. The state is the finding.
    """

    def __init__(self, region: str, pricing: Pricing):
        self.region = region
        self.pricing = pricing

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
        now = datetime.now(UTC)
        return [
            (_to_resource(instance, self.region, now), instance)
            for instance in gateway.describe_rds_instances()
        ]

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings: list[Finding] = []
        now = datetime.now(UTC)

        for resource, instance in resources:
            if resource.resource_type != ResourceType.RDS_INSTANCE:
                continue
            if instance.get("DBInstanceStatus") != STOPPED:
                continue

            storage_gb = int(instance.get("AllocatedStorage") or 0)
            storage_type = instance.get("StorageType", "")
            # Storage only: compute is genuinely not billed while stopped, and
            # claiming otherwise would overstate the saving.
            cost = self.pricing.rds_storage_monthly(
                size_gb=storage_gb, storage_type=storage_type, region=resource.region
            )

            findings.append(
                Finding(
                    id=f"rds_stopped|{resource.resource_id}",
                    resource_ref=resource.id,
                    rule="rds_stopped",
                    evidence={
                        "DBInstanceIdentifier": resource.resource_id,
                        "DBInstanceClass": instance.get("DBInstanceClass", ""),
                        "Engine": instance.get("Engine", ""),
                        "AllocatedStorageGB": storage_gb,
                        "StorageType": storage_type,
                        "auto_restarts_after_days": 7,
                        "compute_billed_while_stopped": False,
                        "notify_only": True,
                    },
                    tags_at_detection=resource.current_tags,
                    est_monthly_cost_usd=cost,
                    status=FindingStatus.OPEN,
                    protected=tag_is_protected(resource.current_tags),
                    detected_at=now,
                    last_seen_at=now,
                )
            )

        return findings
