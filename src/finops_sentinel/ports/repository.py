from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime
from finops_sentinel.domain.models import Finding, Resource

class FindingsRepository(ABC):
    """
    Port for persisting and retrieving resources and findings.
    """
    
    @abstractmethod
    def upsert_resource(self, resource: Resource) -> None:
        pass
        
    @abstractmethod
    def get_resource_by_id(self, resource_id: str) -> Optional[Resource]:
        pass
        
    @abstractmethod
    def get_all_resources(self) -> List[Resource]:
        pass
        
    @abstractmethod
    def mark_unseen_resources_deleted(self, cutoff_time: datetime) -> None:
        pass
        
    @abstractmethod
    def save_finding(self, finding: Finding) -> bool:
        """
        Saves a finding. Must implement atomic Compare-And-Swap (CAS) for status transitions.
        Returns True if successful, False if the transition was blocked by a race condition.
        """
        pass
        
    @abstractmethod
    def get_findings(self) -> List[Finding]:
        pass

    @abstractmethod
    def get_finding_by_id(self, finding_id: str) -> Optional[Finding]:
        pass
