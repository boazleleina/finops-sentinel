"""Inbound FastAPI adapter. Routes are thin: parse input, call a domain
service, format output. Channel-specific callback logic (signatures, payload
shape) lives in the configured Notifier adapter, not here."""
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from finops_sentinel.bootstrap import get_cloud_gateway, get_notifier, get_repository
from finops_sentinel.config import settings
from finops_sentinel.domain.models import AuditEvent, Finding, FindingStatus, Resource
from finops_sentinel.domain.services import approve_finding, deny_finding

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
def health_check() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/findings")
def list_findings(status: Optional[FindingStatus] = None) -> List[Finding]:
    return get_repository().get_findings(status=status)


@app.get("/resources")
def list_resources() -> List[Resource]:
    return get_repository().get_all_resources()


@app.get("/audit")
def list_audit_events(finding_id: Optional[str] = None) -> List[AuditEvent]:
    return get_repository().get_audit_events(finding_id=finding_id)


def _decide(finding_id: str, action: str, actor: str, channel: str) -> bool:
    repo = get_repository()
    if action == "approve":
        return approve_finding(
            finding_id,
            repo,
            get_cloud_gateway(),
            actor=actor,
            channel=channel,
            dry_run=settings.dry_run,
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
        raise HTTPException(
            status_code=409,
            detail=f"Finding {finding_id} cannot be {body.action}d (unknown, protected, "
            "or already decided)",
        )
    return DecisionResponse(
        finding_id=finding_id, action=body.action, success=True, dry_run=settings.dry_run
    )


@app.post("/callbacks/{channel}")
async def notifier_callback(channel: str, request: Request) -> Dict[str, Any]:
    """
    Webhook endpoint for interactive decision callbacks (e.g. Slack buttons).
    The raw payload is handed to the configured Notifier adapter, which
    verifies authenticity and parses it into a domain Decision.
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

    try:
        success = _decide(
            decision.finding_id, decision.action, actor=decision.actor, channel=channel
        )
    except Exception:
        # Playbook failed mid-remediation; the service already recorded
        # FAILED plus the audit/remediation rows. Reply cleanly instead of 500.
        outcome = (
            f"❌ *Remediation failed* after approval by @{decision.actor} — "
            "the resource may no longer exist. See the audit log for details."
        )
        notifier.confirm_decision(reply_context, outcome)
        return {"message": "failed", "outcome": outcome}

    if not success:
        outcome = f"⚠️ Could not {decision.action} — already decided, protected, or gone."
    elif decision.action == "deny":
        outcome = f"🚫 *Denied* by @{decision.actor} — no action taken, finding closed."
    elif settings.dry_run:
        outcome = f"✅ *Approved* by @{decision.actor} — DRY RUN, no resources were changed."
    else:
        outcome = f"✅ *Approved* by @{decision.actor} — remediation executed."

    notifier.confirm_decision(reply_context, outcome)
    return {"message": "ok" if success else "rejected", "outcome": outcome}


# Run via: uvicorn finops_sentinel.adapters.inbound.fastapi_app:app --reload
