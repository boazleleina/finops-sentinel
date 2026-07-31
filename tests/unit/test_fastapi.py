import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from slack_sdk.signature import SignatureVerifier

from finops_sentinel.adapters.inbound import fastapi_app
from finops_sentinel.adapters.persistence.sqlalchemy_repo import Base, SqlAlchemyRepository
from finops_sentinel.config import settings
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
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


def seed_notified_finding(repo, finding_id="f-123", region="us-east-1"):
    now = datetime.now(UTC)
    repo.upsert_resource(Resource(
        id="res-1", resource_id="vol-123", resource_type=ResourceType.EBS_VOLUME,
        resource_arn="arn", region=region, current_tags={},
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
    assert response.json()["message"] == "accepted"
    # Dry run: approved but not remediated
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.APPROVED
    # Two messages: the acknowledgement that removes the buttons, then the
    # outcome once the backgrounded playbook finished.
    ack, outcome = confirmations
    assert "running" in ack and "@boaz" in ack
    assert "DRY RUN" in outcome and "@boaz" in outcome


def test_slack_decision_reply_names_the_region(api_repo, monkeypatch):
    """"Approved" alone does not tell a multi-region approver which corner of
    the account just changed."""
    seed_notified_finding(api_repo, region="eu-west-1")
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
    assert "eu-west-1" in confirmations[0]


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
    # The click is acknowledged before the playbook runs, so the HTTP response
    # cannot carry the failure — the second message does.
    assert response.json()["message"] == "accepted"
    assert "Remediation failed" in confirmations[-1]
    assert "InvalidVolume.NotFound" in confirmations[-1]
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


def test_slow_remediation_is_acknowledged_before_it_runs(api_repo, monkeypatch):
    """The buttons come off before the playbook starts, not after it ends.

    The EBS playbook waits on a snapshot before deleting, which is minutes —
    far outside Slack's three-second budget. A click arriving during that
    window (a double-click, or Slack's own retry after it times out) must find
    the finding already out of NOTIFIED and be refused, and the message must
    already say something.
    """
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    settings.dry_run = False

    confirmations = []
    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: confirmations.append(text),
    )

    replays = []

    def slow_playbook(self, playbook, resource_id, dry_run):
        # Mid-remediation: what a second click sees.
        replays.append(
            client.post(
                "/callbacks/slack",
                data={"payload": json.dumps(slack_payload("approve_f-123"))},
            ).json()
        )
        return {"snapshot_id": "snap-1", "deleted_volume": resource_id}

    monkeypatch.setattr(
        "finops_sentinel.adapters.aws.gateway.Boto3Gateway.execute", slow_playbook
    )

    response = client.post(
        "/callbacks/slack",
        data={"payload": json.dumps(slack_payload("approve_f-123"))},
    )

    assert response.status_code == 200
    ack, outcome = confirmations[0], confirmations[-1]
    assert "running" in ack  # posted before the playbook, so the buttons are gone
    assert "remediation executed" in outcome
    # The replay landed while the playbook was still running, and was refused.
    assert [r["message"] for r in replays] == ["rejected"]
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.REMEDIATED


def test_slack_callback_rejects_other_workspace(api_repo, monkeypatch):
    """A valid signature proves the app, not the install.

    The signing secret is app-level, so a second workspace the app is installed
    into produces perfectly signed approvals.
    """
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    settings.slack_team_id = "T_OURS"
    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: None,
    )

    payload = slack_payload("approve_f-123") | {"team": {"id": "T_THEIRS"}}
    response = client.post("/callbacks/slack", data={"payload": json.dumps(payload)})

    assert response.status_code == 401
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.NOTIFIED


def test_slack_callback_rejects_unlisted_channel(api_repo, monkeypatch):
    seed_notified_finding(api_repo)
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    settings.slack_allowed_channel_ids = "C_FINOPS"
    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.SlackAdapter.confirm_decision",
        lambda self, ctx, text: None,
    )

    payload = slack_payload("approve_f-123") | {"channel": {"id": "C_RANDOM"}}
    response = client.post("/callbacks/slack", data={"payload": json.dumps(payload)})

    assert response.status_code == 401

    payload = slack_payload("approve_f-123") | {"channel": {"id": "C_FINOPS"}}
    response = client.post("/callbacks/slack", data={"payload": json.dumps(payload)})

    assert response.status_code == 200


def test_unauthorized_actor_is_refused_and_named(api_repo):
    """Signature verification answers 'came through the app'. This answers
    'may this person delete infrastructure'."""
    seed_notified_finding(api_repo)
    settings.sentinel_approvers = "boaz,ops-oncall"

    response = client.post(
        "/decisions/f-123", json={"action": "approve", "actor": "intern"}
    )

    assert response.status_code == 409
    assert "not permitted to approve" in response.json()["detail"]
    assert api_repo.get_finding_by_id("f-123").status == FindingStatus.NOTIFIED
    assert any(e.event == "approve_blocked_unauthorized"
               for e in api_repo.get_audit_events("f-123"))

    # ...and the listed approver still gets through.
    response = client.post(
        "/decisions/f-123", json={"action": "approve", "actor": "boaz"}
    )
    assert response.status_code == 200


def test_notify_only_refusal_names_the_real_reason(api_repo):
    """The generic 'already decided, protected, or gone' hid the actual cause."""
    now = datetime.now(UTC)
    api_repo.upsert_resource(
        Resource(
            id="res-idle", resource_id="i-idle", resource_type=ResourceType.EC2_INSTANCE,
            resource_arn="arn", region="us-east-1", current_tags={},
            lifecycle=ResourceLifecycle.ACTIVE, first_seen_at=now, last_seen_at=now,
        )
    )
    api_repo.save_finding(
        Finding(
            id="ec2_idle|i-idle", resource_ref="res-idle", rule="ec2_idle", evidence={},
            tags_at_detection={}, est_monthly_cost_usd=Decimal("70.08"),
            status=FindingStatus.NOTIFIED, protected=False,
            detected_at=now, last_seen_at=now,
        )
    )

    response = client.post(
        "/decisions/ec2_idle|i-idle", json={"action": "approve", "actor": "boaz"}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "advisory only" in detail
    assert "ec2_idle" in detail
    assert "already decided" not in detail
