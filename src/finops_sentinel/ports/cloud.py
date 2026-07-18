from abc import ABC, abstractmethod
from typing import Dict, Any, List

class CloudGateway(ABC):
    """
    Port for interacting with cloud provider APIs.
    The domain only knows about these abstract operations.
    """
    
    @abstractmethod
    def describe_ebs_volumes(self) -> List[dict]:
        pass  # pragma: no cover
        
    @abstractmethod
    def describe_elastic_ips(self) -> List[Dict[str, Any]]:
        pass  # pragma: no cover

    @abstractmethod
    def describe_ec2_instances(self) -> List[Dict[str, Any]]:
        pass  # pragma: no cover
