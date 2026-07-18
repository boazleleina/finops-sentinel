from typer.testing import CliRunner
from finops_sentinel.adapters.inbound.cli import app
from unittest.mock import patch

runner = CliRunner()

@patch("finops_sentinel.adapters.inbound.cli.run_scan")
@patch("finops_sentinel.adapters.inbound.cli.get_cloud_gateway")
@patch("finops_sentinel.adapters.inbound.cli.get_repository")
@patch("finops_sentinel.adapters.inbound.cli.get_scanners")
def test_scan_command(mock_scanners, mock_repo, mock_gateway, mock_run_scan):
    # Setup mock returns to simulate a scan that found nothing
    mock_run_scan.return_value = []
    mock_repo.return_value.get_all_resources.return_value = []
    
    # Run the command
    result = runner.invoke(app, ["scan"])
    
    assert result.exit_code == 0
    assert "Starting FinOps Sentinel Scan" in result.stdout
    assert "Scan completed in" in result.stdout
