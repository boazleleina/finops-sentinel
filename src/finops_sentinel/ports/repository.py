from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, List, Optional

from finops_sentinel.domain.models import AuditEvent, Decision, Finding, FindingStatus, Resource


class FindingsRepository(ABC):
    """
    Port for persisting and retrieving resources, findings, and their
    satellite records (decisions, notifications, remediations, audit events).
    """

    @abstractmethod
    def upsert_resource(self, resource: Resource) -> None:
        ...

    @abstractmethod
    def get_resource_by_id(self, resource_id: str) -> Optional[Resource]:
        ...

    @abstractmethod
    def get_all_resources(self) -> List[Resource]:
        ...

    @abstractmethod
    def mark_unseen_resources_deleted(self, cutoff_time: datetime) -> None:
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
    def get_findings(self, status: Optional[FindingStatus] = None) -> List[Finding]:
        ...

    @abstractmethod
    def get_finding_by_id(self, finding_id: str) -> Optional[Finding]:
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
    def get_audit_events(self, finding_id: Optional[str] = None) -> List[AuditEvent]:
        ...

    @abstractmethod
    def record_notification(
        self, finding_id: str, channel: str, message_ref: Optional[str], sent_at: datetime
    ) -> None:
        ...

    @abstractmethod
    def get_latest_notification_time(self, finding_id: str) -> Optional[datetime]:
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
