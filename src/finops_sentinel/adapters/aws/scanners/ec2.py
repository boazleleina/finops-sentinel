import uuid
from typing import List, Tuple
from datetime import datetime, UTC
from decimal import Decimal
from finops_sentinel.ports.scanner import Scanner
from finops_sentinel.domain.models import Finding, Resource, ResourceType, ResourceLifecycle, FindingStatus
from finops_sentinel.ports.cloud import CloudGateway

class StoppedEC2Scanner(Scanner):
    
    def __init__(self, region: str):
        self.region = region

    def discover(self, gateway: CloudGateway) -> List[Tuple[Resource, dict]]:
        discovered = []
        instances = gateway.describe_ec2_instances()
        now = datetime.now(UTC)
        
        for instance in instances:
            instance_id = instance['InstanceId']
            tags = instance.get('Tags', [])
            tags_dict = {t['Key']: t['Value'] for t in tags} if isinstance(tags, list) else tags
            
            arn = f"arn:aws:ec2:{self.region}:account:instance/{instance_id}"
            
            resource = Resource(
                id=str(uuid.uuid4()),
                resource_id=instance_id,
                resource_type=ResourceType.EC2_INSTANCE,
                resource_arn=arn,
                region=self.region,
                current_tags=tags_dict,
                lifecycle=ResourceLifecycle.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
            discovered.append((resource, instance))
                
        return discovered

    def evaluate(self, resources: List[Tuple[Resource, dict]]) -> List[Finding]:
        findings = []
        now = datetime.now(UTC)
        
        for resource, instance in resources:
            if resource.resource_type != ResourceType.EC2_INSTANCE:
                continue
                
            is_protected = self.check_is_protected(resource.current_tags)
            savings = Decimal("5.00") # placeholder
            
            finding = Finding(
                id=f"ec2_stopped|{resource.resource_id}",
                resource_ref=resource.id,
                rule="ec2_stopped",
                evidence=instance,
                tags_at_detection=resource.current_tags,
                est_monthly_cost_usd=savings,
                status=FindingStatus.OPEN,
                protected=is_protected,
                detected_at=now,
                last_seen_at=now
            )
            findings.append(finding)
                
        return findings
