from abc import ABC, abstractmethod
from typing import Any, List

from finops_sentinel.domain.models import Finding, Resource
from finops_sentinel.ports.cloud import CloudGateway


class Scanner(ABC):
    """
    Abstract Base Class for Scanners using a Two-Pass pattern.
    """

    @abstractmethod
    def discover(self, gateway: CloudGateway) -> List[tuple[Resource, dict[str, Any]]]:
        """
        Pass 1: Discover resources from the cloud provider.
        Returns a tuple of the Domain Resource and the raw cloud provider dictionary.
        """
        ...  # pragma: no cover

    @abstractmethod
    def evaluate(self, resources: List[tuple[Resource, dict[str, Any]]]) -> List[Finding]:
        """
        Pass 2: Evaluate a list of resources to generate findings.
        """
        ...  # pragma: no cover
