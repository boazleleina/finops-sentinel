from abc import ABC, abstractmethod
from typing import Dict, Any

from finops_sentinel.domain.models import Finding, Resource

class Notifier(ABC):
    """
    Port for sending outbound notifications to humans or external systems.
    """
    
    @abstractmethod
    def send_finding_alert(self, finding: Finding, resource: Resource) -> None:
        """
        Send an interactive alert regarding a specific finding.
        """
        pass  # pragma: no cover
