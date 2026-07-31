"""Inbound FastAPI adapter. Routes are thin: parse input, call a domain
service, format output. Channel-specific callback logic (signatures, payload
shape) lives in the configured Notifier adapter, not here."""
import logging
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel

from finops_sentinel.bootstrap import (
    get_authorizer,
    get_cloud_gateway,
    get_notifier,
    get_repository,
)
from finops_sentinel.config import settings
from finops_sentinel.domain.models import AuditEvent, Finding, FindingStatus, Resource
from finops_sentinel.domain.rules import is_remediable
from finops_sentinel.domain.services import (
    ApprovalPlan,
    approve_finding,
    commit_approval,
    deny_finding,
    execute_approval,
)
from finops_sentinel.ports.notifier import Notifier

logger = logging.getLogger(__name__)

app = FastAPI(title="FinOps Sentinel API", version="0.1.0")


class DecisionRequest(BaseModel):
    action: Literal["approve", "deny"]
    actor: str = "api"


class DecisionResponse(BaseModel):
    finding_id: str
    action: str
    success: bool
    dry_run: bool


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/findings")
def list_findings(status: FindingStatus | None = None) -> list[Finding]:
    return get_repository().get_findings(status=status)


@app.get("/resources")
def list_resources() -> list[Resource]:
    return get_repository().get_all_resources()


@app.get("/audit")
def list_audit_events(finding_id: str | None = None) -> list[AuditEvent]:
    return get_repository().get_audit_events(finding_id=finding_id)


def _refusal_reason(finding_id: str, action: str) -> str:
    """Explain a refused decision.

    Notify-only rules are refused by the domain for a reason callers cannot
    guess from the generic message, and it is the one refusal a user can
    trigger deliberately — so name it.
    """
    repo = get_repository()
    events = repo.get_audit_events(finding_id=finding_id)
    if events and events[-1].event == "approve_blocked_unauthorized":
        actor = events[-1].detail.get("actor", "that account")
        return f"@{actor} is not permitted to approve remediations"

    finding = repo.get_finding_by_id(finding_id)
    if finding is not None and not is_remediable(finding.rule):
        return (
            f"Finding {finding_id} is advisory only: rule '{finding.rule}' is inferred from "
            "metrics and has no automated remediation. Act on it manually."
        )
    return (
        f"Finding {finding_id} cannot be {action}d (unknown, protected, or already decided)"
    )


def _finding_region(finding_id: str) -> str | None:
    """The region a finding's resource lives in, for decision replies.

    An approver scanning several regions needs the confirmation to say where
    the change landed — "Approved" alone does not tell them which account
    corner just changed.
    """
    repo = get_repository()
    finding = repo.get_finding_by_id(finding_id)
    if finding is None:
        return None
    resource = repo.get_resource_by_id(finding.resource_ref)
    return resource.region if resource else None


def _decide(finding_id: str, action: str, actor: str, channel: str) -> bool:
    repo = get_repository()
    if action == "approve":
        return approve_finding(
            finding_id,
            repo,
            # The resolver, not a gateway: the service picks the endpoint for
            # the finding's own region.
            get_cloud_gateway,
            actor=actor,
            channel=channel,
            dry_run=settings.dry_run,
            # Authority, checked in the domain: the notifier already proved the
            # request came through the app, which is a different question.
            authorizer=get_authorizer(),
        )
    return deny_finding(finding_id, repo, actor=actor, channel=channel)


@app.post("/decisions/{finding_id}")
def post_decision(finding_id: str, body: DecisionRequest) -> DecisionResponse:
    try:
        success = _decide(finding_id, body.action, actor=body.actor, channel="api")
    except Exception as exc:
        # Playbook failed mid-remediation; the service already recorded
        # FAILED plus the audit/remediation rows.
        raise HTTPException(
            status_code=502, detail=f"Remediation failed for {finding_id}: {exc}"
        ) from exc
    if not success:
        raise HTTPException(status_code=409, detail=_refusal_reason(finding_id, body.action))
    return DecisionResponse(
        finding_id=finding_id, action=body.action, success=True, dry_run=settings.dry_run
    )


def _run_remediation(
    plan: ApprovalPlan,
    notifier: Notifier,
    reply_context: dict[str, Any],
    actor: str,
    where: str,
) -> None:
    """Run an approved playbook after the callback has been acknowledged.

    Its own repository handle: the request that scheduled it is gone by the
    time this runs, and so is that handle's session.
    """
    try:
        execute_approval(plan, get_repository(), get_cloud_gateway, dry_run=settings.dry_run)
    except Exception as exc:  # a failed playbook must still update the message
        logger.exception("Remediation failed for %s", plan.finding_id)
        notifier.confirm_decision(
            reply_context,
            f"❌ *Remediation failed*{where} after approval by @{actor} — "
            f"`{plan.playbook}` did not complete ({exc}). See the audit log for details.",
        )
        return

    if settings.dry_run:
        outcome = f"✅ *Approved* by @{actor} — DRY RUN, no resources were changed{where}."
    else:
        outcome = f"✅ *Approved* by @{actor} — remediation executed{where}."
    notifier.confirm_decision(reply_context, outcome)


@app.post("/callbacks/{channel}")
async def notifier_callback(
    channel: str, request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """
    Webhook endpoint for interactive decision callbacks (e.g. Slack buttons).
    The raw payload is handed to the configured Notifier adapter, which
    verifies authenticity and parses it into a domain Decision.

    Approvals answer in two messages, because remediation is not fast: the EBS
    playbook waits on a snapshot before it deletes anything, and Slack expects
    a response in three seconds. Everything that decides — guardrails, the
    authority check, the CAS onto APPROVED — runs inline and is quick. Then the
    message is edited (which removes the buttons, closing the window a
    double-click or a Slack retry-after-timeout would arrive through) and the
    playbook runs in the background, editing the message again with the
    outcome.
    """
    notifier = get_notifier()
    if channel != notifier.channel_name:
        raise HTTPException(status_code=404, detail=f"No notifier for channel '{channel}'")

    raw_body = await request.body()
    try:
        decision, reply_context = notifier.parse_callback(raw_body, request.headers)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Resolved before the decision: a successful remediation may delete the
    # resource row's usefulness, and the reply should still name the region.
    region = _finding_region(decision.finding_id)
    where = f" in `{region}`" if region else ""

    if decision.action == "deny":
        # Nothing to execute — a denial is one state change and no cloud call.
        if deny_finding(
            decision.finding_id, get_repository(), actor=decision.actor, channel=channel
        ):
            outcome = f"🚫 *Denied* by @{decision.actor} — no action taken, finding closed."
            notifier.confirm_decision(reply_context, outcome)
            return {"message": "ok", "outcome": outcome}
        return _rejected(notifier, reply_context, decision.finding_id, "deny")

    plan = commit_approval(
        decision.finding_id,
        get_repository(),
        actor=decision.actor,
        channel=channel,
        # Authority, checked in the domain: the notifier already proved the
        # request came through the app, which is a different question.
        authorizer=get_authorizer(),
    )
    if plan is None:
        # Refused by a guardrail, or already decided. The buttons come off
        # either way: the finding is not in NOTIFIED any more, so nothing a
        # second click could do would be accepted.
        return _rejected(notifier, reply_context, decision.finding_id, "approve")

    # The finding is APPROVED in the database before this message goes out, so
    # the acknowledgement and the state that makes a replay a no-op land
    # together rather than a remediation apart.
    acknowledgement = (
        f"⏳ *Approved* by @{decision.actor} — running `{plan.playbook}`{where}. "
        "This message will update when it finishes."
    )
    notifier.confirm_decision(reply_context, acknowledgement)
    background_tasks.add_task(
        _run_remediation, plan, notifier, reply_context, decision.actor, where
    )
    return {"message": "accepted", "outcome": acknowledgement}


def _rejected(
    notifier: Notifier, reply_context: dict[str, Any], finding_id: str, action: str
) -> dict[str, Any]:
    outcome = f"⚠️ Could not {action} — {_refusal_reason(finding_id, action)}"
    notifier.confirm_decision(reply_context, outcome)
    return {"message": "rejected", "outcome": outcome}


# Run via: uvicorn finops_sentinel.adapters.inbound.fastapi_app:app --reload
