"""The zero-dependency Advisor: deterministic templates, no LLM.

Used when the LLM advisor is disabled, and as the fallback path inside
OllamaAdvisor.
"""
from typing import Any

from finops_sentinel.domain.models import Finding, Resource
from finops_sentinel.domain.summaries import (
    render_template_narration,
    render_template_summary,
)
from finops_sentinel.ports.advisor import Advisor


class TemplateAdvisor(Advisor):
    def summarize(self, finding: Finding, resource: Resource) -> str:
        return render_template_summary(finding, resource)

    def narrate(self, topic: str, facts: dict[str, Any]) -> str:
        return render_template_narration(topic, facts)
