"""Deterministic finding summaries. Pure domain logic — no framework imports.

This is the floor the Advisor port promises: whatever happens to the LLM
backend, every finding can still be explained. The OllamaAdvisor falls back
here on timeout, transport error, or schema violation, and TemplateAdvisor
uses it as its only implementation.
"""
from typing import Any

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
    "rds_idle": (
        "Nothing has connected to this database for the whole observation "
        "window, so it is billing for compute and storage while serving no "
        "traffic. This agent will not touch a database: confirm it is not a "
        "replica, a failover target, or a restore staging point, then snapshot "
        "and delete it yourself."
    ),
    "s3_no_lifecycle": (
        "This bucket has no lifecycle policy, so nothing ever transitions to "
        "cheaper storage or expires — objects and old versions bill at full "
        "rate indefinitely. The cost shown is a fraction of the bucket's total, "
        "since only some of it will be cold enough to tier. Add a lifecycle "
        "policy; this agent will not delete your objects."
    ),
    "s3_incomplete_multipart": (
        "Multipart uploads in this bucket were abandoned part-way and are "
        "billing for the parts already stored. No object was ever created, so "
        "nothing in the console shows them and nothing depends on them. "
        "Aborting the uploads reclaims the space without deleting any object."
    ),
    "rds_stopped": (
        "This database is stopped, which is not the saving it looks like — "
        "allocated storage bills at the full rate while the engine is down, and "
        "AWS restarts a stopped RDS instance automatically after 7 days, so the "
        "compute charge returns on its own. Take a final snapshot and delete it "
        "yourself if it is genuinely finished with."
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


# Narration topics. Every number these read was computed deterministically
# elsewhere; the copy only arranges it.
def _narrate_spend_anomaly(facts: dict[str, Any]) -> str:
    direction = facts.get("direction", "changed")
    verb = "above" if direction == "increase" else "below"
    return (
        f"Estimated monthly waste on {facts.get('date')} was "
        f"${facts.get('value')}, {facts.get('z_score')} standard deviations "
        f"{verb} the ${facts.get('mean')} average of the previous "
        f"{facts.get('window_days')} days. Worth checking what changed — a new "
        f"deployment, a stalled cleanup job, or simply more resources reaching "
        f"the age thresholds at once."
    )


def _narrate_rightsizing(facts: dict[str, Any]) -> str:
    return (
        f"{facts.get('count')} instance(s) look over-provisioned across "
        f"{facts.get('window_days')} days of metrics, worth about "
        f"${facts.get('total_saving')}/mo if every suggestion were taken. "
        f"These are advisory: peak utilisation, not average, drives the "
        f"suggestion, but only you know what headroom each workload needs."
    )


_NARRATORS = {
    "spend_anomaly": _narrate_spend_anomaly,
    "rightsizing": _narrate_rightsizing,
}


def render_template_narration(topic: str, facts: dict[str, Any]) -> str:
    """Deterministic prose for a digest section.

    The floor under Advisor.narrate, so — like render_template_summary — it
    must never raise. An unknown topic still produces something readable
    rather than an exception in the middle of a digest, and a missing fact
    renders as "None" rather than taking the whole notification down.
    """
    narrator = _NARRATORS.get(topic)
    if narrator is None:
        details = ", ".join(f"{key}: {value}" for key, value in sorted(facts.items()))
        return f"{topic.replace('_', ' ').capitalize()} — {details}"
    return narrator(facts)
