"""Tests for the credentials an approved playbook runs under.

What is testable here: that Sentinel asks STS for the right thing — the right
role, a session that names the approver, an ExternalId, and a policy narrowed
to the one resource — and that a refusal surfaces as a failed remediation
rather than a silent skip.

What is not testable here, or in LocalStack: whether AWS would actually deny a
session policy it dislikes. LocalStack Community evaluates no IAM policies, so
a denial test there passes the deletion, which is worse than no test. The
denial path is exercised with a fake STS that raises, and the enforcement
itself has to be proven once against a real account (docs/iam-policies.md).
"""
import json

import pytest
from botocore.exceptions import ClientError

from finops_sentinel.adapters.aws.approval_credentials import (
    ApprovalCredentialError,
    AssumeRoleGatewayFactory,
    session_name,
    session_policy,
)
from finops_sentinel.bootstrap import get_approval_gateway_factory, get_authorizer
from finops_sentinel.config import settings
from finops_sentinel.domain.models import FindingStatus
from finops_sentinel.domain.services import ApprovalPlan, execute_approval
from tests.fakes import make_finding, make_resource

ROLE = "arn:aws:iam::123456789012:role/finops-approver"


def plan(playbook="snapshot_then_delete_volume", resource_id="vol-123", actor="U024BE7LH"):
    return ApprovalPlan(
        finding_id="f-mock",
        resource_id=resource_id,
        region="eu-west-1",
        playbook=playbook,
        actor=actor,
    )


class FakeSTS:
    """Records assume_role calls; can be told to deny."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {
            "Credentials": {
                "AccessKeyId": "ASIA-TEMP",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }


def test_session_policy_scopes_to_one_resource():
    policy = session_policy(plan(), account_id="123456789012")
    act = next(s for s in policy["Statement"] if s["Sid"] == "ActOnExactlyThisResource")

    assert "ec2:DeleteVolume" in act["Action"]
    assert "arn:aws:ec2:eu-west-1:123456789012:volume/vol-123" in act["Resource"]
    # The snapshot the playbook is about to create cannot be named yet.
    assert "arn:aws:ec2:eu-west-1::snapshot/*" in act["Resource"]
    # Nothing else in the account: a second volume is not in scope.
    assert not any("volume/vol-999" in arn for arn in act["Resource"])
    # Describe calls cannot be resource-scoped, and are read-only.
    read = next(s for s in policy["Statement"] if s["Sid"] == "ReadWhatThePlaybookMustCheck")
    assert read["Resource"] == "*"
    assert all(action.startswith(("ec2:Describe", "s3:List")) for action in read["Action"])


@pytest.mark.parametrize(
    ("playbook", "resource_id", "expected"),
    [
        ("release_eip", "eipalloc-1", "arn:aws:ec2:eu-west-1:123456789012:elastic-ip/eipalloc-1"),
        ("terminate_stopped_instance", "i-1", "arn:aws:ec2:eu-west-1:123456789012:instance/i-1"),
        ("delete_ebs_snapshot", "snap-1", "arn:aws:ec2:eu-west-1:123456789012:snapshot/snap-1"),
        ("abort_incomplete_multipart_uploads", "my-bucket", "arn:aws:s3:::my-bucket"),
    ],
)
def test_every_playbook_scopes_to_its_own_resource(playbook, resource_id, expected):
    policy = session_policy(plan(playbook, resource_id), account_id="123456789012")
    act = next(s for s in policy["Statement"] if s["Sid"] == "ActOnExactlyThisResource")
    assert expected in act["Resource"]


def test_unknown_playbook_gets_no_credentials():
    """Fails closed: a playbook added without a grant entry cannot run."""
    with pytest.raises(ApprovalCredentialError):
        session_policy(plan(playbook="delete_everything"), account_id="123456789012")


def test_session_name_is_cloudtrail_safe():
    assert session_name("U024BE7LH") == "sentinel-U024BE7LH"
    # Display names reach here if someone maps one; STS rejects most of them.
    assert session_name("boaz leleina!") == "sentinel-boaz-leleina"
    assert session_name("") == "sentinel-unknown"


def test_assume_role_request_names_the_approver_and_the_resource():
    sts = FakeSTS()
    factory = AssumeRoleGatewayFactory(
        role_for_actor={"U024BE7LH": ROLE},
        external_id="sentinel-prod",
        session_duration_seconds=900,
        sts_client=sts,
    )

    factory(plan())

    (request,) = sts.calls
    assert request["RoleArn"] == ROLE
    assert request["RoleSessionName"] == "sentinel-U024BE7LH"  # CloudTrail names the human
    assert request["ExternalId"] == "sentinel-prod"  # confused-deputy control
    assert request["DurationSeconds"] == 900
    policy = json.loads(request["Policy"])
    assert "arn:aws:ec2:eu-west-1:123456789012:volume/vol-123" in str(policy)


def test_gateway_uses_the_temporary_credentials(monkeypatch):
    built = {}

    class SpyGateway:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(
        "finops_sentinel.adapters.aws.gateway.Boto3Gateway", SpyGateway
    )
    factory = AssumeRoleGatewayFactory(role_for_actor={"U024BE7LH": ROLE}, sts_client=FakeSTS())

    factory(plan())

    assert built["aws_access_key_id"] == "ASIA-TEMP"
    assert built["aws_session_token"] == "token"  # temporary, not Sentinel's own
    assert built["region"] == "eu-west-1"


def test_unmapped_actor_gets_no_credentials():
    factory = AssumeRoleGatewayFactory(role_for_actor={"someone-else": ROLE}, sts_client=FakeSTS())

    with pytest.raises(ApprovalCredentialError):
        factory(plan())


def test_approver_without_a_role_cannot_approve():
    """Caught at the guardrail, not after the CAS.

    Finding out at credential time would strand the finding in FAILED for what
    is a configuration mistake, and would read in Slack as a broken playbook.
    """
    settings.sentinel_approvers = f"U024BE7LH={ROLE},no-role-mapped"
    settings.sentinel_assume_role = True

    authorizer = get_authorizer()

    assert authorizer.can_approve("U024BE7LH") is True
    assert authorizer.can_approve("no-role-mapped") is False

    # With assume-role off, the same entry is a plain allowlist member again.
    settings.sentinel_assume_role = False
    assert get_authorizer().can_approve("no-role-mapped") is True


def test_factory_choice_follows_the_setting():
    settings.sentinel_approvers = f"U024BE7LH={ROLE}"

    settings.sentinel_assume_role = True
    assert isinstance(get_approval_gateway_factory(), AssumeRoleGatewayFactory)

    # Off: Sentinel's own credentials, which is what the demo and LocalStack use.
    settings.sentinel_assume_role = False
    assert not isinstance(get_approval_gateway_factory(), AssumeRoleGatewayFactory)


def test_denied_assume_role_is_recorded_as_a_failed_remediation(repository):
    """What an approver whose role cannot delete should experience.

    The finding does not strand in APPROVED, and the audit log says the
    credentials were refused rather than that the resource was missing.
    """
    repository.upsert_resource(make_resource())
    repository.save_finding(make_finding(status=FindingStatus.APPROVED))
    denial = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform sts:AssumeRole"}},
        "AssumeRole",
    )
    factory = AssumeRoleGatewayFactory(
        role_for_actor={"U024BE7LH": ROLE}, sts_client=FakeSTS(error=denial)
    )

    with pytest.raises(ClientError):
        execute_approval(plan(resource_id="vol-123"), repository, factory, dry_run=False)

    assert repository.get_finding_by_id("f-mock").status == FindingStatus.FAILED
    failure = next(
        e for e in repository.get_audit_events("f-mock") if e.event == "remediation_failed"
    )
    assert "AccessDenied" in failure.detail["error"]
