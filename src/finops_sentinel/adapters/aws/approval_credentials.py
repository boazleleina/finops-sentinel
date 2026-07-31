"""Credentials for a single approved remediation.

Sentinel's own identity is read-only. When a playbook runs, it runs under a
role assumed on behalf of the approver, with a session policy narrowed to the
one resource that approval named — so the deletion is authorized by AWS
against that principal, not by Sentinel's opinion of a username, and CloudTrail
records who it was.

Two limits worth stating plainly:

- The actor -> role mapping is Sentinel *asserting* an identity. AWS enforces
  what the resulting session may do, but it never sees the Slack user, so the
  chain is only as strong as the Slack account and the signature check in front
  of it. This is better than an app-level name list; it is not the approver
  authenticating to AWS.
- LocalStack Community does not evaluate IAM policies, so an assume-role run
  there proves the wiring and nothing about enforcement. SENTINEL_ASSUME_ROLE
  defaults to false for exactly that reason (see bootstrap).
"""
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import boto3

from finops_sentinel.domain.services import ApprovalPlan
from finops_sentinel.ports.cloud import CloudGateway

logger = logging.getLogger(__name__)

# Session names must match [\w+=,.@-]{2,64}; Slack ids are safe, display names
# are not, and an invalid name fails the AssumeRole call rather than the
# approval, which would be an unhelpful place to find out.
_SESSION_NAME_ALLOWED = re.compile(r"[^\w+=,.@-]")

# What each playbook is allowed to do, and to what. Two statements per
# playbook: the calls that cannot be resource-scoped (EC2 Describe* does not
# support resource-level permissions) and the calls that can.
#
# Keyed by playbook rather than by resource type because the session is scoped
# to an *action*, not to a category. Adding a playbook without adding an entry
# here fails closed: no grant, no credentials, no remediation.
_READ_ONLY_BY_PLAYBOOK: dict[str, list[str]] = {
    "snapshot_then_delete_volume": ["ec2:DescribeVolumes", "ec2:DescribeSnapshots"],
    "release_eip": ["ec2:DescribeAddresses"],
    "terminate_stopped_instance": ["ec2:DescribeInstances", "ec2:DescribeInstanceStatus"],
    "delete_ebs_snapshot": ["ec2:DescribeSnapshots"],
    "abort_incomplete_multipart_uploads": ["s3:ListBucketMultipartUploads"],
}

_MUTATING_BY_PLAYBOOK: dict[str, list[str]] = {
    # CreateSnapshot is granted on the volume *and* on the snapshot that does
    # not exist yet, which is why the resource list below adds snapshot/*.
    "snapshot_then_delete_volume": ["ec2:CreateSnapshot", "ec2:CreateTags", "ec2:DeleteVolume"],
    "release_eip": ["ec2:ReleaseAddress"],
    "terminate_stopped_instance": ["ec2:TerminateInstances"],
    "delete_ebs_snapshot": ["ec2:DeleteSnapshot"],
    "abort_incomplete_multipart_uploads": [
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
    ],
}


class ApprovalCredentialError(RuntimeError):
    """No credentials could be issued for this approval."""


def _account_id(role_arn: str) -> str:
    # arn:aws:iam::123456789012:role/finops-approver
    parts = role_arn.split(":")
    if len(parts) < 6 or not parts[4]:
        raise ApprovalCredentialError(f"Cannot read an account id from role ARN {role_arn!r}")
    return parts[4]


def resource_arns(plan: ApprovalPlan, account_id: str) -> list[str]:
    """The ARNs this approval's session policy may touch.

    Built here rather than read from Resource.resource_arn: the scanners record
    a display ARN with a literal "account" segment, which is fine for a Slack
    message and useless in a policy. A security decision does not get to run on
    a placeholder.
    """
    region, resource_id = plan.region, plan.resource_id
    ec2 = f"arn:aws:ec2:{region}:{account_id}"

    if plan.playbook == "snapshot_then_delete_volume":
        # The snapshot is created by this call, so it cannot be named yet.
        return [f"{ec2}:volume/{resource_id}", f"arn:aws:ec2:{region}::snapshot/*"]
    if plan.playbook == "delete_ebs_snapshot":
        return [f"{ec2}:snapshot/{resource_id}"]
    if plan.playbook == "release_eip":
        return [f"{ec2}:elastic-ip/{resource_id}"]
    if plan.playbook == "terminate_stopped_instance":
        return [f"{ec2}:instance/{resource_id}"]
    if plan.playbook == "abort_incomplete_multipart_uploads":
        return [f"arn:aws:s3:::{resource_id}", f"arn:aws:s3:::{resource_id}/*"]

    raise ApprovalCredentialError(f"No session policy defined for playbook {plan.playbook!r}")


def session_policy(plan: ApprovalPlan, account_id: str) -> dict[str, Any]:
    """An inline policy granting exactly this approval and nothing else.

    Intersected with the role's own permissions by STS, so it can only narrow.
    That is the point: the approver's role may be written broadly, and the
    session that runs the deletion is still one action on one resource for a
    quarter of an hour.
    """
    if plan.playbook not in _MUTATING_BY_PLAYBOOK:
        raise ApprovalCredentialError(f"No session policy defined for playbook {plan.playbook!r}")

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadWhatThePlaybookMustCheck",
                "Effect": "Allow",
                "Action": _READ_ONLY_BY_PLAYBOOK[plan.playbook],
                "Resource": "*",
            },
            {
                "Sid": "ActOnExactlyThisResource",
                "Effect": "Allow",
                "Action": _MUTATING_BY_PLAYBOOK[plan.playbook],
                "Resource": resource_arns(plan, account_id),
            },
        ],
    }


def session_name(actor: str) -> str:
    """CloudTrail's record of who approved. Sanitised, never empty."""
    cleaned = _SESSION_NAME_ALLOWED.sub("-", actor)[:50].strip("-")
    return f"sentinel-{cleaned or 'unknown'}"


class AssumeRoleGatewayFactory:
    """Builds a CloudGateway whose credentials belong to the approver.

    Called with the ApprovalPlan produced by commit_approval, so every session
    is issued after the decision is committed and for exactly one resource.
    """

    def __init__(
        self,
        role_for_actor: Mapping[str, str],
        external_id: str | None = None,
        session_duration_seconds: int = 900,
        endpoint_url: str | None = None,
        mpu_age_days: int = 7,
        sts_client: Any | None = None,
    ):
        self._role_for_actor = dict(role_for_actor)
        self._external_id = external_id
        self._session_duration_seconds = session_duration_seconds
        self._endpoint_url = endpoint_url
        self._mpu_age_days = mpu_age_days
        self._sts = sts_client

    def _sts_client(self, region: str) -> Any:
        if self._sts is None:
            self._sts = boto3.client("sts", region_name=region, endpoint_url=self._endpoint_url)
        return self._sts

    def __call__(self, plan: ApprovalPlan) -> CloudGateway:
        # Imported here: the gateway module imports boto3 and the scanners, and
        # this module is imported by bootstrap before either is needed.
        from finops_sentinel.adapters.aws.gateway import Boto3Gateway

        role_arn = self._role_for_actor.get(plan.actor)
        if not role_arn:
            # Should be unreachable — the authorizer is built from the same
            # mapping, so an actor with no role never gets an approval
            # committed. Kept because "unreachable" and "unenforced" are
            # different things, and this one issues credentials.
            raise ApprovalCredentialError(
                f"No approver role mapped for actor {plan.actor!r}; refusing to issue credentials"
            )

        account_id = _account_id(role_arn)
        request: dict[str, Any] = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name(plan.actor),
            "DurationSeconds": self._session_duration_seconds,
            "Policy": json.dumps(session_policy(plan, account_id)),
        }
        if self._external_id:
            request["ExternalId"] = self._external_id

        logger.info(
            "Assuming %s for %s to run %s on %s",
            role_arn,
            plan.actor,
            plan.playbook,
            plan.resource_id,
        )
        credentials = self._sts_client(plan.region).assume_role(**request)["Credentials"]

        return Boto3Gateway(
            region=plan.region,
            endpoint_url=self._endpoint_url,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            mpu_age_days=self._mpu_age_days,
        )
