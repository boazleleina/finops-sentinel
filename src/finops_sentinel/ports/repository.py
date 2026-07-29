from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import date, datetime
from typing import Any

from finops_sentinel.domain.models import (
    AuditEvent,
    Decision,
    Finding,
    FindingStatus,
    Resource,
    SpendSnapshot,
)


class FindingsRepository(ABC):
    """
    Port for persisting and retrieving resources, findings, and their
    satellite records (decisions, notifications, remediations, audit events).
    """

    @abstractmethod
    def upsert_resource(self, resource: Resource) -> None:
        ...

    @abstractmethod
    def get_resource_by_id(self, resource_id: str) -> Resource | None:
        ...

    @abstractmethod
    def get_all_resources(self) -> list[Resource]:
        ...

    @abstractmethod
    def mark_unseen_resources_deleted(
        self, cutoff_time: datetime, regions: Collection[str] | None = None
    ) -> None:
        """
        Mark every resource not seen since cutoff_time as DELETED.

        `regions` scopes the sweep to the regions a scan actually reached.
        A region that failed mid-scan must keep its inventory ACTIVE: DELETED
        blocks remediation, so a transient API error would otherwise disarm
        every finding in that region. None sweeps all regions.
        """
        ...

    @abstractmethod
    def save_finding(self, finding: Finding) -> bool:
        """
        Insert a new finding, or refresh a re-detected one (evidence, tags,
        cost, last_seen_at, protected flag). NEVER changes status — status
        moves only through transition_finding, so re-scans cannot resurrect
        terminal findings.
        """
        ...

    @abstractmethod
    def transition_finding(
        self, finding_id: str, expected: FindingStatus, new: FindingStatus
    ) -> bool:
        """
        Atomic compare-and-swap status transition:
        UPDATE findings SET status=:new WHERE id=:id AND status=:expected.
        Returns True only if this call performed the transition (rowcount 1).
        A lost race or wrong expected status returns False.
        """
        ...

    @abstractmethod
    def get_findings(self, status: FindingStatus | None = None) -> list[Finding]:
        ...

    @abstractmethod
    def get_finding_by_id(self, finding_id: str) -> Finding | None:
        ...

    @abstractmethod
    def record_decision(self, decision: Decision) -> None:
        """Append a decision row. History is never overwritten; latest wins."""
        ...

    @abstractmethod
    def record_audit(self, event: AuditEvent) -> None:
        """Append-only audit log. Every scan/notify/approve/deny/execute/failure."""
        ...

    @abstractmethod
    def get_audit_events(self, finding_id: str | None = None) -> list[AuditEvent]:
        ...

    @abstractmethod
    def record_notification(
        self, finding_id: str, channel: str, message_ref: str | None, sent_at: datetime
    ) -> None:
        ...

    @abstractmethod
    def get_latest_notification_time(self, finding_id: str) -> datetime | None:
        ...

    @abstractmethod
    def record_spend_snapshot(self, snapshot: SpendSnapshot) -> None:
        """Upsert one day's estimated-waste snapshot, keyed by its date.

        Upsert rather than append: several scans a day are normal (a cron plus
        a manual run), and appending would let a busy Tuesday contribute five
        points to a mean that a quiet Wednesday contributes one to — skewing
        the baseline by scan cadence rather than by spend. Last write for a
        date wins, so the snapshot always reflects the latest known state.
        """
        ...

    @abstractmethod
    def get_spend_snapshots(self, since: date) -> list[SpendSnapshot]:
        """Snapshots on or after `since`, oldest first."""
        ...

    @abstractmethod
    def record_remediation(
        self,
        finding_id: str,
        playbook: str,
        dry_run: bool,
        result: str,
        detail: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """One row per attempt; dry-runs and retries are attempts too."""
        ...
