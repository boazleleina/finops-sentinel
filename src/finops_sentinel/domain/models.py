from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


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
    RDS_INSTANCE = "rds_instance"
    S3_BUCKET = "s3_bucket"

class Resource(BaseModel):                     
    id: str                                    
    resource_id: str                           
    resource_type: ResourceType                
    resource_arn: str                          
    region: str                                
    current_tags: dict[str, Any]                         
    lifecycle: ResourceLifecycle
    first_seen_at: datetime
    last_seen_at: datetime                     

class Finding(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    resource_ref: str                          
    rule: str                                  
    evidence: dict[str, Any]                             
    tags_at_detection: dict[str, Any]                    
    est_monthly_cost_usd: Decimal              
    llm_summary: str | None = None                    
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
    finding_id: str | None = None
    detail: dict[str, Any]


# --------------------------------------------------------------------------
# Digest shapes. Deliberately NOT Findings: a right-sizing suggestion and a
# spend anomaly have nothing to approve — no resource is being deleted — so
# they carry no status, no protection flag, and never enter the state machine.
# --------------------------------------------------------------------------


class RightsizingCandidate(BaseModel):
    """A cheaper instance type than the one a resource runs today.

    Supplied by the Pricing port, which knows what things cost and nothing
    about whether a swap is wise. The saving is stated rather than derived by
    callers so one place owns the arithmetic.
    """

    instance_type: str
    monthly_cost_usd: Decimal
    monthly_saving_usd: Decimal


class RightsizingSuggestion(BaseModel):
    """One advisory "this box looks too big" line in the digest.

    Carries the peak that drove it, not just the mean: an operator has to be
    able to disagree with the suggestion, and average CPU is the number that
    would mislead them into agreeing with a bad one.
    """

    resource_id: str
    region: str
    current_instance_type: str
    current_monthly_cost_usd: Decimal
    candidate: RightsizingCandidate
    max_cpu_percent: float
    avg_cpu_percent: float
    datapoints: int
    observation_days: int

    @property
    def monthly_saving_usd(self) -> Decimal:
        return self.candidate.monthly_saving_usd


class SpendSnapshot(BaseModel):
    """One day's estimated monthly waste, the input to anomaly detection.

    "Estimated waste", not spend: there is no billing data in this system
    (Cost Explorer needs a real account), so what is measured is the total
    est_monthly_cost_usd of live findings. Every user-facing string says so.
    A Cost-Explorer-backed adapter can replace the input later without the
    z-score logic changing at all.
    """

    snapshot_date: date
    total_estimated_monthly_usd: Decimal
    open_findings: int
    active_resources: int
    captured_at: datetime


class SpendAnomaly(BaseModel):
    """A day whose estimated waste sits far off its own trailing average."""

    date: date
    value: Decimal
    mean: Decimal
    stdev: Decimal
    z_score: float
    direction: Literal["increase", "decrease"]
    window_days: int
