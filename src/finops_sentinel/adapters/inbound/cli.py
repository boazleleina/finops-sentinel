from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from finops_sentinel.bootstrap import (
    get_cloud_gateway,
    get_notifier,
    get_repository,
    get_scanners,
)
from finops_sentinel.domain.services import expire_stale, notify_open_findings, run_scan

app = typer.Typer(help="FinOps Sentinel - AWS Cost Optimization Agent")
console = Console()


@app.command()
def scan() -> None:
    """
    Run a full scan across all AWS resources to find cost optimization
    opportunities, then notify any new (OPEN, non-protected) findings.
    """
    console.print("[bold green]Starting FinOps Sentinel Scan...[/bold green]")

    gateway = get_cloud_gateway()
    repo = get_repository()
    scanners = get_scanners()
    notifier = get_notifier()

    console.print(f"Loaded [bold cyan]{len(scanners)}[/bold cyan] scanners.")

    with console.status("[bold yellow]Scanning AWS environment and evaluating rules...[/bold yellow]"):
        start_time = datetime.now(UTC)
        findings = run_scan(gateway, repo, scanners)
        duration = (datetime.now(UTC) - start_time).total_seconds()

    inventory = repo.get_all_resources()

    console.print(f"\nScan completed in [bold]{duration:.2f}s[/bold]")
    console.print(f"Inventory Discovered: [bold cyan]{len(inventory)}[/bold cyan] resources")

    if not findings:
        console.print(
            "[bold green]No cost optimization opportunities found! "
            "Your environment is perfectly clean.[/bold green]"
        )
        return

    notified = notify_open_findings(repo, notifier)

    console.print(f"Findings Generated: [bold red]{len(findings)}[/bold red] violations")
    console.print(
        f"Notifications Sent: [bold cyan]{len(notified)}[/bold cyan] "
        f"(via {notifier.channel_name})\n"
    )

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
        resource = next((r for r in inventory if r.id == f.resource_ref), None)
        res_type = str(resource.resource_type) if resource else "Unknown"
        res_id = resource.resource_id if resource else f.resource_ref

        current = repo.get_finding_by_id(f.id)
        status = current.status if current else f.status

        table.add_row(
            res_id,
            res_type,
            f.rule,
            f"${f.est_monthly_cost_usd:.2f}",
            protected_str,
            status.upper(),
        )
        if not f.protected:
            total_savings += float(f.est_monthly_cost_usd)

    console.print(table)
    console.print(
        f"\n[bold]Total Potential Monthly Savings: [green]${total_savings:.2f}[/green][/bold]"
    )


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """
    Start the FastAPI server (inbound API + notifier callbacks).
    """
    import uvicorn

    console.print(f"[bold green]Starting API server on {host}:{port}...[/bold green]")
    uvicorn.run("finops_sentinel.adapters.inbound.fastapi_app:app", host=host, port=port)


@app.command()
def expire() -> None:
    """
    Expire NOTIFIED findings older than 72 hours.
    """
    expired = expire_stale(get_repository())
    if expired:
        console.print(f"[bold yellow]Expired {len(expired)} stale finding(s):[/bold yellow]")
        for finding_id in expired:
            console.print(f"  - {finding_id}")
    else:
        console.print("[bold green]No stale findings to expire.[/bold green]")


if __name__ == "__main__":
    app()
