from abc import ABC, abstractmethod
from typing import Any

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

    @abstractmethod
    def narrate(self, topic: str, facts: dict[str, Any]) -> str:
        """
        Turn a set of already-computed facts into a sentence or two of prose,
        for digest sections that are not about a single Finding.

        The advisor narrates; it does not compute. Every number in `facts` was
        derived deterministically in the domain — a spend anomaly's z-score, a
        right-sizing suggestion's saving — precisely so that a model cannot
        change what the system concluded, only how it reads. `topic` names the
        kind of narration wanted; unknown topics still get usable prose.

        Same contract as summarize: always returns a string, never raises.
        """
        ...  # pragma: no cover
