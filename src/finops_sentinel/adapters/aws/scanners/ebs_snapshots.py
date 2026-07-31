import uuid
from datetime import UTC, datetime, timedelta
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


class OldEbsSnapshotScanner(Scanner):
    
    def __init__(self, region: str, pricing: Pricing, age_threshold_days: int):
        self.region = region
        self.pricing = pricing
        self.age_threshold_days = age_threshold_days

    def discover(self, gateway: CloudGateway) -> list[tuple[Resource, dict[str, Any]]]:
        discovered = []
        snapshots = gateway.describe_ebs_snapshots()
        now = datetime.now(UTC)
        
        for snap in snapshots:
            snap_id = snap['SnapshotId']
            tags = snap.get('Tags', [])
            tags_dict = {t['Key']: t['Value'] for t in tags} if isinstance(tags, list) else tags
            
            arn = f"arn:aws:ec2:{self.region}:{gateway.account_id}:snapshot/{snap_id}"
            
            resource = Resource(
                id=str(uuid.uuid4()),
                resource_id=snap_id,
                resource_type=ResourceType.EBS_SNAPSHOT,
                resource_arn=arn,
                region=self.region,
                current_tags=tags_dict,
                lifecycle=ResourceLifecycle.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
            discovered.append((resource, snap))
                
        return discovered

    def evaluate(self, resources: list[tuple[Resource, dict[str, Any]]]) -> list[Finding]:
        findings = []
        now = datetime.now(UTC)
        cutoff_time = now - timedelta(days=self.age_threshold_days)
        
        # Orphan detection relies on the pooled scan resources containing the
        # volume inventory from UnattachedEBSScanner; if that scanner is ever
        # disabled, every snapshot would look orphaned.
        live_volume_ids = {
            res.resource_id for res, raw in resources
            if res.resource_type == ResourceType.EBS_VOLUME
        }
        
        for resource, snap in resources:
            if resource.resource_type != ResourceType.EBS_SNAPSHOT:
                continue
                
            start_time = snap.get('StartTime')
            volume_id = snap.get('VolumeId')
            
            if not start_time:
                continue
                
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            
            is_old = start_time < cutoff_time
            is_orphaned = volume_id not in live_volume_ids
            
            if not (is_old or is_orphaned):
                continue
                
            savings = self.pricing.ebs_snapshot_monthly(
                size_gb=int(snap['VolumeSize']), region=resource.region
            )
            
            is_protected = tag_is_protected(resource.current_tags)
            
            finding = Finding(
                id=f"ebs_old_snapshot|{resource.resource_id}",
                resource_ref=resource.id,
                rule="ebs_old_snapshot",
                evidence=snap,
                tags_at_detection=resource.current_tags,
                est_monthly_cost_usd=savings,
                status=FindingStatus.OPEN,
                protected=is_protected,
                detected_at=now,
                last_seen_at=now
            )
            findings.append(finding)
            
        return findings
