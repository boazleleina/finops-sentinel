from typer.testing import CliRunner
from finops_sentinel.adapters.inbound.cli import app
from unittest.mock import patch

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


@patch("finops_sentinel.adapters.inbound.cli.expire_stale")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
def test_expire_command(mock_repo, mock_expire):
    mock_expire.return_value = ["f-old"]

    result = runner.invoke(app, ["expire"])

    assert result.exit_code == 0
    assert "Expired 1 stale finding" in result.stdout
    assert "f-old" in result.stdout
