import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import httpx
import respx
from typer.testing import CliRunner

from finops_sentinel.adapters.advisor.ollama import OllamaAdvisor
from finops_sentinel.adapters.advisor.template import TemplateAdvisor
from finops_sentinel.adapters.inbound.cli import _one_line, app


def test_one_line_collapses_and_truncates_provider_prose():
    """One dead service yields the same essay per scanner per region.

    Printed in full that is six paragraphs of AWS prose for a single cause,
    which buries the findings the scan actually ran for.
    """
    sprawling = (
        "ClientError: An error occurred (InternalFailure) when calling the\n"
        "DescribeDBInstances operation: Sorry, the rds service is not included "
        "within your LocalStack license, but is available in an upgraded "
        "license. Please refer to https://docs.localstack.cloud/references/"
        "coverage for more details."
    )

    result = _one_line(sprawling)

    assert "\n" not in result
    assert len(result) <= 140
    assert result.startswith("ClientError: An error occurred (InternalFailure)")
    assert result.endswith("…")


def test_one_line_leaves_short_messages_alone():
    assert _one_line("AccessDenied: rds:DescribeDBInstances") == (
        "AccessDenied: rds:DescribeDBInstances"
    )
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
    RightsizingCandidate,
    RightsizingSuggestion,
    SpendAnomaly,
)
from finops_sentinel.domain.services import (
    RightsizingReport,
    ScanResult,
    ScanTarget,
)

runner = CliRunner()


@patch("finops_sentinel.adapters.inbound.cli.run_scan")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
@patch("finops_sentinel.adapters.inbound.cli.get_scan_targets")
def test_scan_command(mock_targets, mock_repo, mock_notifier, mock_run_scan):
    # Simulate a scan that found nothing
    mock_targets.return_value = [ScanTarget("us-east-1", object(), [])]
    mock_run_scan.return_value = ScanResult(
        findings=[], regions_scanned=["us-east-1"], regions_failed={}
    )
    mock_repo.return_value.get_all_resources.return_value = []

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "Starting FinOps Sentinel Scan" in result.stdout
    assert "Scan completed in" in result.stdout
    assert "perfectly clean" in result.stdout


@patch("finops_sentinel.adapters.inbound.cli.run_scan")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
@patch("finops_sentinel.adapters.inbound.cli.get_scan_targets")
def test_scan_reports_failed_regions_instead_of_a_clean_bill(
    mock_targets, mock_repo, mock_notifier, mock_run_scan
):
    """A partial scan must never print "perfectly clean" — that is the one
    output an operator would act on by doing nothing."""
    mock_targets.return_value = [
        ScanTarget("us-east-1", object(), []),
        ScanTarget("eu-west-1", object(), []),
    ]
    mock_run_scan.return_value = ScanResult(
        findings=[],
        regions_scanned=["us-east-1"],
        regions_failed={"eu-west-1": "ClientError: AuthFailure"},
    )
    mock_repo.return_value.get_all_resources.return_value = []

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "eu-west-1" in result.stdout
    assert "AuthFailure" in result.stdout
    assert "perfectly clean" not in result.stdout


@patch("finops_sentinel.adapters.inbound.cli.notify_open_findings")
@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
@patch("finops_sentinel.adapters.inbound.cli.run_scan")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
@patch("finops_sentinel.adapters.inbound.cli.get_scan_targets")
def test_scan_output_attributes_findings_and_savings_to_their_region(
    mock_targets, mock_repo, mock_notifier, mock_run_scan, mock_advisor, mock_notify
):
    """Which region the money is in is the first question a multi-region
    operator asks."""
    now = datetime.now(UTC)

    def resource(res_id, resource_id, region):
        return Resource(
            id=res_id,
            resource_id=resource_id,
            resource_type=ResourceType.EBS_VOLUME,
            resource_arn=f"arn:aws:ec2:{region}:account:volume/{resource_id}",
            region=region,
            current_tags={},
            lifecycle=ResourceLifecycle.ACTIVE,
            first_seen_at=now,
            last_seen_at=now,
        )

    def finding(finding_id, res_id, cost):
        return Finding(
            id=finding_id,
            resource_ref=res_id,
            rule="ebs_unattached",
            evidence={},
            tags_at_detection={},
            est_monthly_cost_usd=Decimal(cost),
            status=FindingStatus.OPEN,
            protected=False,
            detected_at=now,
            last_seen_at=now,
        )

    findings = [
        finding("f-east", "res-east", "8.00"),
        finding("f-west", "res-west", "42.00"),
    ]
    mock_targets.return_value = [
        ScanTarget("us-east-1", object(), []),
        ScanTarget("eu-west-1", object(), []),
    ]
    mock_run_scan.return_value = ScanResult(
        findings=findings,
        regions_scanned=["eu-west-1", "us-east-1"],
        regions_failed={},
    )
    mock_repo.return_value.get_all_resources.return_value = [
        resource("res-east", "vol-east", "us-east-1"),
        resource("res-west", "vol-west", "eu-west-1"),
    ]
    mock_repo.return_value.get_finding_by_id.side_effect = lambda fid: next(
        f for f in findings if f.id == fid
    )
    mock_notify.return_value = findings

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "Savings by Region" in result.stdout
    assert "eu-west-1" in result.stdout
    assert "$42.00" in result.stdout
    assert "$50.00" in result.stdout  # total across both regions


@patch("finops_sentinel.adapters.inbound.cli.get_regions")
def test_regions_command_lists_what_will_be_scanned(mock_regions):
    mock_regions.return_value = ["us-east-1", "eu-west-1", "ap-southeast-2"]

    result = runner.invoke(app, ["regions"])

    assert result.exit_code == 0
    assert "3 region(s)" in result.stdout
    for region in mock_regions.return_value:
        assert region in result.stdout


@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
def test_smoke_llm_reports_healthy_advisor(mock_get_advisor):
    advisor = OllamaAdvisor(base_url="http://ollama.test", model="qwen3:8b")
    mock_get_advisor.return_value = advisor

    with respx.mock:
        respx.post("http://ollama.test/api/chat").mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "Idle for 14 days.",
                                "risk": "medium",
                                "recommended_action": "Confirm, then stop it.",
                            }
                        )
                    }
                },
            )
        )
        result = runner.invoke(app, ["smoke-llm", "--iterations", "3"])

    assert result.exit_code == 0
    assert "Schema-valid: 3/3" in result.stdout
    assert "Advisor healthy" in result.stdout


@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
def test_smoke_llm_exits_nonzero_when_backend_is_down(mock_get_advisor):
    """The gate must fail loudly — summarize() would hide this behind a template."""
    mock_get_advisor.return_value = OllamaAdvisor(
        base_url="http://ollama.test", model="qwen3:8b"
    )

    with respx.mock:
        respx.post("http://ollama.test/api/chat").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = runner.invoke(app, ["smoke-llm", "--iterations", "2"])

    assert result.exit_code == 1
    assert "Schema-valid: 0/2" in result.stdout
    assert "ConnectError" in result.stdout
    # Tells the operator the pipeline survives regardless.
    assert "falls back to" in result.stdout


@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
def test_smoke_llm_exits_nonzero_for_backendless_provider(mock_get_advisor):
    mock_get_advisor.return_value = TemplateAdvisor()

    result = runner.invoke(app, ["smoke-llm"])

    assert result.exit_code == 1
    assert "no backend to smoke-test" in result.stdout


def test_smoke_llm_model_flag_overrides_config_without_touching_env():
    """Qualifying a new model must not require editing .env."""
    with respx.mock:
        route = respx.post("http://alt-host:11434/api/chat").mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "Idle.",
                                "risk": "low",
                                "recommended_action": "Stop it.",
                            }
                        )
                    }
                },
            )
        )
        result = runner.invoke(
            app,
            ["smoke-llm", "--iterations", "1", "--model", "llama3:70b",
             "--base-url", "http://alt-host:11434"],
        )

    assert result.exit_code == 0
    assert json.loads(route.calls[0].request.content)["model"] == "llama3:70b"
    assert "llama3:70b" in result.stdout


@patch("finops_sentinel.adapters.inbound.cli.expire_stale")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
def test_expire_command(mock_repo, mock_expire):
    mock_expire.return_value = ["f-old"]

    result = runner.invoke(app, ["expire"])

    assert result.exit_code == 0
    assert "Expired 1 stale finding" in result.stdout
    assert "f-old" in result.stdout


# --------------------------------------------------------------------------
# sentinel digest — advisory, button-free, and honest about what it measures
# --------------------------------------------------------------------------


def _cli_suggestion():
    return RightsizingSuggestion(
        resource_id="i-0abc",
        region="us-east-1",
        current_instance_type="m5.xlarge",
        current_monthly_cost_usd=Decimal("140.16"),
        candidate=RightsizingCandidate(
            instance_type="m6g.large",
            monthly_cost_usd=Decimal("56.21"),
            monthly_saving_usd=Decimal("83.95"),
        ),
        max_cpu_percent=3.2,
        avg_cpu_percent=2.0,
        datapoints=336,
        observation_days=14,
    )


def _cli_anomaly():
    return SpendAnomaly(
        date=date(2026, 7, 27),
        value=Decimal("412.50"),
        mean=Decimal("180.00"),
        stdev=Decimal("40.00"),
        z_score=5.81,
        direction="increase",
        window_days=14,
    )


@patch("finops_sentinel.adapters.inbound.cli.send_digest")
@patch("finops_sentinel.adapters.inbound.cli.build_rightsizing_digest")
@patch("finops_sentinel.adapters.inbound.cli.detect_spend_anomaly")
@patch("finops_sentinel.adapters.inbound.cli.get_digest_targets")
@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
def test_digest_command_renders_suggestions_and_posts(
    mock_repo, mock_notifier, mock_advisor, mock_targets, mock_anomaly, mock_build, mock_send
):
    mock_targets.return_value = []
    mock_anomaly.return_value = _cli_anomaly()
    mock_build.return_value = RightsizingReport([_cli_suggestion()], {}, 4)

    result = runner.invoke(app, ["digest"])

    assert result.exit_code == 0
    assert "i-0abc" in result.stdout
    assert "m6g.large" in result.stdout
    assert "$83.95" in result.stdout
    # The honesty line: this is not billed spend, and the CLI says so too.
    assert "estimated monthly waste" in result.stdout
    assert "Spend anomaly on 2026-07-27" in result.stdout
    mock_send.assert_called_once()


@patch("finops_sentinel.adapters.inbound.cli.send_digest")
@patch("finops_sentinel.adapters.inbound.cli.build_rightsizing_digest")
@patch("finops_sentinel.adapters.inbound.cli.detect_spend_anomaly")
@patch("finops_sentinel.adapters.inbound.cli.get_digest_targets")
@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
def test_digest_no_send_posts_nothing(
    mock_repo, mock_notifier, mock_advisor, mock_targets, mock_anomaly, mock_build, mock_send
):
    mock_targets.return_value = []
    mock_anomaly.return_value = None
    mock_build.return_value = RightsizingReport([_cli_suggestion()], {}, 4)

    result = runner.invoke(app, ["digest", "--no-send"])

    assert result.exit_code == 0
    assert "nothing was posted" in result.stdout
    mock_send.assert_not_called()


@patch("finops_sentinel.adapters.inbound.cli.send_digest")
@patch("finops_sentinel.adapters.inbound.cli.build_rightsizing_digest")
@patch("finops_sentinel.adapters.inbound.cli.detect_spend_anomaly")
@patch("finops_sentinel.adapters.inbound.cli.get_digest_targets")
@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
def test_digest_with_nothing_to_report_explains_the_silence(
    mock_repo, mock_notifier, mock_advisor, mock_targets, mock_anomaly, mock_build, mock_send
):
    """"No anomaly" and "not enough history to have one" are different
    statements, and only one of them is reassuring."""
    mock_targets.return_value = []
    mock_anomaly.return_value = None
    mock_build.return_value = RightsizingReport([], {}, 4)

    result = runner.invoke(app, ["digest"])

    assert result.exit_code == 0
    assert "No over-provisioned instances found" in result.stdout
    assert "too little history to judge" in result.stdout
    mock_send.assert_called_once()


@patch("finops_sentinel.adapters.inbound.cli.send_digest")
@patch("finops_sentinel.adapters.inbound.cli.build_rightsizing_digest")
@patch("finops_sentinel.adapters.inbound.cli.detect_spend_anomaly")
@patch("finops_sentinel.adapters.inbound.cli.get_digest_targets")
@patch("finops_sentinel.adapters.inbound.cli.get_advisor")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
def test_digest_never_reports_a_clean_result_over_failed_regions(
    mock_repo, mock_notifier, mock_advisor, mock_targets, mock_anomaly, mock_build, mock_send
):
    """Same guarantee `sentinel scan` makes: an empty result over regions that
    never answered must not print as a clean bill of health."""
    mock_targets.return_value = []
    mock_anomaly.return_value = None
    mock_build.return_value = RightsizingReport(
        [], {"eu-west-1": "EndpointConnectionError: Could not connect"}, 0
    )

    result = runner.invoke(app, ["digest"])

    assert result.exit_code == 0
    assert "could not be checked" in result.stdout
    assert "eu-west-1" in result.stdout
    assert "No over-provisioned instances found" not in result.stdout
