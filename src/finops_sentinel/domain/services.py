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
from typing import Any, NamedTuple

from finops_sentinel.domain import rules
from finops_sentinel.domain.models import (
    TRANSITIONS,
    AuditEvent,
    Decision,
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
)
from finops_sentinel.domain.summaries import render_template_summary
from finops_sentinel.ports.advisor import Advisor
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner

logger = logging.getLogger(__name__)

EXPIRY_HOURS = 72

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


def approve_finding(
    finding_id: str,
    repo: FindingsRepository,
    gateway_for_region: Callable[[str], CloudGateway],
    actor: str,
    channel: str,
    dry_run: bool,
) -> bool:
    """
    Approve a finding and execute its allowlisted remediation playbook.

    The gateway is resolved from the resource's own region, not from a single
    configured one: an EC2 API call for a eu-west-1 volume sent to the
    us-east-1 endpoint fails with InvalidVolume.NotFound, which would read as
    "already deleted" rather than "wrong region".

    Guardrails re-checked here, framework-free:
    - protected findings (or resources protected since detection) are refused;
    - only playbooks in PLAYBOOK_ALLOWLIST can run;
    - the NOTIFIED→APPROVED move is an atomic CAS, so a double-click or a
      race against expiry executes at most one remediation;
    - dry_run records the attempt but leaves the finding APPROVED — only a
      real execution reaches REMEDIATED.

    Returns True if the approval (and remediation, when not dry_run)
    succeeded. Raises if the playbook itself fails, after recording FAILED.
    """
    finding = repo.get_finding_by_id(finding_id)
    if finding is None:
        return False

    if FindingStatus.APPROVED not in TRANSITIONS.get(finding.status, set()):
        return False

    resource = repo.get_resource_by_id(finding.resource_ref)
    if resource is None:
        return False

    if resource.lifecycle == ResourceLifecycle.DELETED:
        # The resource vanished since detection (deleted out-of-band or the
        # environment reset) — nothing to remediate, the playbook would only fail.
        _audit(
            repo,
            "approve_blocked_resource_gone",
            finding.id,
            {"actor": actor, "resource_id": resource.resource_id},
        )
        return False

    if finding.protected or rules.is_protected(resource.current_tags):
        _audit(
            repo,
            "approve_blocked_protected",
            finding.id,
            {"actor": actor, "resource_id": resource.resource_id},
        )
        return False

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
        return False

    playbook = rules.PLAYBOOK_ALLOWLIST.get(resource.resource_type)
    if playbook is None:
        _audit(
            repo,
            "approve_blocked_no_playbook",
            finding.id,
            {"actor": actor, "resource_type": str(resource.resource_type)},
        )
        return False

    if not repo.transition_finding(finding.id, finding.status, FindingStatus.APPROVED):
        return False  # lost the race — someone else already decided

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

    started_at = datetime.now(UTC)
    try:
        # Inside the try so a bad region (or missing credentials for it) is
        # recorded as a failed remediation rather than stranding the finding
        # in APPROVED with no trace of why.
        gateway = gateway_for_region(resource.region)
        result = gateway.execute(playbook, resource.resource_id, dry_run)
    except Exception as exc:
        repo.record_remediation(
            finding_id=finding.id,
            playbook=playbook,
            dry_run=dry_run,
            result="error",
            detail={"error": str(exc)},
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        repo.transition_finding(finding.id, FindingStatus.APPROVED, FindingStatus.FAILED)
        _audit(repo, "remediation_failed", finding.id, {"playbook": playbook, "error": str(exc)})
        raise

    if dry_run:
        repo.record_remediation(
            finding_id=finding.id,
            playbook=playbook,
            dry_run=True,
            result="dry_run",
            detail=result,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        _audit(repo, "remediation_dry_run", finding.id, {"playbook": playbook})
        return True

    repo.record_remediation(
        finding_id=finding.id,
        playbook=playbook,
        dry_run=False,
        result="success",
        detail=result,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    repo.transition_finding(finding.id, FindingStatus.APPROVED, FindingStatus.REMEDIATED)
    _audit(repo, "remediation_executed", finding.id, {"playbook": playbook, "result": result})
    return True


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

    if not repo.transition_finding(finding.id, finding.status, FindingStatus.DENIED):
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
