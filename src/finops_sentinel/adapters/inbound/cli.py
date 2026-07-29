import time
from datetime import UTC, datetime
from decimal import Decimal

import typer
from rich.console import Console
from rich.table import Table

from finops_sentinel.bootstrap import (
    get_advisor,
    get_digest_targets,
    get_notifier,
    get_pricing,
    get_regions,
    get_repository,
    get_scan_targets,
)
from finops_sentinel.config import settings
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.services import (
    build_rightsizing_digest,
    detect_spend_anomaly,
    expire_stale,
    notify_open_findings,
    run_scan,
    send_digest,
)

app = typer.Typer(help="FinOps Sentinel - AWS Cost Optimization Agent")
console = Console()

# Provider errors arrive as multi-sentence prose with documentation URLs. The
# summary needs the cause, not the essay.
MAX_ERROR_CHARS = 140


def _one_line(error: str, limit: int = MAX_ERROR_CHARS) -> str:
    """Collapse an exception message to a single readable line."""
    collapsed = " ".join(error.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


@app.command()
def scan() -> None:
    """
    Run a full scan across all AWS resources to find cost optimization
    opportunities, then notify any new (OPEN, non-protected) findings.
    """
    console.print("[bold green]Starting FinOps Sentinel Scan...[/bold green]")

    repo = get_repository()
    targets = get_scan_targets()
    notifier = get_notifier()

    # Print the database up front: findings live here, and the API/Slack
    # callback server must read this SAME path or every Approve click fails
    # with "cannot be approved" because the lookup misses.
    console.print(
        f"Regions: [bold cyan]{', '.join(t.region for t in targets)}[/bold cyan] "
        f"({len(targets)})"
    )
    console.print(f"Loaded [bold cyan]{len(targets[0].scanners)}[/bold cyan] scanners per region.")
    console.print(f"Findings database: [bold]{settings.sentinel_db_path}[/bold]")

    with console.status("[bold yellow]Scanning AWS environment and evaluating rules...[/bold yellow]"):
        start_time = datetime.now(UTC)
        result = run_scan(targets, repo, max_workers=settings.scan_max_workers)
        duration = (datetime.now(UTC) - start_time).total_seconds()

    findings = result.findings
    inventory = repo.get_all_resources()

    console.print(f"\nScan completed in [bold]{duration:.2f}s[/bold]")
    # The scan's own count, not the repository's row count — the latter also
    # holds every resource ever seen and since deleted, which made a 30-resource
    # environment report hundreds.
    console.print(
        f"Inventory Discovered: [bold cyan]{result.resources_discovered}[/bold cyan] resources"
    )

    if result.regions_failed:
        # Loud on purpose: without this, a partial scan is indistinguishable
        # from a clean account.
        console.print(
            f"\n[bold red]{len(result.regions_failed)} region(s) failed to scan — "
            "results below are incomplete:[/bold red]"
        )
        for region, error in sorted(result.regions_failed.items()):
            console.print(f"  [red]✗[/red] {region}: {error}")
        console.print()

    if result.scanners_failed:
        # Same reasoning one level down: these regions scanned, but with a blind
        # spot. Saying so beats reporting "no RDS waste" for an account whose
        # RDS calls never went through.
        #
        # Grouped by message, and truncated. One unavailable service produces an
        # identical error for every scanner in every region — six paragraphs of
        # AWS prose for a single cause, which buries the findings the scan
        # actually came for. The full text is already in the warning log and the
        # audit trail.
        by_error: dict[str, list[str]] = {}
        for scanner, error in sorted(result.scanners_failed.items()):
            by_error.setdefault(_one_line(error), []).append(scanner)

        console.print(
            f"\n[bold yellow]{len(result.scanners_failed)} scanner(s) failed — "
            "those resource types were not checked:[/bold yellow]"
        )
        for error, scanners in by_error.items():
            console.print(f"  [yellow]![/yellow] {', '.join(scanners)}")
            console.print(f"    [dim]{error}[/dim]")
        console.print()

    if not findings:
        if result.regions_failed:
            console.print(
                "[bold yellow]No opportunities found in the regions that were "
                "scanned — but the failures above mean this is not a clean "
                "bill of health.[/bold yellow]"
            )
        else:
            console.print(
                "[bold green]No cost optimization opportunities found! "
                "Your environment is perfectly clean.[/bold green]"
            )
        return

    notified = notify_open_findings(
        repo, notifier, get_advisor(), settings.advisor_max_findings_per_scan
    )

    console.print(f"Findings Generated: [bold red]{len(findings)}[/bold red] violations")
    console.print(
        f"Notifications Sent: [bold cyan]{len(notified)}[/bold cyan] "
        f"(via {notifier.channel_name})\n"
    )

    # Five columns, not seven. Rich shrinks columns to fit the terminal, and at
    # a normal width the old layout squeezed Savings and Status to zero
    # characters — losing the only numbers anyone runs this for. Resource type
    # is dropped because the rule name already implies it (ebs_unattached is
    # never anything but a volume), and Protected becomes a marker on the id.
    table = Table(title="Optimization Opportunities")
    # Truncated rather than wrapped: one line per finding keeps a 24-finding
    # table scannable, and the tail of an AWS id is the least informative part.
    #
    # Cost and status get fixed widths. Rich shares width out by content length,
    # and resource ids are by far the longest thing here, so left to itself it
    # starves the two columns the report exists to communicate — a savings
    # column rendered as "$0…" is worse than no column at all.
    # Four columns fit an 80-column terminal exactly. Status is not among them:
    # the notified count is already reported above, and [P] marks the findings
    # deliberately held back — per-finding lifecycle detail belongs in
    # GET /findings, not in a summary that has to choose what to drop.
    table.add_column(
        "Resource ID", style="cyan", no_wrap=True, overflow="ellipsis", max_width=26
    )
    table.add_column("Region", style="yellow", no_wrap=True, width=14)
    table.add_column("Rule", style="blue", no_wrap=True, width=16)
    table.add_column("$/mo", justify="right", style="green", no_wrap=True, width=8)

    total_savings = 0.0
    # Actionable (non-protected) findings only — the same basis as the total.
    savings_by_region: dict[str, float] = {}
    count_by_region: dict[str, int] = {}
    by_id = {r.id: r for r in inventory}
    for f in findings:
        resource = by_id.get(f.resource_ref)
        res_id = resource.resource_id if resource else f.resource_ref
        res_region = resource.region if resource else "?"

        table.add_row(
            # \[ escapes the bracket: Rich would otherwise read [P] as a markup tag.
            rf"[bold green]\[P][/bold green] {res_id}" if f.protected else res_id,
            res_region,
            f.rule,
            f"${f.est_monthly_cost_usd:,.2f}",
        )
        if not f.protected:
            total_savings += float(f.est_monthly_cost_usd)
            savings_by_region[res_region] = (
                savings_by_region.get(res_region, 0.0) + float(f.est_monthly_cost_usd)
            )
            count_by_region[res_region] = count_by_region.get(res_region, 0) + 1

    console.print(table)
    if any(f.protected for f in findings):
        console.print(
            r"[dim]\[P] protected by tag — reported, never notified, never "
            "remediated, and excluded from the totals below.[/dim]"
        )

    if len(result.regions_scanned) > 1:
        by_region = Table(title="Savings by Region")
        by_region.add_column("Region", style="yellow", no_wrap=True)
        by_region.add_column("Findings", justify="right")
        by_region.add_column("Savings ($/mo)", justify="right", style="green")
        for region, savings in sorted(
            savings_by_region.items(), key=lambda item: item[1], reverse=True
        ):
            by_region.add_row(region, str(count_by_region[region]), f"${savings:.2f}")
        console.print(by_region)

    console.print(
        f"\n[bold]Total Potential Monthly Savings: [green]${total_savings:.2f}[/green][/bold]"
    )


@app.command()
def digest(
    no_send: bool = typer.Option(
        False, "--no-send", help="Render locally without posting to the notifier."
    ),
) -> None:
    """
    Post the advisory digest: right-sizing suggestions and any spend anomaly.

    Advisory by construction — no Approve buttons, no status changes, nothing
    remediated. Intended for a weekly schedule (the Phase 5 CronJob); --no-send
    is for checking what it would say without spending a notification.
    """
    repo = get_repository()
    advisor = get_advisor()

    with console.status("[bold yellow]Reading metrics and spend history...[/bold yellow]"):
        anomaly = detect_spend_anomaly(
            repo,
            window_days=settings.anomaly_window_days,
            min_history_days=settings.anomaly_min_history_days,
            z_threshold=settings.anomaly_z_threshold,
        )
        report = build_rightsizing_digest(
            get_digest_targets(),
            get_pricing(),
            observation_days=settings.rightsizing_observation_days,
            cpu_headroom_percent=settings.rightsizing_cpu_headroom_percent,
            min_datapoints=settings.rightsizing_min_datapoints,
            max_items=settings.digest_max_items,
        )
    suggestions = report.suggestions

    if report.regions_failed:
        # Same rule as `sentinel scan`: an empty result over regions that never
        # answered must never read as "nothing to do here".
        console.print(
            f"\n[bold red]{len(report.regions_failed)} region(s) could not be checked — "
            "the right-sizing results below are incomplete:[/bold red]"
        )
        for region, error in sorted(report.regions_failed.items()):
            console.print(f"  [red]✗[/red] {region}: {_one_line(error)}")

    if anomaly is not None:
        # "Estimated waste" is said out loud every time this number appears.
        # There is no billing data behind it, and a cost tool that blurs that
        # line is one nobody can check.
        arrow = "▲" if anomaly.direction == "increase" else "▼"
        console.print(
            f"\n[bold red]{arrow} Spend anomaly on {anomaly.date}[/bold red] — "
            f"estimated monthly waste [bold]${anomaly.value}[/bold] vs. a "
            f"${anomaly.mean} average (z={anomaly.z_score}) over "
            f"{anomaly.window_days} days.\n"
        )
    else:
        console.print(
            "\n[dim]No spend anomaly (or too little history to judge — "
            f"needs {settings.anomaly_min_history_days} days of scans).[/dim]\n"
        )

    if suggestions:
        table = Table(title="Right-sizing Suggestions (advisory)")
        table.add_column("Instance", style="cyan", no_wrap=True, overflow="ellipsis", max_width=22)
        table.add_column("Region", style="yellow", no_wrap=True, width=14)
        table.add_column("Now", style="blue", no_wrap=True, width=12)
        table.add_column("Suggested", style="magenta", no_wrap=True, width=12)
        table.add_column("Peak CPU", justify="right", no_wrap=True, width=9)
        table.add_column("$/mo saved", justify="right", style="green", no_wrap=True, width=11)

        for suggestion in suggestions:
            table.add_row(
                suggestion.resource_id,
                suggestion.region,
                suggestion.current_instance_type,
                suggestion.candidate.instance_type,
                f"{suggestion.max_cpu_percent:.1f}%",
                f"${suggestion.candidate.monthly_saving_usd:,.2f}",
            )
        console.print(table)

        total = sum(float(s.candidate.monthly_saving_usd) for s in suggestions)
        console.print(
            f"\n[bold]Right-sizing savings if all taken: [green]${total:.2f}[/green]/mo[/bold]"
        )
        console.print(
            "[dim]Peak CPU, not average, drives these. Nothing here is remediable — "
            "resizing needs a stop/start you have to schedule.[/dim]"
        )
    elif report.regions_failed and not report.instances_examined:
        console.print(
            "[bold yellow]No right-sizing verdict: nothing could be examined.[/bold yellow]"
        )
    else:
        console.print(
            f"[bold green]No over-provisioned instances found[/bold green] "
            f"[dim]({report.instances_examined} running instance(s) examined).[/dim]"
        )

    if no_send:
        console.print("\n[dim]--no-send: nothing was posted.[/dim]")
        return

    notifier = get_notifier()
    send_digest(
        repo,
        notifier,
        report,
        anomaly=anomaly,
        advisor=advisor,
        observation_days=settings.rightsizing_observation_days,
    )
    console.print(f"\n[bold cyan]Digest posted via {notifier.channel_name}.[/bold cyan]")


@app.command()
def regions() -> None:
    """
    List the regions the current configuration will scan.

    Cheap way to confirm AWS_REGIONS before paying for a full scan — in
    particular whether AWS_REGIONS=all actually resolved, or quietly fell back
    to the home region because ec2:DescribeRegions was denied.
    """
    resolved = get_regions()
    source = "AWS_REGIONS=all (discovered)" if settings.scans_all_regions else "AWS_REGIONS"
    if not settings.aws_regions.strip():
        source = "AWS_REGION (no AWS_REGIONS set)"

    console.print(f"[bold]{len(resolved)}[/bold] region(s) from [dim]{source}[/dim]:")
    for region in resolved:
        marker = " [dim](home)[/dim]" if region == settings.aws_region else ""
        console.print(f"  [cyan]{region}[/cyan]{marker}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """
    Start the FastAPI server (inbound API + notifier callbacks).
    """
    import uvicorn

    console.print(f"[bold green]Starting API server on {host}:{port}...[/bold green]")
    uvicorn.run("finops_sentinel.adapters.inbound.fastapi_app:app", host=host, port=port)


@app.command()
def smoke_llm(
    iterations: int = 10,
    model: str | None = typer.Option(
        None, help="Override OLLAMA_MODEL for this run, e.g. --model qwen3:4B"
    ),
    base_url: str | None = typer.Option(None, help="Override OLLAMA_BASE_URL for this run"),
) -> None:
    """
    Gate-check the LLM advisor: run the advisor prompt N times against a fixed
    synthetic finding and report how many replies satisfied the strict schema,
    plus latency. Exits non-zero if any iteration failed.

    --model / --base-url let you compare models without editing .env, which is
    the quickest way to qualify a new one before making it the default.
    """
    from finops_sentinel.adapters.advisor.ollama import OllamaAdvisor

    if model or base_url:
        advisor: object = OllamaAdvisor(
            base_url=base_url or settings.ollama_base_url,
            model=model or settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
        )
    else:
        advisor = get_advisor()

    if not isinstance(advisor, OllamaAdvisor):
        console.print(
            f"[bold yellow]ADVISOR_PROVIDER={settings.advisor_provider} has no backend to "
            "smoke-test (it needs no model). Set ADVISOR_PROVIDER=ollama, or pass "
            "--model to test one directly.[/bold yellow]"
        )
        raise typer.Exit(1)

    now = datetime.now(UTC)
    resource = Resource(
        id="smoke-resource",
        resource_id="i-0123456789abcdef0",
        resource_type=ResourceType.EC2_INSTANCE,
        resource_arn="arn:aws:ec2:us-east-1:account:instance/i-0123456789abcdef0",
        region="us-east-1",
        current_tags={"Name": "batch-worker-03", "env": "staging"},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
    )
    finding = Finding(
        id="smoke|i-0123456789abcdef0",
        resource_ref=resource.id,
        rule="ec2_idle",
        evidence={
            "InstanceId": resource.resource_id,
            "InstanceType": "m5.xlarge",
            "State": "running",
            "avg_cpu_percent": 0.7,
            "max_cpu_percent": 3.1,
            "avg_network_bytes": 12043.0,
            "observation_days": 14,
        },
        tags_at_detection=resource.current_tags,
        est_monthly_cost_usd=Decimal("140.16"),
        status=FindingStatus.OPEN,
        protected=False,
        detected_at=now,
        last_seen_at=now,
    )

    console.print(
        f"Smoke-testing [bold cyan]{advisor.model}[/bold cyan] at "
        f"[bold]{advisor.base_url}[/bold] — {iterations} iteration(s)\n"
    )

    passed = 0
    failures: list[str] = []
    durations: list[float] = []

    for attempt in range(1, iterations + 1):
        started = time.monotonic()
        try:
            response = advisor.advise(finding, resource)
        except Exception as exc:  # noqa: BLE001 - a gate check reports every failure mode
            failures.append(f"#{attempt}: {type(exc).__name__}: {exc}")
            console.print(f"  [red]✗[/red] #{attempt} {type(exc).__name__}")
            continue
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        passed += 1
        console.print(f"  [green]✓[/green] #{attempt} {elapsed:.2f}s risk={response.risk}")

    console.print(f"\nSchema-valid: [bold]{passed}/{iterations}[/bold]")
    if durations:
        console.print(
            f"Latency: min {min(durations):.2f}s / "
            f"mean {sum(durations) / len(durations):.2f}s / max {max(durations):.2f}s"
        )
    if failures:
        console.print("\n[bold red]Failures:[/bold red]")
        for failure in failures:
            console.print(f"  {failure}")
        console.print(
            "\n[yellow]Note: the pipeline still runs — the advisor falls back to "
            "deterministic templates on failure.[/yellow]"
        )
        raise typer.Exit(1)

    console.print("\n[bold green]Advisor healthy.[/bold green]")
    console.print(f"Sample: {advisor.summarize(finding, resource)}")


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
