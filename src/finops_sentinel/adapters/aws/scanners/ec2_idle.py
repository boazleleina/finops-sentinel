"""Idle running-EC2 scanner (metric-inferred, notify-only).

Unlike the state-based rules, idleness is an inference from CloudWatch
averages, so this rule is listed in domain.rules.NOTIFY_ONLY_RULES and can
never be auto-remediated: a quiet instance may be a warm standby, a batch
worker between runs, or a license server.
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


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class IdleEC2Scanner(Scanner):
    """Flags running instances whose CPU and network have both flatlined."""

    def __init__(
        self,
        region: str,
        pricing: Pricing,
        observation_days: int = 14,
        cpu_threshold_percent: float = 5.0,
        network_threshold_bytes: float = 1_000_000.0,
        min_datapoints: int = 24,
    ):
        self.region = region
        self.pricing = pricing
        self.observation_days = observation_days
        self.cpu_threshold_percent = cpu_threshold_percent
        self.network_threshold_bytes = network_threshold_bytes
        self.min_datapoints = min_datapoints
        # discover() collects the metric series so evaluate() stays pure;
        # keyed by instance id.
        self._metrics: dict[str, dict[str, list[float]]] = {}

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
        discovered: list[tuple[Resource, dict[str, Any]]] = []
        now = datetime.now(UTC)
        self._metrics = {}

        for instance in gateway.describe_running_ec2_instances():
            instance_id = instance["InstanceId"]
            tags = instance.get("Tags", [])
            tags_dict = {t["Key"]: t["Value"] for t in tags} if isinstance(tags, list) else tags

            self._metrics[instance_id] = {
                metric: gateway.get_metric_averages(
                    namespace="AWS/EC2",
                    dimension_name="InstanceId",
                    dimension_value=instance_id,
                    metric_name=metric,
                    days=self.observation_days,
                )
                for metric in ("CPUUtilization", "NetworkIn", "NetworkOut")
            }

            discovered.append(
                (
                    Resource(
                        id=str(uuid.uuid4()),
                        resource_id=instance_id,
                        resource_type=ResourceType.EC2_INSTANCE,
                        resource_arn=(
                            f"arn:aws:ec2:{self.region}:account:instance/{instance_id}"
                        ),
                        region=self.region,
                        current_tags=tags_dict,
                        lifecycle=ResourceLifecycle.ACTIVE,
                        first_seen_at=now,
                        last_seen_at=now,
                    ),
                    instance,
                )
            )

        return discovered

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings: list[Finding] = []
        now = datetime.now(UTC)

        for resource, instance in resources:
            if resource.resource_type != ResourceType.EC2_INSTANCE:
                continue
            # run_scan hands every scanner the combined inventory, which
            # includes the STOPPED instances found by StoppedEC2Scanner.
            if instance.get("State", {}).get("Name") != "running":
                continue

            metrics = self._metrics.get(resource.resource_id)
            if metrics is None:
                continue

            cpu = metrics["CPUUtilization"]
            if len(cpu) < self.min_datapoints:
                # Too little history to call it idle (freshly launched, or a
                # CloudWatch gap). Silence beats a false positive.
                continue

            avg_cpu = _mean(cpu)
            avg_network = _mean(metrics["NetworkIn"]) + _mean(metrics["NetworkOut"])
            if avg_cpu >= self.cpu_threshold_percent:
                continue
            if avg_network >= self.network_threshold_bytes:
                continue

            findings.append(
                Finding(
                    id=f"ec2_idle|{resource.resource_id}",
                    resource_ref=resource.id,
                    rule="ec2_idle",
                    evidence={
                        "InstanceId": resource.resource_id,
                        "InstanceType": instance.get("InstanceType"),
                        "State": "running",
                        "avg_cpu_percent": round(avg_cpu, 3),
                        "max_cpu_percent": round(max(cpu), 3),
                        "avg_network_bytes": round(avg_network, 1),
                        "observation_days": self.observation_days,
                        "datapoints": len(cpu),
                        "cpu_threshold_percent": self.cpu_threshold_percent,
                        "network_threshold_bytes": self.network_threshold_bytes,
                        "notify_only": True,
                    },
                    tags_at_detection=resource.current_tags,
                    est_monthly_cost_usd=self.pricing.ec2_instance_monthly(
                        instance_type=instance.get("InstanceType", ""),
                        region=resource.region,
                    ),
                    status=FindingStatus.OPEN,
                    protected=tag_is_protected(resource.current_tags),
                    detected_at=now,
                    last_seen_at=now,
                )
            )

        return findings
