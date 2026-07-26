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
PLAYBOOK_ALLOWLIST: dict[ResourceType, str] = {
    ResourceType.EBS_VOLUME: "snapshot_then_delete_volume",
    ResourceType.ELASTIC_IP: "release_eip",
    ResourceType.EC2_INSTANCE: "terminate_stopped_instance",
    ResourceType.EBS_SNAPSHOT: "delete_ebs_snapshot",
}

# Rules whose findings are inferred from metrics rather than observed state.
# Low CPU is evidence, not proof: a warm standby, a batch host between runs,
# or a license server all look idle. These findings are reported for humans
# to act on out-of-band and are NEVER remediable, even when their resource
# type has a playbook — without this gate an ec2_idle finding on a RUNNING
# instance would inherit terminate_stopped_instance from the type allowlist.
NOTIFY_ONLY_RULES: frozenset[str] = frozenset({"ec2_idle"})


def is_remediable(rule: str) -> bool:
    """False for metric-inferred rules, which are advisory only."""
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
