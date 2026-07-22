import uuid
from typing import Any, List, Tuple
from datetime import datetime, UTC
from decimal import Decimal
from finops_sentinel.domain.rules import is_protected as tag_is_protected
from finops_sentinel.ports.scanner import Scanner
from finops_sentinel.domain.models import Finding, Resource, ResourceType, ResourceLifecycle, FindingStatus
from finops_sentinel.ports.cloud import CloudGateway

class UnattachedEBSScanner(Scanner):
    
    def __init__(self, region: str, gp2_price: float, gp3_price: float):
        self.region = region
        self.gp2_price = Decimal(str(gp2_price))
        self.gp3_price = Decimal(str(gp3_price))

    def discover(self, gateway: CloudGateway) -> List[Tuple[Resource, dict[str, Any]]]:
        discovered = []
        volumes = gateway.describe_ebs_volumes()
        now = datetime.now(UTC)
        
        for volume in volumes:
            vol_id = volume['VolumeId']
            tags = volume.get('Tags', [])
            tags_dict = {t['Key']: t['Value'] for t in tags} if isinstance(tags, list) else tags
            
            arn = f"arn:aws:ec2:{self.region}:account:volume/{vol_id}"
            
            resource = Resource(
                id=str(uuid.uuid4()),
                resource_id=vol_id,
                resource_type=ResourceType.EBS_VOLUME,
                resource_arn=arn,
                region=self.region,
                current_tags=tags_dict,
                lifecycle=ResourceLifecycle.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
            discovered.append((resource, volume))
                
        return discovered

    def evaluate(self, resources: List[Tuple[Resource, dict[str, Any]]]) -> List[Finding]:
        findings = []
        now = datetime.now(UTC)
        
        for resource, volume in resources:
            if resource.resource_type != ResourceType.EBS_VOLUME:
                continue
                
            # Filter condition from old scanner logic: describe_ebs_volumes already filters by 'available'
            # If we changed describe_ebs_volumes to return all, we'd check `volume['State'] == 'available'` here.
                
            size_gb = Decimal(str(volume['Size']))
            vol_type = volume.get('VolumeType', 'gp2')
            
            is_protected = tag_is_protected(resource.current_tags)
            
            cost_per_gb = self.gp3_price if vol_type == 'gp3' else self.gp2_price
            savings = size_gb * cost_per_gb
            
            finding = Finding(
                id=f"ebs_unattached|{resource.resource_id}",
                resource_ref=resource.id,
                rule="ebs_unattached",
                evidence=volume,
                tags_at_detection=resource.current_tags,
                est_monthly_cost_usd=savings,
                status=FindingStatus.OPEN,
                protected=is_protected,
                detected_at=now,
                last_seen_at=now
            )
            findings.append(finding)
            
        return findings
