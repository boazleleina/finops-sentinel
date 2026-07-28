"""RDS scanners: idle-by-connections, stopped-by-state, and the refusal to act.

LocalStack's RDS support is Pro-tier, so these scanners have no end-to-end
integration coverage — moto is the whole safety net here. That gap is
deliberate and recorded; see the skipped placeholder in tests/integration/.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.adapters.aws.scanners.rds import IdleRDSScanner, StoppedRDSScanner
from finops_sentinel.domain.models import FindingStatus, ResourceType
from finops_sentinel.domain.rules import PLAYBOOK_ALLOWLIST, is_remediable
from finops_sentinel.domain.services import approve_finding
from tests.fakes import (
    FakeCloudGateway,
    FakeGatewayBase,
    make_finding,
    make_resource,
    resolver,
)


@pytest.fixture
def rds_env():
    """moto RDS plus a gateway pointed at it."""
    with mock_aws():
        yield boto3.client("rds", region_name="us-east-1")


def _create_db(client, identifier, *, db_class="db.m5.large", engine="postgres",
               storage=100, tags=None):
    client.create_db_instance(
        DBInstanceIdentifier=identifier,
        DBInstanceClass=db_class,
        Engine=engine,
        AllocatedStorage=storage,
        Tags=tags or [],
    )


class FakeRDSGateway(FakeGatewayBase):
    """Canned instances and connection series; every other port refuses."""

    def __init__(self, instances, connections=None):
        self.instances = instances
        self.connections = connections or {}
        self.metric_calls = []

    def describe_rds_instances(self):
        return self.instances

    def get_metric_averages(
        self, namespace, dimensions, metric_name, days, period_seconds=3600
    ):
        self.metric_calls.append((namespace, dimensions, metric_name))
        return self.connections.get(dimensions["DBInstanceIdentifier"], [])


def _instance(identifier, *, status="available", db_class="db.m5.large",
              engine="postgres", storage=100, tags=None, multi_az=False):
    return {
        "DBInstanceIdentifier": identifier,
        "DBInstanceStatus": status,
        "DBInstanceClass": db_class,
        "Engine": engine,
        "AllocatedStorage": storage,
        "StorageType": "gp2",
        "MultiAZ": multi_az,
        "DBInstanceArn": f"arn:aws:rds:us-east-1:123456789012:db:{identifier}",
        "TagList": tags or [],
    }


def _series(value, count=336):
    return [value] * count


# --------------------------------------------------------------------------
# rds_idle
# --------------------------------------------------------------------------


def test_database_with_no_connections_is_flagged():
    gateway = FakeRDSGateway([_instance("db-quiet")], {"db-quiet": _series(0.0)})
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())

    findings = scanner.evaluate(scanner.discover(gateway))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "rds_idle"
    assert finding.id == "rds_idle|db-quiet"
    assert finding.evidence["notify_only"] is True
    assert finding.evidence["datapoints"] == 336
    # Compute plus storage: deleting an idle instance stops both meters.
    # db.m5.large 0.178 * 730 = 129.94, plus 100GB gp2 at 0.115 = 11.50
    assert finding.est_monthly_cost_usd == Decimal("141.44")


def test_database_with_connections_is_not_flagged():
    gateway = FakeRDSGateway([_instance("db-busy")], {"db-busy": _series(4.0)})
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_occasional_connections_still_count_as_use():
    """A nightly batch job connects rarely but the database is not disposable."""
    series = _series(0.0, count=335) + [50.0]
    gateway = FakeRDSGateway([_instance("db-batch")], {"db-batch": series})
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_sparse_metrics_are_not_flagged():
    """A short series means 'unknown' — never guess on a production database."""
    gateway = FakeRDSGateway([_instance("db-new")], {"db-new": _series(0.0, count=3)})
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing(), min_datapoints=24)

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_missing_metrics_are_not_flagged():
    """CloudWatch outage returns nothing; that is not evidence of idleness."""
    gateway = FakeRDSGateway([_instance("db-blind")], {})
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_stopped_instances_are_never_called_idle():
    """A stopped database reports no connections, which must not read as idle."""
    gateway = FakeRDSGateway([_instance("db-off", status="stopped")])
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())

    resources = scanner.discover(gateway)

    # Still inventoried — just not judged.
    assert len(resources) == 1
    assert scanner.evaluate(resources) == []
    # And no CloudWatch call was wasted on it.
    assert gateway.metric_calls == []


def test_transient_states_are_inventoried_but_not_judged():
    """An instance mid-backup must not vanish and get swept up as DELETED."""
    gateway = FakeRDSGateway([_instance("db-backup", status="backing-up")])
    idle = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())
    stopped = StoppedRDSScanner(region="us-east-1", pricing=StaticPricing())

    resources = idle.discover(gateway)
    assert [r.resource_id for r, _ in resources] == ["db-backup"]
    assert idle.evaluate(resources) == []
    assert stopped.evaluate(resources) == []


def test_idle_scanner_reads_the_rds_namespace():
    """RDS metrics live under AWS/RDS keyed by DBInstanceIdentifier."""
    gateway = FakeRDSGateway([_instance("db-quiet")], {"db-quiet": _series(0.0)})
    IdleRDSScanner(region="us-east-1", pricing=StaticPricing()).discover(gateway)

    assert gateway.metric_calls == [
        ("AWS/RDS", {"DBInstanceIdentifier": "db-quiet"}, "DatabaseConnections")
    ]


def test_protected_tag_is_carried_onto_the_finding():
    gateway = FakeRDSGateway(
        [_instance("db-prot", tags=[{"Key": "finops:protected", "Value": "true"}])],
        {"db-prot": _series(0.0)},
    )
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())

    findings = scanner.evaluate(scanner.discover(gateway))

    assert len(findings) == 1
    assert findings[0].protected is True


def _idle_finding(instance):
    """Run one instance through a fresh scanner and return its finding."""
    gateway = FakeRDSGateway(
        [instance], {instance["DBInstanceIdentifier"]: _series(0.0)}
    )
    scanner = IdleRDSScanner(region="us-east-1", pricing=StaticPricing())
    findings = scanner.evaluate(scanner.discover(gateway))
    assert len(findings) == 1
    return findings[0]


def test_commercial_engine_is_not_priced_as_postgres():
    """Licence cost dwarfs hardware cost, so the engine cannot be ignored.

    Same instance class, same storage — pricing them identically would bury
    the most expensive finding in the report under cheaper ones.
    """
    postgres = _idle_finding(_instance("db-pg", engine="postgres"))
    sqlserver = _idle_finding(_instance("db-ms", engine="sqlserver-se"))

    assert sqlserver.est_monthly_cost_usd > postgres.est_monthly_cost_usd * 3


def test_unknown_instance_class_still_gets_a_nonzero_cost():
    """Zero would read as "free" and hide the finding in the savings total."""
    finding = _idle_finding(_instance("db-weird", db_class="db.zz.42xlarge"))

    assert finding.est_monthly_cost_usd > Decimal(0)


# --------------------------------------------------------------------------
# rds_stopped
# --------------------------------------------------------------------------


def test_stopped_database_is_flagged_immediately():
    """No grace period: the API exposes no stopped-since timestamp, and AWS
    restarts the instance after 7 days regardless."""
    gateway = FakeRDSGateway([_instance("db-off", status="stopped")])
    scanner = StoppedRDSScanner(region="us-east-1", pricing=StaticPricing())

    findings = scanner.evaluate(scanner.discover(gateway))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "rds_stopped"
    assert finding.evidence["auto_restarts_after_days"] == 7
    assert finding.evidence["compute_billed_while_stopped"] is False
    # Storage only — 100GB gp2 at 0.115. Claiming compute would overstate it.
    assert finding.est_monthly_cost_usd == Decimal("11.50")


def test_available_database_is_not_flagged_as_stopped():
    gateway = FakeRDSGateway([_instance("db-live")])
    scanner = StoppedRDSScanner(region="us-east-1", pricing=StaticPricing())

    assert scanner.evaluate(scanner.discover(gateway)) == []


def test_stopped_scanner_makes_no_metric_calls():
    """State-based rule: it never needs CloudWatch."""
    gateway = FakeRDSGateway([_instance("db-off", status="stopped")])
    StoppedRDSScanner(region="us-east-1", pricing=StaticPricing()).discover(gateway)

    assert gateway.metric_calls == []


# --------------------------------------------------------------------------
# The guardrail: RDS findings are never actionable
# --------------------------------------------------------------------------


def test_rds_rules_are_notify_only():
    assert is_remediable("rds_idle") is False
    assert is_remediable("rds_stopped") is False


def test_rds_has_no_playbook_at_all():
    """Second, independent gate: even a remediable rule could not act on RDS."""
    assert ResourceType.RDS_INSTANCE not in PLAYBOOK_ALLOWLIST


@pytest.mark.parametrize("rule", ["rds_idle", "rds_stopped"])
def test_approving_an_rds_finding_is_refused(repository, rule):
    """The whole point of the phase: this system does not delete databases."""
    repository.upsert_resource(
        make_resource(resource_id="db-quiet", resource_type=ResourceType.RDS_INSTANCE)
    )
    repository.save_finding(make_finding(rule=rule))
    gateway = FakeCloudGateway()

    approved = approve_finding(
        "f-mock", repository, resolver(gateway), actor="boaz", channel="slack",
        dry_run=False,
    )

    assert approved is False
    assert gateway.executed == []
    assert repository.get_finding_by_id("f-mock").status == FindingStatus.NOTIFIED
    assert any(
        e.event == "approve_blocked_notify_only"
        for e in repository.get_audit_events("f-mock")
    )


# --------------------------------------------------------------------------
# The real adapter against moto
# --------------------------------------------------------------------------


def test_gateway_describes_instances_in_every_state(rds_env):
    """DescribeDBInstances has no status filter, so the adapter returns all."""
    _create_db(rds_env, "db-live")
    _create_db(rds_env, "db-off")
    rds_env.stop_db_instance(DBInstanceIdentifier="db-off")

    instances = Boto3Gateway(region="us-east-1").describe_rds_instances()
    by_id = {i["DBInstanceIdentifier"]: i["DBInstanceStatus"] for i in instances}

    assert by_id == {"db-live": "available", "db-off": "stopped"}


def test_end_to_end_stopped_scan_against_moto(rds_env):
    _create_db(rds_env, "db-off", storage=50, tags=[{"Key": "env", "Value": "dev"}])
    rds_env.stop_db_instance(DBInstanceIdentifier="db-off")

    gateway = Boto3Gateway(region="us-east-1")
    scanner = StoppedRDSScanner(region="us-east-1", pricing=StaticPricing())
    resources = scanner.discover(gateway)
    findings = scanner.evaluate(resources)

    assert len(findings) == 1
    assert findings[0].evidence["DBInstanceIdentifier"] == "db-off"
    assert findings[0].tags_at_detection == {"env": "dev"}
    assert resources[0][0].resource_type == ResourceType.RDS_INSTANCE
    assert resources[0][0].resource_arn.startswith("arn:aws:rds:us-east-1:")


def test_end_to_end_idle_scan_against_moto(rds_env):
    """Real gateway, real CloudWatch series, real scanner."""
    _create_db(rds_env, "db-quiet")
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    now = datetime.now(UTC)
    # Backdated by an hour each: CloudWatch buckets by period, and the bucket
    # covering "right now" is still open, so a series stamped entirely at now
    # reads back as empty.
    for hour in range(1, 31):
        cloudwatch.put_metric_data(
            Namespace="AWS/RDS",
            MetricData=[{
                "MetricName": "DatabaseConnections",
                "Dimensions": [
                    {"Name": "DBInstanceIdentifier", "Value": "db-quiet"}
                ],
                "Timestamp": now - timedelta(hours=hour),
                "Value": 0.0,
            }],
        )

    gateway = Boto3Gateway(region="us-east-1")
    scanner = IdleRDSScanner(
        region="us-east-1", pricing=StaticPricing(), min_datapoints=1
    )
    findings = scanner.evaluate(scanner.discover(gateway))

    assert len(findings) == 1
    assert findings[0].rule == "rds_idle"
    assert findings[0].evidence["avg_connections"] == 0.0
