import json
import time
from datetime import datetime, UTC
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from slack_sdk.signature import SignatureVerifier

from finops_sentinel.adapters.inbound import fastapi_app
from finops_sentinel.adapters.persistence.sqlalchemy_repo import Base, SqlAlchemyRepository
from finops_sentinel.config import settings
from finops_sentinel.domain.models import (
    Finding, FindingStatus, Resource, ResourceLifecycle, ResourceType
)

client = TestClient(fastapi_app.app, raise_server_exceptions=False)


@pytest.fixture
def api_repo(tmp_path):
    """File-backed repo shared with the app via settings.sentinel_db_path."""
    db_path = tmp_path / "api.db"
    settings.sentinel_db_path = str(db_path)
    repo = SqlAlchemyRepository(f"sqlite:///{db_path}")
    Base.metadata.create_all(repo.engine)
    yield repo
    repo.engine.dispose()


def seed_notified_finding(repo, finding_id="f-123"):
    now = datetime.now(UTC)
    repo.upsert_resource(Resource(
        id="res-1", resource_id="vol-123", resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn", region="us-east-1", current_tags={},
        lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now
    ))
    repo.save_finding(Finding(
        id=finding_id, resource_ref="res-1", rule="ebs_unattached", evidence={},
        tags_at_detection={}, est_monthly_cost_usd=Decimal("5.00"),
        status=FindingStatus.NOTIFIED, protected=False,
        detected_at=now, last_seen_at=now
    ))


def slack_payload(value):
    return {
        "type": "block_actions",
        "user": {"username": "boaz"},
        "actions": [{"value": value}],
    }


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_findings_with_status_filter(api_repo):
    seed_notified_finding(api_repo)

    response = client.get("/findings")
    assert response.status_code == 200
    assert [f["id"] for f in response.json()] == ["f-123"]

    response = client.get("/findings", params={"status": "open"})
    assert response.json() == []


def test_get_resources(api_repo):
    seed_notified_finding(api_repo)
    response = client.get("/resources")
    assert response.status_code == 200
    assert [r["resource_id"] for r in response.json()] == ["vol-123"]


def test_post_decision_deny_and_audit(api_repo):
    seed_notified_finding(api_repo)

    response = client.post("/decisions/f-123", json={"action": "deny", "actor": "boaz"})
    assert response.status_code == 200
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.DENIED

    audit = client.get("/audit").json()
    assert any(e["event"] == "finding_denied" for e in audit)

    # Second decision on a terminal finding is rejected
    response = client.post("/decisions/f-123", json={"action": "approve", "actor": "boaz"})
    assert response.status_code == 409


def test_callback_missing_payload(api_repo):
    # Configured notifier is console (no webhook in tests) — its channel matches,
    # but it does not accept callbacks.
    response = client.post("/callbacks/console", data={})
    assert response.status_code == 400


def test_callback_unknown_channel(api_repo):
    response = client.post("/callbacks/telegram", data={})
    assert response.status_code == 404


def test_slack_callback_approve_dry_run(api_repo, monkeypatch):
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    settings.dry_run = True

    confirmations = []
    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: confirmations.append(text),
    )

    response = client.post(
        "/callbacks/slack",
        data={"payload": json.dumps(slack_payload("approve_f-123"))},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "ok"
    # Dry run: approved but not remediated
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.APPROVED
    assert confirmations and "DRY RUN" in confirmations[0]
    assert "@boaz" in confirmations[0]


def test_slack_callback_playbook_failure_replies_cleanly(api_repo, monkeypatch):
    """A playbook blow-up must edit the Slack message, not 500."""
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    settings.dry_run = False

    confirmations = []
    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: confirmations.append(text),
    )
    monkeypatch.setattr(
        "finops_sentinel.adapters.aws.gateway.Boto3Gateway.execute",
        lambda self, playbook, resource_id, dry_run: (_ for _ in ()).throw(
            RuntimeError("InvalidVolume.NotFound")
        ),
    )

    response = client.post(
        "/callbacks/slack",
        data={"payload": json.dumps(slack_payload("approve_f-123"))},
    )
    assert response.status_code == 200
    assert response.json()["message"] == "failed"
    assert confirmations and "Remediation failed" in confirmations[0]
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.FAILED


def test_slack_callback_deny_reply_names_actor(api_repo, monkeypatch):
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"

    confirmations = []
    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: confirmations.append(text),
    )

    response = client.post(
        "/callbacks/slack",
        data={"payload": json.dumps(slack_payload("deny_f-123"))},
    )
    assert response.status_code == 200
    assert confirmations
    assert "*Denied* by @boaz" in confirmations[0]
    assert "no action taken" in confirmations[0]


def test_slack_callback_signature_enforced(api_repo, monkeypatch):
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    settings.slack_signing_secret = "test-secret"

    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: None,
    )

    body = "payload=" + json.dumps(slack_payload("deny_f-123"), separators=(",", ":"))

    # Unsigned request rejected
    response = client.post(
        "/callbacks/slack",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 401

    # Correctly signed request accepted
    timestamp = str(int(time.time()))
    signature = SignatureVerifier("test-secret").generate_signature(
        timestamp=timestamp, body=body
    )
    response = client.post(
        "/callbacks/slack",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    assert response.status_code == 200
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.DENIED
