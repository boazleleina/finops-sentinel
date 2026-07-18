from abc import ABC, abstractmethod
from typing import List, Dict
from finops_sentinel.domain.models import Finding, Resource
from finops_sentinel.ports.cloud import CloudGateway

class Scanner(ABC):
    """
    Abstract Base Class for Scanners using a Two-Pass pattern.
    """
    
    @abstractmethod
    def discover(self, gateway: CloudGateway) -> List[tuple[Resource, dict]]:
        """
        Pass 1: Discover resources from the cloud provider.
        Returns a tuple of the Domain Resource and the raw cloud provider dictionary.
        """
        pass

    @abstractmethod
    def evaluate(self, resources: List[tuple[Resource, dict]]) -> List[Finding]:
        """
        Pass 2: Evaluate a list of resources to generate findings.
        """
        pass
        
    def check_is_protected(self, tags: dict | list | None) -> bool:
        if not tags:
            return False
            
        # Handle simple dict format
        if isinstance(tags, dict):
            return str(tags.get('finops:protected', '')).lower() == 'true'
            
        # Handle AWS native list of dicts format
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict) and tag.get('Key') == 'finops:protected' and str(tag.get('Value')).lower() == 'true':
                    return True
        return False
