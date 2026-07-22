from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional

from finops_sentinel.domain.models import Decision, Finding, Resource


class Notifier(ABC):
    """
    Port for sending outbound notifications and parsing inbound decision
    callbacks. Speaks domain language only — transport details (webhooks,
    signatures, Block Kit) live in the adapter.
    """

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Channel identifier recorded on decisions/notifications, e.g. 'slack'."""
        ...  # pragma: no cover

    @abstractmethod
    def send_finding_alert(self, finding: Finding, resource: Resource) -> Optional[str]:
        """
        Send an interactive alert for a finding. Returns a message reference
        usable to edit the message later, or None if the transport has none.
        Raises on delivery failure so the caller can leave the finding
        un-notified and retry on the next scan.
        """
        ...  # pragma: no cover

    @abstractmethod
    def parse_callback(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> tuple[Decision, dict[str, Any]]:
        """
        Verify and parse an inbound callback into a Decision plus an opaque
        reply context for confirm_decision. Raises PermissionError on a bad
        signature and ValueError on a malformed payload.
        """
        ...  # pragma: no cover

    @abstractmethod
    def confirm_decision(self, reply_context: dict[str, Any], text: str) -> None:
        """Update the original message (or equivalent) with the decision outcome."""
        ...  # pragma: no cover
