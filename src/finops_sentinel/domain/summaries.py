"""Deterministic finding summaries. Pure domain logic — no framework imports.

This is the floor the Advisor port promises: whatever happens to the LLM
backend, every finding can still be explained. The OllamaAdvisor falls back
here on timeout, transport error, or schema violation, and TemplateAdvisor
uses it as its only implementation.
"""
from finops_sentinel.domain.models import Finding, Resource

# Per-rule copy. A rule with no entry still gets a usable sentence from
# _GENERIC, so adding a scanner never leaves an unexplained finding.
_RULE_COPY: dict[str, str] = {
    "ebs_unattached": (
        "This EBS volume is not attached to any instance but still bills for "
        "provisioned capacity every hour. Confirm nothing needs the data, then "
        "snapshot and delete it."
    ),
    "eip_orphaned": (
        "This Elastic IP is allocated but not associated with a running "
        "instance, and AWS charges hourly for idle public IPv4 addresses. "
        "Release it unless something is waiting to claim the address."
    ),
    "ec2_stopped": (
        "This EC2 instance has been stopped long enough that it is almost "
        "certainly abandoned. Compute is not billed while stopped, but its EBS "
        "root volume is. Terminate it once you are sure the data is not needed."
    ),
    "ebs_snapshot_old": (
        "This EBS snapshot is older than the retention threshold and is still "
        "accruing storage charges. Delete it if a newer snapshot or backup "
        "already covers this volume."
    ),
    "ec2_idle": (
        "This EC2 instance is running but its CPU and network activity have "
        "been near zero for the whole observation window, so you are paying "
        "full price for an instance doing nothing. Investigate before acting: "
        "low metrics can also mean a warm standby or a batch host between runs."
    ),
}

_GENERIC = (
    "This resource was flagged as likely waste by the {rule} rule and is "
    "billing while unused. Review the evidence before acting."
)


def render_template_summary(finding: Finding, resource: Resource) -> str:
    """Build a deterministic summary for a finding.

    Used as the LLM fallback, so it must stay dependency-free and must never
    raise for any well-formed Finding.
    """
    body = _RULE_COPY.get(finding.rule) or _GENERIC.format(rule=finding.rule)
    return (
        f"{resource.resource_type} {resource.resource_id} in {resource.region} — "
        f"est. ${finding.est_monthly_cost_usd}/mo. {body}"
    )
