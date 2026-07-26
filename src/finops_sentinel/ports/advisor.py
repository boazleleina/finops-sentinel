from abc import ABC, abstractmethod

from finops_sentinel.domain.models import Finding, Resource


class Advisor(ABC):
    """
    Port for turning a Finding into human-readable optimization advice.

    The advisor is advisory only: it never decides, never remediates, and
    never gates a notification. Implementations MUST NOT raise — a summary
    is a nice-to-have, so an unreachable or misbehaving backend has to
    degrade to a deterministic template (see
    finops_sentinel.domain.summaries.render_template_summary) rather than
    block the scan/notify pipeline.
    """

    @abstractmethod
    def summarize(self, finding: Finding, resource: Resource) -> str:
        """
        Return a short operator-facing explanation of the finding: what was
        detected, why it costs money, and what the safe next step is.

        Always returns a usable string. Never raises.
        """
        ...  # pragma: no cover
