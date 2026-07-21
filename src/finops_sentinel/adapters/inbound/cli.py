import typer
from rich.console import Console
from rich.table import Table
from datetime import datetime, UTC

from finops_sentinel.bootstrap import get_cloud_gateway, get_repository, get_scanners
from finops_sentinel.domain.services import run_scan
from finops_sentinel.domain.models import FindingStatus
from finops_sentinel.adapters.notifications.slack import SlackAdapter

app = typer.Typer(help="FinOps Sentinel - AWS Cost Optimization Agent")
console = Console()

@app.command()
def scan():
    """
    Run a full scan across all AWS resources to find cost optimization opportunities.
    """
    console.print("[bold green]Starting FinOps Sentinel Scan...[/bold green]")
    
    gateway = get_cloud_gateway()
    repo = get_repository()
    scanners = get_scanners()
    
    console.print(f"Loaded [bold cyan]{len(scanners)}[/bold cyan] scanners.")
    
    with console.status("[bold yellow]Scanning AWS environment and evaluating rules...[/bold yellow]"):
        start_time = datetime.now(UTC)
        findings = run_scan(gateway, repo, scanners)
        duration = (datetime.now(UTC) - start_time).total_seconds()
    
    # Also fetch all resources to show inventory count
    inventory = repo.get_all_resources()
    
    console.print(f"\nScan completed in [bold]{duration:.2f}s[/bold]")
    console.print(f"Inventory Discovered: [bold cyan]{len(inventory)}[/bold cyan] resources")
    
    if not findings:
        console.print("[bold green]No cost optimization opportunities found! Your environment is perfectly clean.[/bold green]")
        return
        
    console.print(f"Findings Generated: [bold red]{len(findings)}[/bold red] violations\n")
    
    # Print the Rich Table
    table = Table(title="Optimization Opportunities")
    table.add_column("Resource ID", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Rule", style="blue")
    table.add_column("Savings ($/mo)", justify="right", style="green")
    table.add_column("Protected", justify="center")
    table.add_column("Status")
    
    total_savings = 0.0
    for f in findings:
        protected_str = "[bold green]Yes[/bold green]" if f.protected else "No"
        # We need the resource type, so let's find the matching resource from the inventory
        resource = next((r for r in inventory if r.id == f.resource_ref), None)
        res_type = resource.resource_type if resource else "Unknown"
        res_id = resource.resource_id if resource else f.resource_ref
        
        table.add_row(
            res_id,
            res_type,
            f.rule,
            f"${f.est_monthly_cost_usd:.2f}",
            protected_str,
            f.status.upper()
        )
        if not f.protected:
            total_savings += float(f.est_monthly_cost_usd)
            
            # Phase 2: Send Slack Alert and transition to NOTIFIED
            if f.status == FindingStatus.OPEN:
                notifier = SlackAdapter()
                notifier.send_finding_alert(f, resource)
                f.status = FindingStatus.NOTIFIED
                repo.save_finding(f)
            
    console.print(table)
    console.print(f"\n[bold]Total Potential Monthly Savings: [green]${total_savings:.2f}[/green][/bold]")

@app.command()
def serve():
    """
    Start the FastAPI server (coming in Phase 2).
    """
    console.print("[bold yellow]API Server is coming in Phase 2![/bold yellow]")

if __name__ == "__main__":
    app()
