import json
import logging
import time
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from slack_sdk.signature import SignatureVerifier
from slack_sdk.webhook import WebhookClient

from finops_sentinel.config import settings
from finops_sentinel.domain.models import Decision, Finding, Resource
from finops_sentinel.domain.rules import is_remediable
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

    def send_finding_alert(self, finding: Finding, resource: Resource) -> str | None:
        webhook_url = settings.slack_webhook_url
        if not webhook_url:
            raise RuntimeError("SLACK_WEBHOOK_URL is not configured")

        remediable = is_remediable(finding.rule)
        header = (
            "🚨 *FinOps Alert: Waste Detected*"
            if remediable
            else "📊 *FinOps Advisory: Possible Idle Resource*"
        )

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{header}\n\n"
                        f"*Rule:* {finding.rule}\n"
                        f"*Resource:* `{resource.resource_id}` ({resource.resource_type})\n"
                        # Region is not decoration: with several regions
                        # scanned, it is the first thing an approver needs to
                        # know where to look, and remediation runs there.
                        f"*Region:* `{resource.region}`\n"
                        f"*Cost Impact:* ${finding.est_monthly_cost_usd}/mo"
                    ),
                },
            }
        ]

        if finding.llm_summary:
            # Advisor output is untrusted display copy (it summarizes
            # user-controlled tags), so it goes in its own context block and is
            # never used to build action values.
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": finding.llm_summary}],
                }
            )

        if remediable:
            blocks.append(
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
                }
            )
        else:
            # Metric-inferred: no playbook is allowed to run, so offering an
            # Approve button would promise an action the domain refuses.
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                "_Advisory only — inferred from CloudWatch metrics. "
                                "No automated remediation is available for this rule._"
                            ),
                        }
                    ],
                }
            )

        response = WebhookClient(webhook_url).send(
            # Notification preview text — the region belongs here too, since
            # this is all a phone lock screen shows.
            text=(
                f"FinOps Alert: {finding.rule} on {resource.resource_id} "
                f"in {resource.region}"
            ),
            blocks=blocks,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Slack webhook returned {response.status_code}: {response.body}")

        logger.info("Sent Slack alert for finding %s", finding.id)
        # Incoming webhooks return no message timestamp; edits happen via the
        # interaction payload's response_url instead.
        return None

    def send_digest(self, title: str, sections: list[str]) -> str | None:
        """Post an advisory digest — header and sections, and no buttons.

        The absence of an actions block is the contract, not an oversight. A
        digest describes patterns across many resources; there is no single
        finding id an Approve click could carry, and the domain would have
        nothing to transition. Anything actionable arrives via
        send_finding_alert instead.
        """
        webhook_url = settings.slack_webhook_url
        if not webhook_url:
            raise RuntimeError("SLACK_WEBHOOK_URL is not configured")

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": title}}
        ]
        for section in sections:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": section}}
            )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_Advisory digest — nothing here is actionable from Slack._",
                    }
                ],
            }
        )

        response = WebhookClient(webhook_url).send(text=title, blocks=blocks)
        if response.status_code != 200:
            raise RuntimeError(f"Slack webhook returned {response.status_code}: {response.body}")

        logger.info("Sent Slack digest: %s", title)
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

        self._verify_provenance(payload)

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

    def _verify_provenance(self, payload: dict[str, Any]) -> None:
        """Check the callback came from the workspace and channel we notified.

        The signature proves the request transited this app; it does not say
        which install produced it. Install the app in a second workspace, or
        widen the channel, and well-signed approvals start arriving from a
        population nobody enumerated. Slack-shaped by nature — a Telegram
        adapter would pin a chat id here instead — so it stays on this side of
        the port, unlike the actor authority check, which is in the domain.
        """
        expected_team = settings.slack_team_id
        if expected_team:
            team_id = (payload.get("team") or {}).get("id")
            if team_id != expected_team:
                raise PermissionError(f"Callback from unexpected Slack workspace: {team_id!r}")

        allowed_channels = settings.allowed_slack_channels
        if allowed_channels:
            channel_id = (payload.get("channel") or {}).get("id")
            if channel_id not in allowed_channels:
                raise PermissionError(f"Callback from unexpected channel: {channel_id!r}")

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
