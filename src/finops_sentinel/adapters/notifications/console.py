import logging
from collections.abc import Mapping
from typing import Any

from finops_sentinel.domain.models import Decision, Finding, Resource
from finops_sentinel.domain.rules import is_remediable
from finops_sentinel.ports.notifier import Notifier

logger = logging.getLogger(__name__)


class ConsoleNotifier(Notifier):
    """
    Fallback Notifier when no Slack webhook is configured: logs the alert so
    the pipeline (OPEN → NOTIFIED → decision via API/CLI) still works without
    any external service.
    """

    @property
    def channel_name(self) -> str:
        return "console"

    def send_finding_alert(self, finding: Finding, resource: Resource) -> str | None:
        if is_remediable(finding.rule):
            next_step = f"Decide via POST /decisions/{finding.id}"
        else:
            next_step = "Advisory only — not remediable, review manually"
        logger.info(
            "FinOps alert: %s on %s (~$%s/mo). %s%s",
            finding.rule,
            resource.resource_id,
            finding.est_monthly_cost_usd,
            next_step,
            f"\n  {finding.llm_summary}" if finding.llm_summary else "",
        )
        return None

    def parse_callback(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> tuple[Decision, dict[str, Any]]:
        raise ValueError("Console notifier does not accept callbacks")

    def confirm_decision(self, reply_context: dict[str, Any], text: str) -> None:
        logger.info("Decision outcome: %s", text)
