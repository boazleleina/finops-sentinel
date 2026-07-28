"""Guardrail rules. Pure domain logic — no framework imports.

These are the safety invariants from the spec (§2):
- Tag-based protection: anything tagged `finops:protected=true` is never actionable.
- Playbook allowlist: the agent can only remediate resource types with an
  explicit playbook entry here. No entry, no action.
"""
from typing import Any

from finops_sentinel.domain.models import ResourceType

PROTECTED_TAG_KEY = "finops:protected"

# The only remediations the system is allowed to execute, keyed by resource
# type. Adding a new remediable type requires an explicit entry here plus a
# playbook implementation in the cloud gateway.
#
# Keyed by resource type, which holds only because no two rules on the same
# type need DIFFERENT playbooks. S3 is the closest this has come to breaking:
# s3_incomplete_multipart is remediable and s3_no_lifecycle is not, and both sit
# on S3_BUCKET. NOTIFY_ONLY_RULES covers that case. If a second remediable rule
# ever lands on an existing type, this mapping has to become rule-keyed.
#
# RDS is absent on purpose — see NOTIFY_ONLY_RULES below.
PLAYBOOK_ALLOWLIST: dict[ResourceType, str] = {
    ResourceType.EBS_VOLUME: "snapshot_then_delete_volume",
    ResourceType.ELASTIC_IP: "release_eip",
    ResourceType.EC2_INSTANCE: "terminate_stopped_instance",
    ResourceType.EBS_SNAPSHOT: "delete_ebs_snapshot",
    # Deletes no object: an incomplete upload never became one. It discards
    # orphaned parts that bill at full storage rate and that nothing lists.
    ResourceType.S3_BUCKET: "abort_incomplete_multipart_uploads",
}

# Rules the system reports but will never act on. Two reasons land a rule here.
#
# Metric-inferred: low CPU is evidence, not proof — a warm standby, a batch
# host between runs, or a license server all look idle. Without this gate an
# ec2_idle finding on a RUNNING instance would inherit
# terminate_stopped_instance from the type-keyed allowlist below.
#
# Too destructive for v1: the RDS rules are state- and metric-based
# respectively, and both are perfectly actionable — by a human. Deleting a
# database, even with a final snapshot, is the largest irreversible action in
# this system's reach, so RDS ships with no playbook at all. Listing the rules
# here as well is belt and braces: the allowlist gate alone would refuse them,
# but only after Slack had already offered an Approve button the domain
# intends to reject.
# Not measurable enough to act on: s3_no_lifecycle reports a bounded FRACTION
# of a bucket's cost, because a bucket without a policy is not wholly waste.
# Its resource type does carry a playbook — the one belonging to its sibling
# rule s3_incomplete_multipart — so without this entry the type-keyed allowlist
# would offer to abort uploads in answer to a missing lifecycle policy.
NOTIFY_ONLY_RULES: frozenset[str] = frozenset(
    {"ec2_idle", "rds_idle", "rds_stopped", "s3_no_lifecycle"}
)


def is_remediable(rule: str) -> bool:
    """False for advisory rules — metric-inferred, or deliberately hands-off."""
    return rule not in NOTIFY_ONLY_RULES


def is_protected(tags: dict[str, Any] | list[dict[str, Any]] | None) -> bool:
    """True if the tag set carries the protection marker.

    Accepts both the domain's dict form and AWS's native
    [{"Key": ..., "Value": ...}] list form so callers can check raw
    provider data as well as stored tags.
    """
    if not tags:
        return False

    if isinstance(tags, dict):
        return str(tags.get(PROTECTED_TAG_KEY, "")).lower() == "true"

    for tag in tags:
        if (
            isinstance(tag, dict)
            and tag.get("Key") == PROTECTED_TAG_KEY
            and str(tag.get("Value")).lower() == "true"
        ):
            return True
    return False
