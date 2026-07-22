"""Use-case services. Pure domain logic — orchestrates ports only.

All status changes go through the repository's atomic compare-and-swap
(transition_finding); the domain decides WHICH transition to attempt by
consulting TRANSITIONS, the repository executes it atomically. Every
meaningful event is appended to the audit log.
"""
from datetime import UTC, datetime, timedelta
from typing import Any, List

from finops_sentinel.domain import rules
from finops_sentinel.domain.models import (
    AuditEvent,
    Decision,
    Finding,
    FindingStatus,
    ResourceLifecycle,
    TRANSITIONS,
)
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner

EXPIRY_HOURS = 72


def _audit(
    repo: FindingsRepository, event: str, finding_id: str | None, detail: dict[str, Any]
) -> None:
    repo.record_audit(
        AuditEvent(ts=datetime.now(UTC), event=event, finding_id=finding_id, detail=detail)
    )


def run_scan(
    gateway: CloudGateway,
    repo: FindingsRepository,
    scanners: List[Scanner],
) -> List[Finding]:
    """
    Orchestrates the Two-Pass Scan:
    Pass 1: Discover inventory (Resources) and upsert to repository. Unseen resources marked DELETED.
    Pass 2: Evaluate the active inventory to generate findings and upsert to repository.

    save_finding never touches status, so re-detected findings keep whatever
    state their lifecycle reached (DENIED stays DENIED, REMEDIATED stays
    REMEDIATED).
    """
    scan_start_time = datetime.now(UTC)

    discovered_resources: List[tuple[Any, dict[str, Any]]] = []
    for scanner in scanners:
        scanner_resources = scanner.discover(gateway)
        discovered_resources.extend(scanner_resources)

        for resource, _ in scanner_resources:
            repo.upsert_resource(resource)

    repo.mark_unseen_resources_deleted(scan_start_time)

    all_findings: List[Finding] = []
    for scanner in scanners:
        findings = scanner.evaluate(discovered_resources)
        all_findings.extend(findings)

        for finding in findings:
            repo.save_finding(finding)

    _audit(
        repo,
        "scan_completed",
        None,
        {"resources_discovered": len(discovered_resources), "findings": len(all_findings)},
    )
    return all_findings


def notify_open_findings(repo: FindingsRepository, notifier: Notifier) -> List[Finding]:
    """
    Send alerts for OPEN, non-protected findings and transition them to
    NOTIFIED. Protected findings are never notified and never leave OPEN.
    A failed send leaves the finding OPEN so the next scan retries it.
    """
    notified: List[Finding] = []
    for finding in repo.get_findings(status=FindingStatus.OPEN):
        if finding.protected:
            continue

        resource = repo.get_resource_by_id(finding.resource_ref)
        if resource is None:
            continue

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
    gateway: CloudGateway,
    actor: str,
    channel: str,
    dry_run: bool,
) -> bool:
    """
    Approve a finding and execute its allowlisted remediation playbook.

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


def expire_stale(repo: FindingsRepository, max_age_hours: int = EXPIRY_HOURS) -> List[str]:
    """
    Expire NOTIFIED findings whose latest notification is older than
    max_age_hours. Findings notified before notification tracking existed
    fall back to detected_at. Returns the ids expired.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    expired: List[str] = []

    for finding in repo.get_findings(status=FindingStatus.NOTIFIED):
        notified_at = repo.get_latest_notification_time(finding.id) or finding.detected_at
        if notified_at >= cutoff:
            continue
        if repo.transition_finding(finding.id, FindingStatus.NOTIFIED, FindingStatus.EXPIRED):
            _audit(repo, "finding_expired", finding.id, {"notified_at": notified_at.isoformat()})
            expired.append(finding.id)
    return expired
