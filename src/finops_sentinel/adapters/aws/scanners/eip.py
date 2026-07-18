import uuid
from typing import List, Tuple
from datetime import datetime, UTC
from decimal import Decimal
from finops_sentinel.ports.scanner import Scanner
from finops_sentinel.domain.models import Finding, Resource, ResourceType, ResourceLifecycle, FindingStatus
from finops_sentinel.ports.cloud import CloudGateway

class OrphanedEIPScanner(Scanner):
    
    def __init__(self, region: str, eip_price: float):
        self.region = region
        self.eip_price = Decimal(str(eip_price))

    def discover(self, gateway: CloudGateway) -> List[Tuple[Resource, dict]]:
        discovered = []
        addresses = gateway.describe_elastic_ips()
        now = datetime.now(UTC)
        
        for address in addresses:
            allocation_id = address.get('AllocationId', address.get('PublicIp', 'Unknown'))
            tags = address.get('Tags', [])
            tags_dict = {t['Key']: t['Value'] for t in tags} if isinstance(tags, list) else tags
            
            arn = f"arn:aws:ec2:{self.region}:account:elastic-ip/{allocation_id}"
            
            resource = Resource(
                id=str(uuid.uuid4()),
                resource_id=allocation_id,
                resource_type=ResourceType.ELASTIC_IP,
                resource_arn=arn,
                region=self.region,
                current_tags=tags_dict,
                lifecycle=ResourceLifecycle.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
            discovered.append((resource, address))
                
        return discovered

    def evaluate(self, resources: List[Tuple[Resource, dict]]) -> List[Finding]:
        findings = []
        now = datetime.now(UTC)
        
        for resource, address in resources:
            if resource.resource_type != ResourceType.ELASTIC_IP:
                continue
                
            if 'AssociationId' not in address:
                is_protected = self.check_is_protected(resource.current_tags)
                savings = self.eip_price
                
                finding = Finding(
                    id=f"eip_orphaned|{resource.resource_id}",
                    resource_ref=resource.id,
                    rule="eip_orphaned",
                    evidence=address,
                    tags_at_detection=resource.current_tags,
                    est_monthly_cost_usd=savings,
                    status=FindingStatus.OPEN,
                    protected=is_protected,
                    detected_at=now,
                    last_seen_at=now
                )
                findings.append(finding)
                
        return findings
