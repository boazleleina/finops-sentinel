import json
from typing import List, Optional
from datetime import datetime, UTC
from sqlalchemy import create_engine, String, Boolean, DateTime, CheckConstraint, select, update, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from decimal import Decimal

class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

from finops_sentinel.domain.models import (
    Finding, Resource, Decision, AuditEvent,
    FindingStatus, ResourceLifecycle, ResourceType
)
from finops_sentinel.ports.repository import FindingsRepository

class SafeNumeric(TypeDecorator):
    """
    Safe Decimal type for SQLite (which lacks native Decimal).
    Stores as string to prevent float precision loss.
    """
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return str(value)
        return None

    def process_result_value(self, value, dialect):
        if value is not None:
            return Decimal(value)
        return None

class Base(DeclarativeBase):
    pass

class ResourceModel(Base):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    resource_type: Mapped[str] = mapped_column(String)
    resource_arn: Mapped[str] = mapped_column(String)
    region: Mapped[str] = mapped_column(String)
    current_tags: Mapped[str] = mapped_column(String) # JSON
    lifecycle: Mapped[str] = mapped_column(String)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    
    __table_args__ = (
        CheckConstraint(
            f"lifecycle IN ('{ResourceLifecycle.ACTIVE}', '{ResourceLifecycle.DELETED}')",
            name="check_resource_lifecycle"
        ),
        CheckConstraint(
            f"resource_type IN ('{ResourceType.EBS_VOLUME}', '{ResourceType.ELASTIC_IP}', '{ResourceType.EC2_INSTANCE}', '{ResourceType.EBS_SNAPSHOT}')",
            name="check_resource_type"
        ),
    )

class FindingModel(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_ref: Mapped[str] = mapped_column(String, index=True)
    rule: Mapped[str] = mapped_column(String, index=True)
    evidence: Mapped[str] = mapped_column(String) # JSON
    tags_at_detection: Mapped[str] = mapped_column(String) # JSON
    est_monthly_cost_usd: Mapped[Decimal] = mapped_column(SafeNumeric)
    llm_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    protected: Mapped[bool] = mapped_column(Boolean)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            f"status IN ('{FindingStatus.OPEN}', '{FindingStatus.NOTIFIED}', '{FindingStatus.APPROVED}', '{FindingStatus.DENIED}', '{FindingStatus.REMEDIATED}', '{FindingStatus.FAILED}', '{FindingStatus.EXPIRED}')",
            name="check_finding_status"
        ),
    )

class DecisionModel(Base):
    __tablename__ = "decisions"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    finding_id: Mapped[str] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String)

class AuditEventModel(Base):
    __tablename__ = "audit_events"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event: Mapped[str] = mapped_column(String)
    finding_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    detail: Mapped[str] = mapped_column(String) # JSON


class SqlAlchemyRepository(FindingsRepository):
    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def upsert_resource(self, resource: Resource) -> None:
        db = self.SessionLocal()
        try:
            db_res = db.query(ResourceModel).filter(ResourceModel.resource_id == resource.resource_id).first()
            if not db_res:
                db_res = ResourceModel(
                    id=resource.id,
                    resource_id=resource.resource_id,
                    resource_type=resource.resource_type,
                    resource_arn=resource.resource_arn,
                    region=resource.region,
                    current_tags=json.dumps(resource.current_tags),
                    lifecycle=resource.lifecycle,
                    first_seen_at=resource.first_seen_at,
                    last_seen_at=resource.last_seen_at
                )
                db.add(db_res)
            else:
                db_res.current_tags = json.dumps(resource.current_tags)
                db_res.lifecycle = resource.lifecycle
                db_res.last_seen_at = resource.last_seen_at
                resource.id = db_res.id 
            
            db.commit()
        finally:
            db.close()

    def get_resource_by_id(self, resource_id: str) -> Optional[Resource]:
        db = self.SessionLocal()
        try:
            db_res = db.query(ResourceModel).filter(ResourceModel.id == resource_id).first()
            if not db_res:
                return None
            return Resource(
                id=db_res.id,
                resource_id=db_res.resource_id,
                resource_type=ResourceType(db_res.resource_type),
                resource_arn=db_res.resource_arn,
                region=db_res.region,
                current_tags=json.loads(db_res.current_tags),
                lifecycle=ResourceLifecycle(db_res.lifecycle),
                first_seen_at=db_res.first_seen_at.replace(tzinfo=UTC),
                last_seen_at=db_res.last_seen_at.replace(tzinfo=UTC)
            )
        finally:
            db.close()

    def get_all_resources(self) -> List[Resource]:
        db = self.SessionLocal()
        try:
            db_res_list = db.query(ResourceModel).all()
            return [
                Resource(
                    id=db_res.id,
                    resource_id=db_res.resource_id,
                    resource_type=ResourceType(db_res.resource_type),
                    resource_arn=db_res.resource_arn,
                    region=db_res.region,
                    current_tags=json.loads(db_res.current_tags),
                    lifecycle=ResourceLifecycle(db_res.lifecycle),
                    first_seen_at=db_res.first_seen_at.replace(tzinfo=UTC),
                    last_seen_at=db_res.last_seen_at.replace(tzinfo=UTC)
                ) for db_res in db_res_list
            ]
        finally:
            db.close()

    def mark_unseen_resources_deleted(self, cutoff_time: datetime) -> None:
        db = self.SessionLocal()
        try:
            stmt = update(ResourceModel).where(
                ResourceModel.last_seen_at < cutoff_time
            ).values(lifecycle=ResourceLifecycle.DELETED)
            db.execute(stmt)
            db.commit()
        finally:
            db.close()

    def save_finding(self, finding: Finding) -> bool:
        """
        Atomic CAS update for findings.
        """
        db = self.SessionLocal()
        try:
            db_f = db.query(FindingModel).filter(FindingModel.id == finding.id).first()
            if not db_f:
                db_f = FindingModel(
                    id=finding.id,
                    resource_ref=finding.resource_ref,
                    rule=finding.rule,
                    evidence=json.dumps(finding.evidence, cls=DateTimeEncoder),
                    tags_at_detection=json.dumps(finding.tags_at_detection, cls=DateTimeEncoder),
                    est_monthly_cost_usd=finding.est_monthly_cost_usd,
                    llm_summary=finding.llm_summary,
                    status=finding.status,
                    protected=finding.protected,
                    detected_at=finding.detected_at,
                    last_seen_at=finding.last_seen_at
                )
                db.add(db_f)
                db.commit()
                return True
            else:
                if db_f.status == finding.status:
                    db_f.last_seen_at = finding.last_seen_at
                    db_f.protected = finding.protected
                    db_f.evidence = json.dumps(finding.evidence, cls=DateTimeEncoder)
                    db_f.tags_at_detection = json.dumps(finding.tags_at_detection, cls=DateTimeEncoder)
                    db.commit()
                    return True
                else:
                    old_status = db_f.status
                    stmt = update(FindingModel).where(
                        FindingModel.id == finding.id,
                        FindingModel.status == old_status
                    ).values(
                        status=finding.status,
                        last_seen_at=finding.last_seen_at
                    )
                    result = db.execute(stmt)
                    db.commit()
                    return result.rowcount == 1
        finally:
            db.close()

    def get_findings(self) -> List[Finding]:
        db = self.SessionLocal()
        try:
            db_findings = db.query(FindingModel).all()
            findings = []
            for db_f in db_findings:
                findings.append(Finding(
                    id=db_f.id,
                    resource_ref=db_f.resource_ref,
                    rule=db_f.rule,
                    evidence=json.loads(db_f.evidence),
                    tags_at_detection=json.loads(db_f.tags_at_detection),
                    est_monthly_cost_usd=db_f.est_monthly_cost_usd,
                    llm_summary=db_f.llm_summary,
                    status=FindingStatus(db_f.status),
                    protected=db_f.protected,
                    detected_at=db_f.detected_at.replace(tzinfo=UTC),
                    last_seen_at=db_f.last_seen_at.replace(tzinfo=UTC)
                ))
            return findings
        finally:
            db.close()
