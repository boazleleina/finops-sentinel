import uuid
from datetime import UTC, datetime
from typing import Any

from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.rules import is_protected as tag_is_protected
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.pricing import Pricing
from finops_sentinel.ports.scanner import Scanner


class UnattachedEBSScanner(Scanner):
    
    def __init__(self, region: str, pricing: Pricing):
        self.region = region
        self.pricing = pricing

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
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

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings = []
        now = datetime.now(UTC)
        
        for resource, volume in resources:
            if resource.resource_type != ResourceType.EBS_VOLUME:
                continue
                
            if volume.get('State') != 'available':
                continue
                
            is_protected = tag_is_protected(resource.current_tags)

            savings = self.pricing.ebs_volume_monthly(
                volume_type=volume.get('VolumeType', 'gp2'),
                size_gb=int(volume['Size']),
                region=resource.region,
            )
            
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
