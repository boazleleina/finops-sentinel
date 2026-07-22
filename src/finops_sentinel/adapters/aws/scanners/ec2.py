import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.rules import is_protected as tag_is_protected
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.scanner import Scanner

# AWS reports the stop time only inside StateTransitionReason, e.g.
# "User initiated (2026-07-01 12:34:56 GMT)".
_STOP_TIME_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_stop_time(state_transition_reason: str | None) -> Optional[datetime]:
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

    def __init__(self, region: str, threshold_days: int = 7):
        self.region = region
        self.threshold_days = threshold_days

    def discover(self, gateway: CloudGateway) -> List[Tuple[Resource, dict[str, Any]]]:
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

    def evaluate(self, resources: List[Tuple[Resource, dict[str, Any]]]) -> List[Finding]:
        findings = []
        now = datetime.now(UTC)

        for resource, instance in resources:
            if resource.resource_type != ResourceType.EC2_INSTANCE:
                continue

            stopped_days: Optional[int]
            stopped_at = parse_stop_time(instance.get("StateTransitionReason"))
            if stopped_at is not None:
                stopped_days = (now - stopped_at).days
                if stopped_days < self.threshold_days:
                    continue
            else:
                # Stop time unknown (e.g. LocalStack omits it) — flag anyway;
                # the human approval gate is the safety net.
                stopped_days = None

            # Placeholder: a stopped instance still bills for its EBS root
            # volume and any attached EIP; a proper per-resource estimate
            # arrives with the pricing table work in Phase 4.
            savings = Decimal("5.00")

            finding = Finding(
                id=f"ec2_stopped|{resource.resource_id}",
                resource_ref=resource.id,
                rule="ec2_stopped",
                evidence={
                    **instance,
                    "stopped_days": stopped_days,
                    "threshold_days": self.threshold_days,
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
