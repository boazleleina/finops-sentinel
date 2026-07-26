import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from finops_sentinel.adapters.aws.pricing import (
    ASSUMED_ROOT_VOLUME_GB,
    ASSUMED_ROOT_VOLUME_TYPE,
)
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

# AWS reports the stop time only inside StateTransitionReason, e.g.
# "User initiated (2026-07-01 12:34:56 GMT)".
_STOP_TIME_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_stop_time(state_transition_reason: str | None) -> datetime | None:
    if not state_transition_reason:
        return None
    match = _STOP_TIME_RE.search(state_transition_reason)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


class StoppedEC2Scanner(Scanner):

    def __init__(self, region: str, pricing: Pricing, threshold_days: int = 7):
        self.region = region
        self.pricing = pricing
        self.threshold_days = threshold_days

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
        discovered = []
        instances = gateway.describe_ec2_instances()
        now = datetime.now(UTC)

        for instance in instances:
            instance_id = instance["InstanceId"]
            tags = instance.get("Tags", [])
            tags_dict = {t["Key"]: t["Value"] for t in tags} if isinstance(tags, list) else tags

            arn = f"arn:aws:ec2:{self.region}:account:instance/{instance_id}"

            resource = Resource(
                id=str(uuid.uuid4()),
                resource_id=instance_id,
                resource_type=ResourceType.EC2_INSTANCE,
                resource_arn=arn,
                region=self.region,
                current_tags=tags_dict,
                lifecycle=ResourceLifecycle.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
            discovered.append((resource, instance))

        return discovered

    def _root_volume_cost(
        self, instance: dict[str, Any], volumes: dict[str, dict[str, Any]], region: str
    ) -> tuple[Decimal, bool]:
        """Cost of the EBS volumes a stopped instance keeps paying for.

        Compute is free while stopped; the attached volumes are not. Volume
        sizes come from the pooled scan inventory (UnattachedEBSScanner
        discovers every volume, not just detached ones) — the same pattern
        OldEbsSnapshotScanner uses for orphan detection. Returns the cost and
        whether it had to fall back to an assumed root volume.
        """
        total = Decimal(0)
        matched = False
        for mapping in instance.get("BlockDeviceMappings", []):
            volume_id = mapping.get("Ebs", {}).get("VolumeId")
            volume = volumes.get(volume_id) if volume_id else None
            if volume is None:
                continue
            matched = True
            total += self.pricing.ebs_volume_monthly(
                volume_type=volume.get("VolumeType", "gp2"),
                size_gb=int(volume["Size"]),
                region=region,
            )

        if matched:
            return total, False

        # The EBS scanner is disabled, or the API did not report the mapping.
        # Assume a default root volume rather than reporting $0, which would
        # read as "this instance is free to keep".
        return (
            self.pricing.ebs_volume_monthly(
                volume_type=ASSUMED_ROOT_VOLUME_TYPE,
                size_gb=ASSUMED_ROOT_VOLUME_GB,
                region=region,
            ),
            True,
        )

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings = []
        now = datetime.now(UTC)

        volumes_by_id = {
            res.resource_id: raw
            for res, raw in resources
            if res.resource_type == ResourceType.EBS_VOLUME
        }

        for resource, instance in resources:
            if resource.resource_type != ResourceType.EC2_INSTANCE:
                continue
            # run_scan hands every scanner the combined inventory, so this list
            # also holds RUNNING instances discovered by IdleEC2Scanner. They
            # have no StateTransitionReason, which the unknown-stop-time branch
            # below would otherwise treat as "flag it anyway".
            if instance.get("State", {}).get("Name") != "stopped":
                continue

            stopped_days: int | None
            stopped_at = parse_stop_time(instance.get("StateTransitionReason"))
            if stopped_at is not None:
                stopped_days = (now - stopped_at).days
                if stopped_days < self.threshold_days:
                    continue
            else:
                # Stop time unknown (e.g. LocalStack omits it) — flag anyway;
                # the human approval gate is the safety net.
                stopped_days = None

            savings, cost_estimated = self._root_volume_cost(
                instance, volumes_by_id, resource.region
            )

            finding = Finding(
                id=f"ec2_stopped|{resource.resource_id}",
                resource_ref=resource.id,
                rule="ec2_stopped",
                evidence={
                    **instance,
                    "stopped_days": stopped_days,
                    "threshold_days": self.threshold_days,
                    "cost_basis": (
                        "assumed root volume" if cost_estimated else "attached EBS volumes"
                    ),
                },
                tags_at_detection=resource.current_tags,
                est_monthly_cost_usd=savings,
                status=FindingStatus.OPEN,
                protected=tag_is_protected(resource.current_tags),
                detected_at=now,
                last_seen_at=now,
            )
            findings.append(finding)

        return findings
