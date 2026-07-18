from typing import List
from datetime import datetime, UTC
from finops_sentinel.domain.models import Finding, Resource, ResourceLifecycle
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner

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
