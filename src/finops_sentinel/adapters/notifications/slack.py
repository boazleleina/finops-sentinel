import json
import logging
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any, Literal, Mapping, Optional

from slack_sdk.signature import SignatureVerifier
from slack_sdk.webhook import WebhookClient

from finops_sentinel.config import settings
from finops_sentinel.domain.models import Decision, Finding, Resource
from finops_sentinel.ports.notifier import Notifier

logger = logging.getLogger(__name__)

CALLBACK_MAX_AGE_SECONDS = 60 * 5


class SlackAdapter(Notifier):
    """
    Implements the Notifier port for Slack.

    Outbound: incoming-webhook messages with Block Kit Approve/Deny buttons.
    Inbound: parse_callback verifies the signing secret and turns the
    interaction payload into a domain Decision. All Slack-specific knowledge
    (signatures, form encoding, response_url) stays inside this adapter.
    """

    @property
    def channel_name(self) -> str:
        return "slack"

    def send_finding_alert(self, finding: Finding, resource: Resource) -> Optional[str]:
        webhook_url = settings.slack_webhook_url
        if not webhook_url:
            raise RuntimeError("SLACK_WEBHOOK_URL is not configured")

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🚨 *FinOps Alert: Waste Detected*\n\n"
                        f"*Rule:* {finding.rule}\n"
                        f"*Resource:* `{resource.resource_id}` ({resource.resource_type})\n"
                        f"*Cost Impact:* ${finding.est_monthly_cost_usd}/mo"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve Remediation"},
                        "style": "primary",
                        "value": f"approve_{finding.id}",
                        "action_id": "approve_remediation",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "value": f"deny_{finding.id}",
                        "action_id": "deny_remediation",
                    },
                ],
            },
        ]

        response = WebhookClient(webhook_url).send(
            text=f"FinOps Alert: {finding.rule} on {resource.resource_id}",
            blocks=blocks,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Slack webhook returned {response.status_code}: {response.body}")

        logger.info("Sent Slack alert for finding %s", finding.id)
        # Incoming webhooks return no message timestamp; edits happen via the
        # interaction payload's response_url instead.
        return None

    def parse_callback(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> tuple[Decision, dict[str, Any]]:
        self._verify_signature(raw_body, headers)

        form = urllib.parse.parse_qs(raw_body.decode("utf-8"))
        payload_values = form.get("payload")
        if not payload_values:
            raise ValueError("No payload found")

        try:
            payload = json.loads(payload_values[0])
        except json.JSONDecodeError as exc:
            raise ValueError("Payload is not valid JSON") from exc

        actions = payload.get("actions") or []
        if not actions:
            raise ValueError("No actions in payload")

        value = str(actions[0].get("value", ""))
        action: Literal["approve", "deny"]
        if value.startswith("approve_"):
            action, finding_id = "approve", value.removeprefix("approve_")
        elif value.startswith("deny_"):
            action, finding_id = "deny", value.removeprefix("deny_")
        else:
            raise ValueError(f"Unrecognized action value: {value!r}")

        actor = payload.get("user", {}).get("username") or payload.get("user", {}).get(
            "id", "unknown"
        )

        decision = Decision(
            finding_id=finding_id,
            actor=actor,
            action=action,
            decided_at=datetime.now(UTC),
            channel=self.channel_name,
        )
        reply_context = {
            "response_url": payload.get("response_url"),
            "original_blocks": payload.get("message", {}).get("blocks", []),
        }
        return decision, reply_context

    def confirm_decision(self, reply_context: dict[str, Any], text: str) -> None:
        response_url = reply_context.get("response_url")
        if not response_url:
            return

        blocks: list[dict[str, Any]] = []
        original_blocks = reply_context.get("original_blocks") or []
        if original_blocks:
            blocks.append(original_blocks[0])  # keep the alert text, drop the buttons
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        )

        response = WebhookClient(response_url).send(
            text=text, blocks=blocks, replace_original=True
        )
        if response.status_code != 200:
            logger.error("Failed to update Slack message: %s", response.body)

    def _verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        secret = settings.slack_signing_secret
        if not secret:
            # No secret configured — verification bypassed (local testing).
            return

        timestamp = headers.get("x-slack-request-timestamp") or headers.get(
            "X-Slack-Request-Timestamp"
        )
        signature = headers.get("x-slack-signature") or headers.get("X-Slack-Signature")
        if not timestamp or not signature:
            raise PermissionError("Missing Slack signature headers")

        try:
            age = abs(time.time() - int(timestamp))
        except ValueError as exc:
            raise PermissionError("Invalid Slack timestamp header") from exc
        if age > CALLBACK_MAX_AGE_SECONDS:
            raise PermissionError("Slack request timestamp expired")

        verifier = SignatureVerifier(secret)
        if not verifier.is_valid(body=raw_body, timestamp=timestamp, signature=signature):
            raise PermissionError("Invalid Slack signature")
