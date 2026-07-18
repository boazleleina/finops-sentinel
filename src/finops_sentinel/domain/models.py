from enum import StrEnum
from pydantic import BaseModel, ConfigDict
from typing import Literal, Optional, Any, Dict, Set
from datetime import datetime
from decimal import Decimal

class FindingStatus(StrEnum):
    OPEN = "open"
    NOTIFIED = "notified"
    APPROVED = "approved"
    DENIED = "denied"
    REMEDIATED = "remediated"
    FAILED = "failed"
    EXPIRED = "expired"

# The state machine lives WITH the enum, in the domain
TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN:     {FindingStatus.NOTIFIED},
    FindingStatus.NOTIFIED: {FindingStatus.APPROVED, FindingStatus.DENIED, FindingStatus.EXPIRED},
    FindingStatus.APPROVED: {FindingStatus.REMEDIATED, FindingStatus.FAILED},
    # DENIED / REMEDIATED / FAILED / EXPIRED are terminal in v1
}

class ResourceLifecycle(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"

class ResourceType(StrEnum):
    EBS_VOLUME = "ebs_volume"
    ELASTIC_IP = "elastic_ip"
    EC2_INSTANCE = "ec2_instance"
    EBS_SNAPSHOT = "ebs_snapshot"

class Resource(BaseModel):                     
    id: str                                    
    resource_id: str                           
    resource_type: ResourceType                
    resource_arn: str                          
    region: str                                
    current_tags: dict                         
    lifecycle: ResourceLifecycle
    first_seen_at: datetime
    last_seen_at: datetime                     

class Finding(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    resource_ref: str                          
    rule: str                                  
    evidence: dict                             
    tags_at_detection: dict                    
    est_monthly_cost_usd: Decimal              
    llm_summary: Optional[str] = None                    
    status: FindingStatus
    protected: bool
    detected_at: datetime                      
    last_seen_at: datetime                     

class Decision(BaseModel):                     
    finding_id: str
    actor: str
    action: Literal["approve","deny"]
    decided_at: datetime
    channel: str                               

class AuditEvent(BaseModel):                   
    ts: datetime
    event: str
    finding_id: Optional[str] = None
    detail: dict
