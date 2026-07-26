"""Ollama-backed Advisor adapter.

Talks to a locally hosted Ollama daemon over its HTTP API. Ollama runs
natively on the host (not in a container): Docker on Apple Silicon has no GPU
passthrough, so a containerized Ollama would be CPU-only. From inside the app
container the daemon is reachable at http://host.docker.internal:11434.

Trust model: the prompt embeds cloud metadata (tags, resource ids) that any
user with tagging permission can write, so the generated text is treated as
untrusted display copy. It is shown to operators and never parsed for
decisions — approve/deny buttons carry finding ids, not model output.
"""
import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from finops_sentinel.domain.models import Finding, Resource
from finops_sentinel.domain.summaries import render_template_summary
from finops_sentinel.ports.advisor import Advisor

logger = logging.getLogger(__name__)

# Reasoning models (qwen3) may still wrap output in think tags even with
# thinking disabled; strip them before parsing.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Evidence is a raw cloud API dict and can be large (block device mappings,
# network interfaces). Only these keys ever reach the prompt, and only if the
# scanner set them.
_EVIDENCE_ALLOWLIST = (
    "stopped_days",
    "threshold_days",
    "idle_days",
    "avg_cpu_percent",
    "max_cpu_percent",
    "avg_network_bytes",
    "observation_days",
    "Size",
    "VolumeType",
    "State",
    "InstanceType",
    "StartTime",
)

# Ambiguous field names get read as something else — qwen3 reported
# observation_days as "running for 14 days". Rename them in the prompt only;
# the stored evidence keeps the scanner's original keys.
_EVIDENCE_LABELS = {
    "observation_days": "metric_window_days",
    "idle_days": "metric_window_days",
}

_SYSTEM_PROMPT = (
    "You are a FinOps advisor for an AWS cost-optimization agent. Given one "
    "finding, explain it to an on-call engineer who has 10 seconds to read.\n"
    "Rules:\n"
    "- Use ONLY the fields provided. Never invent resource names, costs, "
    "metrics, or uptime. metric_window_days is how far back the metrics were "
    "sampled; it is NOT how long the resource has existed or run.\n"
    "- Never claim an action was taken. A human approves every remediation.\n"
    "- 'risk' means the risk of REMOVING this resource, not the size of the "
    "cost: 'low' when the evidence shows it is clearly unused, 'medium' when "
    "tags or naming hint it may still be needed (a standby, a batch worker, a "
    "scheduled job), 'high' when removing it could plausibly break something "
    "or the evidence is thin. Cost alone never makes the risk high.\n"
    "- Respond with JSON only."
)


class AdvisorResponse(BaseModel):
    """Strict schema for the model's reply. Anything else is a failure."""

    summary: str = Field(min_length=1, max_length=600)
    risk: str = Field(pattern="^(low|medium|high)$")
    recommended_action: str = Field(min_length=1, max_length=300)


class OllamaAdvisor(Advisor):
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        disable_thinking: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.disable_thinking = disable_thinking

    def summarize(self, finding: Finding, resource: Resource) -> str:
        """Ask the model for advice, falling back to the domain template.

        Per the Advisor port this never raises: every failure mode (transport
        error, timeout, non-JSON body, schema violation) degrades to
        render_template_summary so the notify pipeline keeps running.
        """
        fallback = render_template_summary(finding, resource)
        try:
            parsed = self.advise(finding, resource)
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError, KeyError) as exc:
            logger.warning(
                "Ollama advisor failed for finding %s (%s: %s); using template summary",
                finding.id,
                type(exc).__name__,
                exc,
            )
            return fallback

        return f"{parsed.summary} (risk: {parsed.risk}) Next step: {parsed.recommended_action}"

    def advise(self, finding: Finding, resource: Resource) -> AdvisorResponse:
        """Strict path: return the validated model response or raise.

        summarize() is the safe entry point every caller should use. This one
        exists so `sentinel smoke-llm` can tell a real success apart from a
        silent fallback — through summarize() the two are indistinguishable.
        """
        return self._request(self._build_prompt(finding, resource))

    def _build_prompt(self, finding: Finding, resource: Resource) -> str:
        evidence = {
            _EVIDENCE_LABELS.get(key, key): finding.evidence[key]
            for key in _EVIDENCE_ALLOWLIST
            if key in finding.evidence
        }
        payload = {
            "rule": finding.rule,
            "resource_type": str(resource.resource_type),
            "resource_id": resource.resource_id,
            "region": resource.region,
            "estimated_monthly_cost_usd": str(finding.est_monthly_cost_usd),
            "tags": finding.tags_at_detection,
            "evidence": evidence,
        }
        return f"Finding:\n{json.dumps(payload, default=str, indent=2)}"

    def _request(self, prompt: str) -> AdvisorResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "format": AdvisorResponse.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.disable_thinking:
            body["think"] = False

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(f"{self.base_url}/api/chat", json=body)
            if response.status_code == 400 and "think" in body:
                # Model does not accept the thinking toggle — retry without it
                # and rely on tag stripping instead.
                del body["think"]
                response = client.post(f"{self.base_url}/api/chat", json=body)
            response.raise_for_status()
            content = response.json()["message"]["content"]

        return AdvisorResponse.model_validate_json(_THINK_BLOCK_RE.sub("", content).strip())
