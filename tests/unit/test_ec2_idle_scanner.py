"""IdleEC2Scanner: metric-inferred idleness, and the false-positive guards."""
from datetime import UTC, datetime
from decimal import Decimal

from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.adapters.aws.scanners.ec2_idle import IdleEC2Scanner
from finops_sentinel.domain.models import Resource, ResourceLifecycle, ResourceType
from tests.conftest import FakeGatewayBase


class FakeMetricsGateway(FakeGatewayBase):
    """Serves canned instances and metric series; unused ports raise."""

    def __init__(self, instances, metrics):
        self.instances = instances
        self.metrics = metrics
        self.metric_calls: list[tuple[str, str, str, int]] = []

    def describe_running_ec2_instances(self):
        return self.instances

    def get_metric_averages(
        self,
        namespace,
        dimension_name,
        dimension_value,
        metric_name,
        days,
        period_seconds=3600,
    ):
        self.metric_calls.append((namespace, dimension_value, metric_name, days))
        return self.metrics.get(dimension_value, {}).get(metric_name, [])


def _instance(instance_id, instance_type="m5.large", tags=None):
    return {
        "InstanceId": instance_id,
        "InstanceType": instance_type,
        "State": {"Name": "running"},
        "Tags": tags or [],
    }


def _series(value, count=336):
    return [value] * count


def test_idle_instance_is_flagged():
    instances = [_instance("i-idle")]
    metrics = {
        "i-idle": {
            "CPUUtilization": _series(0.4),
            "NetworkIn": _series(500.0),
            "NetworkOut": _series(400.0),
        }
    }
    gateway = FakeMetricsGateway(instances, metrics)
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing(), observation_days=14)

    resources = scanner.discover(gateway)
    assert len(resources) == 1

    findings = scanner.evaluate(resources)
    assert len(findings) == 1

    finding = findings[0]
    assert finding.rule == "ec2_idle"
    assert finding.id == "ec2_idle|i-idle"
    assert finding.evidence["notify_only"] is True
    assert finding.evidence["datapoints"] == 336
    # m5.large: 0.096 * 730 = 70.08
    assert finding.est_monthly_cost_usd == Decimal("70.08")


def test_busy_cpu_is_not_flagged():
    metrics = {
        "i-busy": {
            "CPUUtilization": _series(42.0),
            "NetworkIn": _series(10.0),
            "NetworkOut": _series(10.0),
        }
    }
    gateway = FakeMetricsGateway([_instance("i-busy")], metrics)
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_chatty_network_is_not_flagged():
    """Low CPU but heavy traffic — a proxy or NAT box, not idle."""
    metrics = {
        "i-proxy": {
            "CPUUtilization": _series(1.0),
            "NetworkIn": _series(5_000_000.0),
            "NetworkOut": _series(5_000_000.0),
        }
    }
    gateway = FakeMetricsGateway([_instance("i-proxy")], metrics)
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_sparse_metrics_are_not_flagged():
    """A short series means 'unknown', not 'idle' — e.g. just-launched hosts."""
    metrics = {
        "i-new": {
            "CPUUtilization": _series(0.0, count=3),
            "NetworkIn": _series(0.0, count=3),
            "NetworkOut": _series(0.0, count=3),
        }
    }
    gateway = FakeMetricsGateway([_instance("i-new")], metrics)
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing(), min_datapoints=24)

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_missing_metrics_are_not_flagged():
    """CloudWatch outage returns empty series; must not manufacture findings."""
    gateway = FakeMetricsGateway([_instance("i-blind")], {})
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_stopped_instances_from_other_scanners_are_ignored():
    """run_scan passes the combined inventory to every scanner's evaluate()."""
    gateway = FakeMetricsGateway([], {})
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing())
    scanner.discover(gateway)

    now = datetime.now(UTC)
    stopped = (
        Resource(
            id="res-stopped",
            resource_id="i-stopped",
            resource_type=ResourceType.EC2_INSTANCE,
            resource_arn="arn",
            region="us-east-1",
            current_tags={},
            lifecycle=ResourceLifecycle.ACTIVE,
            first_seen_at=now,
            last_seen_at=now,
        ),
        {"InstanceId": "i-stopped", "State": {"Name": "stopped"}},
    )

    assert scanner.evaluate([stopped]) == []


def test_protected_tag_is_carried_onto_the_finding():
    metrics = {
        "i-prot": {
            "CPUUtilization": _series(0.1),
            "NetworkIn": _series(1.0),
            "NetworkOut": _series(1.0),
        }
    }
    gateway = FakeMetricsGateway(
        [_instance("i-prot", tags=[{"Key": "finops:protected", "Value": "true"}])], metrics
    )
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing())

    findings = scanner.evaluate(scanner.discover(gateway))
    assert len(findings) == 1
    assert findings[0].protected is True


def test_unknown_instance_type_still_gets_a_nonzero_cost():
    """Zero would read as "free" and hide the finding in the savings total."""
    metrics = {
        "i-weird": {
            "CPUUtilization": _series(0.2),
            "NetworkIn": _series(1.0),
            "NetworkOut": _series(1.0),
        }
    }
    gateway = FakeMetricsGateway([_instance("i-weird", instance_type="zz.42xlarge")], metrics)
    scanner = IdleEC2Scanner(region="us-east-1", pricing=StaticPricing())

    findings = scanner.evaluate(scanner.discover(gateway))
    assert len(findings) == 1
    assert findings[0].est_monthly_cost_usd > Decimal(0)
