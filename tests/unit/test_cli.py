import json
from unittest.mock import patch

import httpx
import respx
from typer.testing import CliRunner

from finops_sentinel.adapters.advisor.ollama import OllamaAdvisor
from finops_sentinel.adapters.advisor.template import TemplateAdvisor
from finops_sentinel.adapters.inbound.cli import app

runner = CliRunner()


@patch("finops_sentinel.adapters.inbound.cli.run_scan")
@patch("finops_sentinel.adapters.inbound.cli.get_notifier")
@patch("finops_sentinel.adapters.inbound.cli.get_cloud_gateway")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
@patch("finops_sentinel.adapters.inbound.cli.get_scanners")
def test_scan_command(mock_scanners, mock_repo, mock_gateway, mock_notifier, mock_run_scan):
    # Simulate a scan that found nothing
    mock_run_scan.return_value = []
    mock_repo.return_value.get_all_resources.return_value = []

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "Starting FinOps Sentinel Scan" in result.stdout
    assert "Scan completed in" in result.stdout


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
