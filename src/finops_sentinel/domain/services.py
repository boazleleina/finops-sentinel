"""Use-case services. Pure domain logic — orchestrates ports only.

All status changes go through the repository's atomic compare-and-swap
(transition_finding); the domain decides WHICH transition to attempt by
consulting TRANSITIONS, the repository executes it atomically. Every
meaningful event is appended to the audit log.
"""
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

from finops_sentinel.domain import rules
from finops_sentinel.domain.anomaly import detect_anomaly
from finops_sentinel.domain.models import (
    TRANSITIONS,
    AuditEvent,
    Decision,
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    RightsizingSuggestion,
    SpendAnomaly,
    SpendSnapshot,
)
from finops_sentinel.domain.rightsizing import rank_suggestions, suggest_rightsizing, total_saving
from finops_sentinel.domain.summaries import render_template_narration, render_template_summary
from finops_sentinel.ports.advisor import Advisor
from finops_sentinel.ports.authorization import Authorizer
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.pricing import Pricing
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner

logger = logging.getLogger(__name__)

EXPIRY_HOURS = 72

# Findings that still represent money being spent right now. DENIED,
# REMEDIATED and EXPIRED are excluded: the first two are decided, and an
# expired finding is one nobody acted on within the window — re-detected next
# scan if it is still real. Protected findings are excluded too, matching the
# CLI's savings total: they are reported, never actioned, and counting them
# would make the trend line move when someone adds a tag.
LIVE_FINDING_STATUSES = frozenset({FindingStatus.OPEN, FindingStatus.NOTIFIED})

# Max LLM calls per notify pass. Local inference costs seconds per finding, so
# the spend goes to the most expensive findings and everything else takes the
# deterministic template.
DEFAULT_ADVISOR_BUDGET = 25


def _audit(
    repo: FindingsRepository, event: str, finding_id: str | None, detail: dict[str, Any]
) -> None:
    repo.record_audit(
        AuditEvent(ts=datetime.now(UTC), event=event, finding_id=finding_id, detail=detail)
    )


class ScanTarget(NamedTuple):
    """One region's worth of scanning: its gateway and its own scanners.

    Scanners are per-target, not shared: each stamps its region onto every
    Resource it discovers, and IdleEC2Scanner caches metric series on itself,
    so one instance cannot serve two regions.
    """

    region: str
    gateway: CloudGateway
    scanners: Sequence[Scanner]


class ScanResult(NamedTuple):
    """Findings plus what actually produced them, and what did not.

    Both failure maps are part of the result rather than log lines because a
    partial scan looks exactly like a clean account from the findings alone.

    scanners_failed is keyed "region/ScannerName" — one entry per scanner that
    could not complete discovery in a region that otherwise scanned fine.
    """

    findings: list[Finding]
    regions_scanned: list[str]
    regions_failed: dict[str, str]
    scanners_failed: dict[str, str] = {}  # noqa: RUF012 — NamedTuple default, never mutated
    # Resources THIS scan saw in the cloud. Not the same as the repository's
    # row count, which also holds every resource ever seen and since deleted.
    resources_discovered: int = 0


class _Discovery(NamedTuple):
    resources: list[tuple[Resource, dict[str, Any]]]
    # Position in target.scanners -> error, for the ones that raised. Keyed by
    # index rather than class name so two scanners of the same class in one
    # region stay distinct: collapsing them would both under-count failures
    # (hiding a fully blind region) and, in pass 2, skip a scanner that
    # actually succeeded.
    failures: dict[int, str]


def _discover_target(target: ScanTarget) -> _Discovery:
    """Pass 1 for a single region. Runs on a worker thread — no repo access.

    Each scanner is isolated. The region-level guard in run_scan stops one bad
    region blinding the others; this is the same argument one level down, and
    it matters just as much: the scanners share a gateway but not a blast
    radius. A service the account has no grant for, or that the endpoint does
    not implement at all, must not take the other five scanners' findings down
    with it — that turns a missing IAM permission into a silent, total loss of
    visibility, which is the most expensive failure mode this tool has.
    """
    discovered: list[tuple[Resource, dict[str, Any]]] = []
    failures: dict[int, str] = {}

    for index, scanner in enumerate(target.scanners):
        try:
            discovered.extend(scanner.discover(target.gateway))
        except Exception as exc:  # noqa: BLE001 — one bad scanner must not end the region
            failures[index] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Scanner %s failed in region %s: %s",
                type(scanner).__name__,
                target.region,
                exc,
            )

    return _Discovery(resources=discovered, failures=failures)


def run_scan(
    targets: Sequence[ScanTarget],
    repo: FindingsRepository,
    max_workers: int = 1,
) -> ScanResult:
    """
    Orchestrates the Two-Pass Scan across every target region:
    Pass 1: Discover inventory (Resources) and upsert to repository. Unseen resources marked DELETED.
    Pass 2: Evaluate the active inventory to generate findings and upsert to repository.

    save_finding never touches status, so re-detected findings keep whatever
    state their lifecycle reached (DENIED stays DENIED, REMEDIATED stays
    REMEDIATED).

    Region isolation matters in three places:

    - A region that raises is recorded and skipped, not fatal. One region with
      a missing IAM grant or an outage must not blind the other fifteen.
    - The DELETED sweep is scoped to the regions that succeeded. Sweeping
      globally would declare a failed region's entire inventory gone, and
      DELETED resources are refused by approve_finding.
    - evaluate() sees only its own region's inventory. Cross-region pooling
      would let a volume in one region vouch for a snapshot in another, and
      hand region-A resources to region-B scanners.

    Discovery is threaded (repository writes stay on this thread, so the
    repository needs no thread-safety guarantees). Raises RuntimeError when
    every region failed — returning "no findings" there reads as a clean
    account, which is the most expensive lie this tool could tell.
    """
    scan_start_time = datetime.now(UTC)

    inventory_by_region: dict[str, list[tuple[Resource, dict[str, Any]]]] = {}
    failed_regions: dict[str, str] = {}
    # Scanner class names that failed discovery, per region. Pass 2 skips them:
    # a scanner whose discover() raised may hold half-populated state (the idle
    # scanners cache metric series between the two passes), so evaluating it
    # would judge instances on a partial series.
    failed_scanners_by_region: dict[str, dict[int, str]] = {}

    workers = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_discover_target, target): target for target in targets}
        for future in as_completed(futures):
            target = futures[future]
            region = target.region
            try:
                discovery = future.result()
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - backstop
                # Unreachable by construction: _discover_target catches every
                # scanner exception itself, so nothing propagates here. Kept
                # anyway — if a future edit lets something escape that function,
                # the cost of not catching it is the entire multi-region scan,
                # and this is three lines.
                failed_regions[region] = f"{type(exc).__name__}: {exc}"
                logger.warning("Scan of region %s failed: %s", region, exc)
                continue

            if target.scanners and len(discovery.failures) == len(target.scanners):
                # Every scanner failed: the region is blind, not clean. Treat it
                # as a failed region so the DELETED sweep skips it and nothing
                # here gets disarmed.
                failed_regions[region] = "; ".join(
                    f"{type(target.scanners[index]).__name__}: {error}"
                    for index, error in sorted(discovery.failures.items())
                )
                continue

            inventory_by_region[region] = discovery.resources
            if discovery.failures:
                failed_scanners_by_region[region] = discovery.failures

    for region, error in failed_regions.items():
        _audit(repo, "region_scan_failed", None, {"region": region, "error": error})

    scanners_by_region = {target.region: target.scanners for target in targets}
    scanners_failed: dict[str, str] = {}
    for region, failures in failed_scanners_by_region.items():
        for index, error in sorted(failures.items()):
            name = type(scanners_by_region[region][index]).__name__
            scanners_failed[f"{region}/{name}"] = error
            _audit(
                repo,
                "scanner_failed",
                None,
                {"region": region, "scanner": name, "error": error},
            )

    if targets and not inventory_by_region:
        raise RuntimeError(
            "Every region failed to scan: "
            + "; ".join(f"{region}: {error}" for region, error in failed_regions.items())
        )

    # Upsert before Pass 2: upsert_resource rewrites resource.id to the stored
    # id for already-known resources, and findings reference that id.
    discovered_count = 0
    for target in targets:
        for resource, _ in inventory_by_region.get(target.region, []):
            repo.upsert_resource(resource)
            discovered_count += 1

    scanned_regions = sorted(inventory_by_region)
    repo.mark_unseen_resources_deleted(scan_start_time, regions=scanned_regions)

    all_findings: list[Finding] = []
    for target in targets:
        inventory = inventory_by_region.get(target.region)
        if inventory is None:
            continue
        blind = failed_scanners_by_region.get(target.region, {})
        for index, scanner in enumerate(target.scanners):
            if index in blind:
                continue
            findings = scanner.evaluate(inventory)
            all_findings.extend(findings)

            for finding in findings:
                repo.save_finding(finding)

    record_spend_snapshot(repo)

    _audit(
        repo,
        "scan_completed",
        None,
        {
            "regions_scanned": scanned_regions,
            "regions_failed": sorted(failed_regions),
            "scanners_failed": sorted(scanners_failed),
            "resources_discovered": discovered_count,
            "findings": len(all_findings),
        },
    )
    return ScanResult(
        findings=all_findings,
        regions_scanned=scanned_regions,
        regions_failed=failed_regions,
        scanners_failed=scanners_failed,
        resources_discovered=discovered_count,
    )


def notify_open_findings(
    repo: FindingsRepository,
    notifier: Notifier,
    advisor: Advisor | None = None,
    advisor_budget: int = DEFAULT_ADVISOR_BUDGET,
) -> list[Finding]:
    """
    Send alerts for OPEN, non-protected findings and transition them to
    NOTIFIED. Protected findings are never notified and never leave OPEN.
    A failed send leaves the finding OPEN so the next scan retries it.

    When an advisor is supplied, findings get a summary attached and persisted
    before the alert goes out. The advisor is advisory only: its port forbids
    raising, so a dead LLM backend cannot stop a notification.

    Findings are processed most-expensive-first, and only the first
    advisor_budget of them pay for LLM inference — the rest fall back to the
    deterministic template. A local model costs seconds per call, so an
    unbudgeted scan over an account with a thousand findings would run for
    hours. Set advisor_budget=0 to skip inference entirely.
    """
    notified: list[Finding] = []
    candidates = sorted(
        repo.get_findings(status=FindingStatus.OPEN),
        key=lambda f: f.est_monthly_cost_usd,
        reverse=True,
    )
    inferences_left = advisor_budget

    for finding in candidates:
        if finding.protected:
            continue

        resource = repo.get_resource_by_id(finding.resource_ref)
        if resource is None:
            continue

        if advisor is not None and not finding.llm_summary:
            if inferences_left > 0:
                finding.llm_summary = advisor.summarize(finding, resource)
                inferences_left -= 1
            else:
                finding.llm_summary = render_template_summary(finding, resource)
            repo.save_finding(finding)

        message_ref = notifier.send_finding_alert(finding, resource)
        if not repo.transition_finding(finding.id, FindingStatus.OPEN, FindingStatus.NOTIFIED):
            continue  # raced another notifier; alert may duplicate but state stays consistent

        sent_at = datetime.now(UTC)
        repo.record_notification(finding.id, notifier.channel_name, message_ref, sent_at)
        _audit(
            repo,
            "finding_notified",
            finding.id,
            {"channel": notifier.channel_name, "message_ref": message_ref},
        )
        finding.status = FindingStatus.NOTIFIED
        notified.append(finding)
    return notified


class ApprovalPlan(NamedTuple):
    """A committed approval, and everything its playbook needs to run.

    Produced by commit_approval once the finding is already APPROVED in the
    database. Holds no repository handle and no gateway: it crosses a thread or
    a task boundary, and it has to stay a statement of what was decided rather
    than a live object graph.

    Carries the actor because the gateway factory is what resolves credentials:
    an adapter that runs the playbook under the approver's own AWS role needs
    to know who approved. Stated in domain terms — no role ARNs, no session
    policies, nothing this module would have to relearn for a different cloud.
    """

    finding_id: str
    resource_id: str
    region: str
    playbook: str
    actor: str


def commit_approval(
    finding_id: str,
    repo: FindingsRepository,
    actor: str,
    channel: str,
    authorizer: Authorizer | None = None,
) -> ApprovalPlan | None:
    """
    Run every guardrail and commit the NOTIFIED→APPROVED transition. No cloud
    call happens here, so this is repository reads and one write — fast enough
    to finish inside a channel's acknowledgement budget.

    Split out from approve_finding because remediation is not fast. The EBS
    playbook snapshots the volume and *waits* for the snapshot before deleting,
    which is minutes. A caller that ran the whole thing inline would blow
    Slack's three-second window, leave the buttons on the message for the
    entire remediation, and hand the user a timeout to retry into. Committing
    the decision first means the state change — the thing that makes a second
    click a no-op — has already landed when the acknowledgement goes out.

    Returns None if any guardrail refused; the reason is in the audit log.
    """
    finding = repo.get_finding_by_id(finding_id)
    if finding is None:
        return None

    if FindingStatus.APPROVED not in TRANSITIONS.get(finding.status, set()):
        return None

    if authorizer is not None and not authorizer.can_approve(actor):
        # Audited like every other refusal: a rejected attempt has to be as
        # visible in the log as an accepted one, or the trail only records the
        # approvals that happened to be permitted.
        _audit(repo, "approve_blocked_unauthorized", finding.id, {"actor": actor})
        return None

    resource = repo.get_resource_by_id(finding.resource_ref)
    if resource is None:
        return None

    if resource.lifecycle == ResourceLifecycle.DELETED:
        # The resource vanished since detection (deleted out-of-band or the
        # environment reset) — nothing to remediate, the playbook would only fail.
        _audit(
            repo,
            "approve_blocked_resource_gone",
            finding.id,
            {"actor": actor, "resource_id": resource.resource_id},
        )
        return None

    if finding.protected or rules.is_protected(resource.current_tags):
        _audit(
            repo,
            "approve_blocked_protected",
            finding.id,
            {"actor": actor, "resource_id": resource.resource_id},
        )
        return None

    if not rules.is_remediable(finding.rule):
        # Metric-inferred findings are advisory. The type-keyed playbook
        # allowlist would happily hand an ec2_idle finding the
        # terminate_stopped_instance playbook — and that instance is RUNNING.
        _audit(
            repo,
            "approve_blocked_notify_only",
            finding.id,
            {"actor": actor, "rule": finding.rule},
        )
        return None

    playbook = rules.PLAYBOOK_ALLOWLIST.get(resource.resource_type)
    if playbook is None:
        _audit(
            repo,
            "approve_blocked_no_playbook",
            finding.id,
            {"actor": actor, "resource_type": str(resource.resource_type)},
        )
        return None

    # NOTIFIED, not finding.status: the precondition has to name the
    # pre-decision state itself. Passing the status this invocation happens to
    # have read makes the CAS a tautology on a replay — a second click that
    # loads APPROVED would run `SET status='APPROVED' WHERE status='APPROVED'`,
    # match one row, and remediate again. The transition table refuses that
    # case one gate above, but only as long as nobody adds a self-loop to it.
    if not repo.transition_finding(finding.id, FindingStatus.NOTIFIED, FindingStatus.APPROVED):
        return None  # already decided — a concurrent click, or a replayed one

    repo.record_decision(
        Decision(
            finding_id=finding.id,
            actor=actor,
            action="approve",
            decided_at=datetime.now(UTC),
            channel=channel,
        )
    )
    _audit(repo, "finding_approved", finding.id, {"actor": actor, "channel": channel})

    return ApprovalPlan(
        finding_id=finding.id,
        resource_id=resource.resource_id,
        region=resource.region,
        playbook=playbook,
        actor=actor,
    )


def execute_approval(
    plan: ApprovalPlan,
    repo: FindingsRepository,
    gateway_for_approval: Callable[[ApprovalPlan], CloudGateway],
    dry_run: bool,
) -> bool:
    """
    Run a committed approval's playbook and record what it did.

    The gateway is built from the plan, not from ambient configuration. The
    region has to come from the resource — an EC2 call for a eu-west-1 volume
    sent to the us-east-1 endpoint fails with InvalidVolume.NotFound, which
    reads as "already deleted" rather than "wrong region" — and the factory may
    use the rest of the plan to decide *whose* credentials execute it. The AWS
    adapter assumes the approver's role and narrows the session to this one
    resource; a fake in a test hands back a recorder. The domain does not care
    which, only that a refusal to issue credentials surfaces here as a failed
    remediation with an audit row.

    Safe to run outside the request that approved it — the finding is already
    APPROVED, so nothing else can enter this path for it. Raises if the
    playbook fails, after recording the failure and moving the finding to
    FAILED.
    """
    started_at = datetime.now(UTC)
    try:
        # Inside the try so a refused credential — or a bad region, or missing
        # permissions for it — is recorded as a failed remediation rather than
        # stranding the finding in APPROVED with no trace of why.
        gateway = gateway_for_approval(plan)
        result = gateway.execute(plan.playbook, plan.resource_id, dry_run)
    except Exception as exc:
        repo.record_remediation(
            finding_id=plan.finding_id,
            playbook=plan.playbook,
            dry_run=dry_run,
            result="error",
            detail={"error": str(exc)},
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        repo.transition_finding(plan.finding_id, FindingStatus.APPROVED, FindingStatus.FAILED)
        _audit(
            repo,
            "remediation_failed",
            plan.finding_id,
            {"playbook": plan.playbook, "error": str(exc)},
        )
        raise

    if dry_run:
        repo.record_remediation(
            finding_id=plan.finding_id,
            playbook=plan.playbook,
            dry_run=True,
            result="dry_run",
            detail=result,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        _audit(repo, "remediation_dry_run", plan.finding_id, {"playbook": plan.playbook})
        return True

    repo.record_remediation(
        finding_id=plan.finding_id,
        playbook=plan.playbook,
        dry_run=False,
        result="success",
        detail=result,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    repo.transition_finding(plan.finding_id, FindingStatus.APPROVED, FindingStatus.REMEDIATED)
    _audit(
        repo,
        "remediation_executed",
        plan.finding_id,
        {"playbook": plan.playbook, "result": result},
    )
    return True


def approve_finding(
    finding_id: str,
    repo: FindingsRepository,
    gateway_for_approval: Callable[[ApprovalPlan], CloudGateway],
    actor: str,
    channel: str,
    dry_run: bool,
    authorizer: Authorizer | None = None,
) -> bool:
    """
    Approve a finding and execute its allowlisted remediation playbook.

    commit_approval followed by execute_approval, for callers with no
    acknowledgement deadline — the CLI and the HTTP API. A channel adapter that
    must answer within seconds calls the two halves itself and runs the second
    one in the background.

    Guardrails re-checked in commit_approval, framework-free:
    - the actor must be permitted to approve. Channel adapters authenticate the
      transport (a Slack signature proves the request came through the app,
      for every human who can see the message); only this check asks whether
      the person who clicked may delete infrastructure;
    - protected findings (or resources protected since detection) are refused;
    - only playbooks in PLAYBOOK_ALLOWLIST can run;
    - the NOTIFIED→APPROVED move is an atomic CAS against a literal expected
      status, so concurrent clicks resolve to one winner, and a replay — the
      double-click, the retry after a Slack timeout — finds the finding already
      out of NOTIFIED and updates zero rows;
    - dry_run records the attempt but leaves the finding APPROVED — only a
      real execution reaches REMEDIATED.

    Returns True if the approval (and remediation, when not dry_run)
    succeeded. Raises if the playbook itself fails, after recording FAILED.
    """
    plan = commit_approval(finding_id, repo, actor=actor, channel=channel, authorizer=authorizer)
    if plan is None:
        return False
    return execute_approval(plan, repo, gateway_for_approval, dry_run)


def deny_finding(
    finding_id: str,
    repo: FindingsRepository,
    actor: str,
    channel: str,
) -> bool:
    """
    Deny a finding, terminating its lifecycle (DENIED is terminal in v1).
    """
    finding = repo.get_finding_by_id(finding_id)
    if finding is None:
        return False

    if FindingStatus.DENIED not in TRANSITIONS.get(finding.status, set()):
        return False

    # Literal precondition, for the reason spelled out in approve_finding.
    if not repo.transition_finding(finding.id, FindingStatus.NOTIFIED, FindingStatus.DENIED):
        return False

    repo.record_decision(
        Decision(
            finding_id=finding.id,
            actor=actor,
            action="deny",
            decided_at=datetime.now(UTC),
            channel=channel,
        )
    )
    _audit(repo, "finding_denied", finding.id, {"actor": actor, "channel": channel})
    return True


class MetricTarget(NamedTuple):
    """One region's gateway, for work that reads metrics but scans nothing.

    Deliberately not a ScanTarget: the digest builds no inventory and evaluates
    no rules, so handing it a scanner list would imply a discovery pass that
    never happens — and would mean building eight scanners per region to make
    one CloudWatch call each.
    """

    region: str
    gateway: CloudGateway


def record_spend_snapshot(repo: FindingsRepository) -> SpendSnapshot:
    """Store today's estimated monthly waste, so tomorrow has something to compare to.

    "Estimated waste", not spend — see SpendSnapshot. Upserted by date, so the
    third scan of the day overwrites the first two rather than triple-weighting
    today in the trailing mean.
    """
    now = datetime.now(UTC)
    live = [
        finding
        for finding in repo.get_findings()
        if finding.status in LIVE_FINDING_STATUSES and not finding.protected
    ]
    active = [
        resource
        for resource in repo.get_all_resources()
        if resource.lifecycle == ResourceLifecycle.ACTIVE
    ]
    snapshot = SpendSnapshot(
        snapshot_date=now.date(),
        total_estimated_monthly_usd=sum(
            (finding.est_monthly_cost_usd for finding in live), Decimal(0)
        ),
        open_findings=len(live),
        active_resources=len(active),
        captured_at=now,
    )
    repo.record_spend_snapshot(snapshot)
    _audit(
        repo,
        "spend_snapshot_recorded",
        None,
        {
            "date": snapshot.snapshot_date.isoformat(),
            "total_estimated_monthly_usd": str(snapshot.total_estimated_monthly_usd),
            "open_findings": snapshot.open_findings,
            "active_resources": snapshot.active_resources,
        },
    )
    return snapshot


def detect_spend_anomaly(
    repo: FindingsRepository,
    window_days: int = 14,
    min_history_days: int = 7,
    z_threshold: float = 2.0,
) -> SpendAnomaly | None:
    """Today's estimated waste against its own trailing distribution.

    Reads one extra day either side of the window so the maths in
    domain.anomaly gets a full baseline plus the candidate day, and so a
    missing day (no scan ran) narrows the baseline rather than shifting it.
    """
    since = (datetime.now(UTC) - timedelta(days=window_days + 1)).date()
    snapshots = repo.get_spend_snapshots(since)
    return detect_anomaly(
        snapshots,
        window_days=window_days,
        min_history_days=min_history_days,
        z_threshold=z_threshold,
    )


class RightsizingReport(NamedTuple):
    """Suggestions plus what was actually looked at to produce them.

    instances_examined is here for the same reason regions_failed is: an empty
    suggestion list over 40 instances is good news, and an empty one over zero
    instances is a broken pipeline. The list alone cannot tell them apart.
    """

    suggestions: list[RightsizingSuggestion]
    regions_failed: dict[str, str]
    instances_examined: int


def build_rightsizing_digest(
    targets: Sequence[MetricTarget],
    pricing: Pricing,
    observation_days: int = 14,
    cpu_headroom_percent: float = 40.0,
    min_datapoints: int = 24,
    max_items: int = 10,
) -> RightsizingReport:
    """Suggest smaller instance types for running boxes that never get busy.

    This re-reads CloudWatch rather than using anything a scan stored, because
    right-sizing is about instances that are NOT idle — no finding exists for
    them, so no finding carries their metrics. One extra GetMetricStatistics
    pass on a weekly digest is $0.01/1000 requests; a metric_summaries table
    written on every scan is a schema and a migration forever.

    A region that fails is skipped, never fatal — a digest covering three
    regions out of four is still worth sending — but the failures come back in
    the report rather than only in a log line. "Nothing is over-provisioned"
    and "every region refused to answer" produce identical suggestion lists and
    mean opposite things, and only one of them is safe to act on by doing
    nothing. That is the same lie run_scan's regions_failed exists to prevent.

    Protected instances are excluded. A resource tagged finops:protected=true
    is one the owner has said not to touch, and "you could shrink this" is
    still touching it.
    """
    suggestions: list[RightsizingSuggestion] = []
    regions_failed: dict[str, str] = {}
    examined = 0

    for target in targets:
        try:
            instances = target.gateway.describe_running_ec2_instances()
        except Exception as exc:  # noqa: BLE001 — one bad region must not sink the digest
            regions_failed[target.region] = f"{type(exc).__name__}: {exc}"
            logger.warning("Right-sizing skipped region %s: %s", target.region, exc)
            continue

        for instance in instances:
            instance_id = instance["InstanceId"]
            instance_type = instance.get("InstanceType", "")
            tags = instance.get("Tags", [])
            tags_dict = {t["Key"]: t["Value"] for t in tags} if isinstance(tags, list) else tags
            if rules.is_protected(tags_dict):
                continue
            examined += 1

            try:
                cpu = target.gateway.get_metric_averages(
                    namespace="AWS/EC2",
                    dimensions={"InstanceId": instance_id},
                    metric_name="CPUUtilization",
                    days=observation_days,
                )
            except Exception as exc:  # noqa: BLE001 — one instance, not the digest
                logger.warning("No CPU metrics for %s: %s", instance_id, exc)
                continue

            suggestion = suggest_rightsizing(
                resource_id=instance_id,
                region=target.region,
                instance_type=instance_type,
                cpu_series=cpu,
                current_monthly_cost_usd=pricing.ec2_instance_monthly(
                    instance_type=instance_type, region=target.region
                ),
                candidates=pricing.rightsizing_candidates(
                    instance_type=instance_type, region=target.region
                ),
                observation_days=observation_days,
                cpu_headroom_percent=cpu_headroom_percent,
                min_datapoints=min_datapoints,
            )
            if suggestion is not None:
                suggestions.append(suggestion)

    return RightsizingReport(
        suggestions=rank_suggestions(suggestions, max_items),
        regions_failed=regions_failed,
        instances_examined=examined,
    )


def _narrate(advisor: Advisor | None, topic: str, facts: dict[str, Any]) -> str:
    """Prose for a digest section, whatever the advisor does.

    The Advisor port forbids raising, and both shipped adapters honour it — but
    this is the one caller where a third-party implementation breaking that
    contract would cost the entire digest rather than one finding's summary.
    Three lines to make that impossible.
    """
    if advisor is not None:
        try:
            return advisor.narrate(topic, facts)
        except Exception as exc:  # noqa: BLE001 — advisory text is never worth a failed send
            logger.warning("Advisor.narrate(%s) raised, using template: %s", topic, exc)
    return render_template_narration(topic, facts)


def compose_digest_sections(
    report: RightsizingReport,
    anomaly: SpendAnomaly | None,
    advisor: Advisor | None = None,
    observation_days: int = 14,
) -> list[str]:
    """Render the digest body. Pure: no sending, no repository, no clock.

    The anomaly leads when there is one — it is the time-sensitive part ("did
    something change since yesterday"), where right-sizing is a standing
    backlog that will still be true next week.

    Takes the whole report rather than just its suggestions so the message can
    say when its coverage was partial. A reader who sees "nothing looks
    over-provisioned" acts by doing nothing, which is the wrong response to
    "three regions did not answer".

    Every number here was computed deterministically before the advisor saw
    it; narration only arranges what it was given.
    """
    suggestions = report.suggestions
    sections: list[str] = []

    if anomaly is not None:
        facts = {
            "date": anomaly.date.isoformat(),
            "value": str(anomaly.value),
            "mean": str(anomaly.mean),
            "z_score": anomaly.z_score,
            "window_days": anomaly.window_days,
            "direction": anomaly.direction,
        }
        sections.append(
            "*Spend anomaly*\n"
            + _narrate(advisor, "spend_anomaly", facts)
            + f"\n_Estimated monthly waste, not billed spend — "
            f"${anomaly.value} vs. a ${anomaly.mean} average "
            f"(z={anomaly.z_score}) over {anomaly.window_days} days._"
        )

    if suggestions:
        saving = total_saving(list(suggestions))
        facts = {
            "count": len(suggestions),
            "window_days": observation_days,
            "total_saving": str(saving),
        }
        # Both costs and both CPU numbers, not just the deltas: the saving is
        # checkable arithmetic when the two prices are shown, and peak-vs-avg
        # side by side is the evidence an operator needs to disagree with the
        # suggestion (a big gap says "bursty", a small one says "flat").
        lines = [
            f"• `{s.resource_id}` ({s.region}) "
            f"{s.current_instance_type} ${s.current_monthly_cost_usd}/mo → "
            f"{s.candidate.instance_type} ${s.candidate.monthly_cost_usd}/mo — "
            f"save ${s.candidate.monthly_saving_usd}/mo "
            f"(CPU peak {s.max_cpu_percent}% / avg {s.avg_cpu_percent}%, "
            f"{s.datapoints} datapoints)"
            for s in suggestions
        ]
        sections.append(
            "*Right-sizing suggestions*\n"
            + _narrate(advisor, "rightsizing", facts)
            + "\n"
            + "\n".join(lines)
        )
    else:
        sections.append(
            "*Right-sizing suggestions*\nNothing looks over-provisioned across the "
            f"{report.instances_examined} running instance(s) examined over the last "
            f"{observation_days} days. Instances with too little metric history to "
            "judge are skipped rather than assumed healthy."
        )

    if report.regions_failed:
        # Last, and unmissable. This is the line that stops an empty digest
        # reading as a clean bill of health.
        failures = "\n".join(
            f"• {region}: {error}" for region, error in sorted(report.regions_failed.items())
        )
        sections.append(
            f"*⚠️ Incomplete coverage* — {len(report.regions_failed)} region(s) could not "
            f"be checked, so the suggestions above are not the whole picture:\n{failures}"
        )

    return sections


def send_digest(
    repo: FindingsRepository,
    notifier: Notifier,
    report: RightsizingReport,
    anomaly: SpendAnomaly | None = None,
    advisor: Advisor | None = None,
    observation_days: int = 14,
    title: str = "FinOps Sentinel — weekly digest",
) -> str | None:
    """Compose and send the digest exactly once, then audit it.

    Advisory end to end: send_digest's port contract forbids approve/deny
    affordances, and nothing here changes a finding's status. A digest is a
    report, so re-sending it is harmless — which is why it needs none of the
    compare-and-swap machinery the notification path has.
    """
    sections = compose_digest_sections(
        report, anomaly, advisor=advisor, observation_days=observation_days
    )
    message_ref = notifier.send_digest(title, sections)
    _audit(
        repo,
        "digest_sent",
        None,
        {
            "channel": notifier.channel_name,
            "message_ref": message_ref,
            "suggestions": len(report.suggestions),
            "total_saving_usd": str(total_saving(report.suggestions)),
            "instances_examined": report.instances_examined,
            "regions_failed": sorted(report.regions_failed),
            "anomaly": anomaly.date.isoformat() if anomaly else None,
        },
    )
    return message_ref


def expire_stale(repo: FindingsRepository, max_age_hours: int = EXPIRY_HOURS) -> list[str]:
    """
    Expire NOTIFIED findings whose latest notification is older than
    max_age_hours. Findings notified before notification tracking existed
    fall back to detected_at. Returns the ids expired.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    expired: list[str] = []

    for finding in repo.get_findings(status=FindingStatus.NOTIFIED):
        notified_at = repo.get_latest_notification_time(finding.id) or finding.detected_at
        if notified_at >= cutoff:
            continue
        if repo.transition_finding(finding.id, FindingStatus.NOTIFIED, FindingStatus.EXPIRED):
            _audit(repo, "finding_expired", finding.id, {"notified_at": notified_at.isoformat()})
            expired.append(finding.id)
    return expired
