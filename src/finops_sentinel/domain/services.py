from typing import List
from datetime import datetime, UTC
from finops_sentinel.domain.models import Finding
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner
from finops_sentinel.domain.models import FindingStatus, TRANSITIONS, ResourceType
from datetime import timedelta

def run_scan(
    gateway: CloudGateway,
    repo: FindingsRepository,
    scanners: List[Scanner]
) -> List[Finding]:
    """
    Orchestrates the Two-Pass Scan:
    Pass 1: Discover inventory (Resources) and upsert to repository. Unseen resources marked DELETED.
    Pass 2: Evaluate the active inventory to generate findings and upsert to repository.
    """
    scan_start_time = datetime.now(UTC)
    
    # Pass 1: Discovery
    discovered_resources = []
    for scanner in scanners:
        # returns List[Tuple[Resource, provider_data_dict]]
        scanner_resources = scanner.discover(gateway)
        discovered_resources.extend(scanner_resources)
        
        for resource, _ in scanner_resources:
            repo.upsert_resource(resource)
            
    # Mark anything not seen in this scan as DELETED
    repo.mark_unseen_resources_deleted(scan_start_time)
    
    # Pass 2: Evaluation
    # Note: We evaluate based on the discovered_resources which contain the raw provider data
    all_findings = []
    for scanner in scanners:
        findings = scanner.evaluate(discovered_resources)
        all_findings.extend(findings)
        
        for finding in findings:
            repo.save_finding(finding)
            
    return all_findings


def approve_finding(
    finding_id: str, 
    repo: FindingsRepository, 
    gateway: CloudGateway
) -> bool:
    """
    Approves a finding, attempts remediation via the CloudGateway, and updates the status.
    Returns True if fully remediated, False if CAS failed or not allowed.
    Raises exception if playbook fails.
    """
    finding = repo.get_finding_by_id(finding_id)
    if not finding:
        return False
        
    if FindingStatus.APPROVED not in TRANSITIONS.get(finding.status, set()):
        return False
        
    # Attempt atomic state transition to APPROVED
    old_status = finding.status
    finding.status = FindingStatus.APPROVED
    if not repo.save_finding(finding):
        return False
        
    # Playbook execution
    resource = repo.get_resource_by_id(finding.resource_ref)
    if not resource:
        return False
        
    try:
        if resource.resource_type == ResourceType.EBS_VOLUME:
            gateway.snapshot_and_delete_volume(resource.resource_id)
        elif resource.resource_type == ResourceType.ELASTIC_IP:
            gateway.release_elastic_ip(resource.resource_id)
        elif resource.resource_type == ResourceType.EC2_INSTANCE:
            gateway.terminate_instance(resource.resource_id)
            
        finding.status = FindingStatus.REMEDIATED
        repo.save_finding(finding)
        return True
    except Exception as e:
        finding.status = FindingStatus.FAILED
        repo.save_finding(finding)
        raise e

def deny_finding(
    finding_id: str, 
    repo: FindingsRepository
) -> bool:
    """
    Denies a finding, terminating its lifecycle.
    Returns True if transition successful, False otherwise.
    """
    finding = repo.get_finding_by_id(finding_id)
    if not finding:
        return False
        
    if FindingStatus.DENIED not in TRANSITIONS.get(finding.status, set()):
        return False
        
    finding.status = FindingStatus.DENIED
    return repo.save_finding(finding)

def expire_stale(repo: FindingsRepository) -> None:
    """
    Finds findings in NOTIFIED state that are older than 72 hours and marks them EXPIRED.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=72)
    findings = repo.get_findings()
    
    for f in findings:
        if f.status == FindingStatus.NOTIFIED and f.last_seen_at < cutoff:
            f.status = FindingStatus.EXPIRED
            repo.save_finding(f)
