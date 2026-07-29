"""Notifier adapters: Block Kit shape, advisory copy, and the button gate.

The button gate is the safety-relevant part: a notify-only finding must not
render an Approve button, because domain.services.approve_finding refuses it.
An offered button that always fails is worse than no button.
"""
import logging
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from finops_sentinel.adapters.notifications.console import ConsoleNotifier
from finops_sentinel.adapters.notifications.slack import SlackAdapter
from finops_sentinel.config import settings
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from tests.fakes import make_finding as base_make_finding


@pytest.fixture
def resource():
    now = datetime.now(UTC)
    return Resource(
        id="res-1",
        resource_id="i-abc123",
        resource_type=ResourceType.EC2_INSTANCE,
        resource_arn="arn",
        region="us-east-1",
        current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
    )


def make_finding(rule: str, summary: str | None = None) -> Finding:
    """This module's defaults over the shared constructor in tests.fakes."""
    return base_make_finding(
        finding_id=f"{rule}|i-abc123",
        status=FindingStatus.OPEN,
        resource_ref="res-1",
        rule=rule,
        llm_summary=summary,
        est_monthly_cost_usd=Decimal("70.08"),
    )


def send_and_capture(finding, resource):
    """Send an alert through a mocked webhook and return the blocks sent."""
    settings.slack_webhook_url = "https://hooks.slack.test/T/B/X"
    try:
        with patch(
            "finops_sentinel.adapters.notifications.slack.WebhookClient"
        ) as client_cls:
            client_cls.return_value.send.return_value = MagicMock(status_code=200, body="ok")
            SlackAdapter().send_finding_alert(finding, resource)
            return client_cls.return_value.send.call_args.kwargs["blocks"]
    finally:
        settings.slack_webhook_url = None


def block_types(blocks):
    return [block["type"] for block in blocks]


def send_and_capture_kwargs(finding, resource):
    """Like send_and_capture, but returns every kwarg the webhook received."""
    settings.slack_webhook_url = "https://hooks.slack.test/T/B/X"
    try:
        with patch(
            "finops_sentinel.adapters.notifications.slack.WebhookClient"
        ) as client_cls:
            client_cls.return_value.send.return_value = MagicMock(status_code=200, body="ok")
            SlackAdapter().send_finding_alert(finding, resource)
            return client_cls.return_value.send.call_args.kwargs
    finally:
        settings.slack_webhook_url = None


def test_alert_names_the_region(resource):
    """With several regions scanned, the same finding shape recurs in each —
    without the region an approver cannot tell them apart."""
    resource.region = "ap-southeast-2"

    kwargs = send_and_capture_kwargs(make_finding("ec2_stopped"), resource)

    assert "ap-southeast-2" in kwargs["blocks"][0]["text"]["text"]
    # Also in the fallback text: that is all a lock-screen notification shows.
    assert "ap-southeast-2" in kwargs["text"]


def test_console_alert_names_the_region(resource, caplog):
    resource.region = "eu-west-1"

    with caplog.at_level(logging.INFO):
        ConsoleNotifier().send_finding_alert(make_finding("ec2_stopped"), resource)

    assert "eu-west-1" in caplog.text


def test_remediable_finding_gets_approve_and_deny_buttons(resource):
    blocks = send_and_capture(make_finding("ec2_stopped"), resource)

    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    action_ids = [e["action_id"] for e in actions[0]["elements"]]
    assert action_ids == ["approve_remediation", "deny_remediation"]
    assert "Waste Detected" in blocks[0]["text"]["text"]


def test_notify_only_finding_gets_no_buttons(resource):
    """ec2_idle is in NOTIFY_ONLY_RULES — approve_finding would refuse it."""
    blocks = send_and_capture(make_finding("ec2_idle"), resource)

    assert "actions" not in block_types(blocks)
    assert "Advisory" in blocks[0]["text"]["text"]
    assert any("Advisory only" in str(b) for b in blocks)


def test_advisor_summary_is_rendered_in_its_own_block(resource):
    summary = "Averaged 0.4% CPU over 14 days."
    blocks = send_and_capture(make_finding("ec2_stopped", summary=summary), resource)

    contexts = [b for b in blocks if b["type"] == "context"]
    assert any(summary in e["text"] for b in contexts for e in b["elements"])
    # Never folded into a button value — model output must not steer actions.
    actions = [b for b in blocks if b["type"] == "actions"]
    assert all(summary not in e["value"] for e in actions[0]["elements"])


def test_missing_summary_omits_the_block(resource):
    blocks = send_and_capture(make_finding("ec2_stopped"), resource)

    assert block_types(blocks) == ["section", "actions"]


def test_slack_requires_a_webhook(resource):
    settings.slack_webhook_url = None

    with pytest.raises(RuntimeError, match="SLACK_WEBHOOK_URL"):
        SlackAdapter().send_finding_alert(make_finding("ec2_stopped"), resource)


def test_slack_raises_on_non_200(resource):
    settings.slack_webhook_url = "https://hooks.slack.test/T/B/X"
    try:
        with patch(
            "finops_sentinel.adapters.notifications.slack.WebhookClient"
        ) as client_cls:
            client_cls.return_value.send.return_value = MagicMock(
                status_code=403, body="invalid_token"
            )
            with pytest.raises(RuntimeError, match="403"):
                SlackAdapter().send_finding_alert(make_finding("ec2_stopped"), resource)
    finally:
        settings.slack_webhook_url = None


def test_console_notifier_marks_advisory_findings(resource, caplog):
    with caplog.at_level(logging.INFO):
        ConsoleNotifier().send_finding_alert(make_finding("ec2_idle"), resource)

    assert "Advisory only" in caplog.text
    assert "POST /decisions" not in caplog.text


def test_console_notifier_points_remediable_findings_at_the_api(resource, caplog):
    with caplog.at_level(logging.INFO):
        ConsoleNotifier().send_finding_alert(
            make_finding("ec2_stopped", summary="Stopped 30 days."), resource
        )

    assert "POST /decisions/ec2_stopped|i-abc123" in caplog.text
    assert "Stopped 30 days." in caplog.text
