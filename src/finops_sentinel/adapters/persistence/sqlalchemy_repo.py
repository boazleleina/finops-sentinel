import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    TypeDecorator,
    create_engine,
    update,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from finops_sentinel.domain.models import (
    AuditEvent,
    Decision,
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.ports.repository import FindingsRepository


class DateTimeEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


class SafeNumeric(TypeDecorator[Decimal]):
    """
    Safe Decimal type for SQLite (which lacks native Decimal).
    Stores as string to prevent float precision loss.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Optional[Decimal], dialect: Dialect) -> Optional[str]:
        if value is not None:
            return str(value)
        return None

    def process_result_value(self, value: Optional[str], dialect: Dialect) -> Optional[Decimal]:
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
    current_tags: Mapped[str] = mapped_column(String)  # JSON
    lifecycle: Mapped[str] = mapped_column(String)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            f"lifecycle IN ('{ResourceLifecycle.ACTIVE}', '{ResourceLifecycle.DELETED}')",
            name="check_resource_lifecycle",
        ),
        CheckConstraint(
            f"resource_type IN ('{ResourceType.EBS_VOLUME}', '{ResourceType.ELASTIC_IP}', "
            f"'{ResourceType.EC2_INSTANCE}', '{ResourceType.EBS_SNAPSHOT}')",
            name="check_resource_type",
        ),
    )


class FindingModel(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_ref: Mapped[str] = mapped_column(String, index=True)
    rule: Mapped[str] = mapped_column(String, index=True)
    evidence: Mapped[str] = mapped_column(String)  # JSON
    tags_at_detection: Mapped[str] = mapped_column(String)  # JSON
    est_monthly_cost_usd: Mapped[Decimal] = mapped_column(SafeNumeric)
    llm_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    protected: Mapped[bool] = mapped_column(Boolean)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            f"status IN ('{FindingStatus.OPEN}', '{FindingStatus.NOTIFIED}', "
            f"'{FindingStatus.APPROVED}', '{FindingStatus.DENIED}', "
            f"'{FindingStatus.REMEDIATED}', '{FindingStatus.FAILED}', "
            f"'{FindingStatus.EXPIRED}')",
            name="check_finding_status",
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
    detail: Mapped[str] = mapped_column(String)  # JSON


class NotificationModel(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    finding_id: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String)
    message_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RemediationModel(Base):
    __tablename__ = "remediations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    finding_id: Mapped[str] = mapped_column(String, index=True)
    playbook: Mapped[str] = mapped_column(String)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(String)  # JSON — snapshot_id lives here
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _to_resource(db_res: ResourceModel) -> Resource:
    return Resource(
        id=db_res.id,
        resource_id=db_res.resource_id,
        resource_type=ResourceType(db_res.resource_type),
        resource_arn=db_res.resource_arn,
        region=db_res.region,
        current_tags=json.loads(db_res.current_tags),
        lifecycle=ResourceLifecycle(db_res.lifecycle),
        first_seen_at=db_res.first_seen_at.replace(tzinfo=UTC),
        last_seen_at=db_res.last_seen_at.replace(tzinfo=UTC),
    )


def _to_finding(db_f: FindingModel) -> Finding:
    return Finding(
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
        last_seen_at=db_f.last_seen_at.replace(tzinfo=UTC),
    )


class SqlAlchemyRepository(FindingsRepository):
    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def upsert_resource(self, resource: Resource) -> None:
        db = self.SessionLocal()
        try:
            db_res = (
                db.query(ResourceModel)
                .filter(ResourceModel.resource_id == resource.resource_id)
                .first()
            )
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
                    last_seen_at=resource.last_seen_at,
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
            return _to_resource(db_res)
        finally:
            db.close()

    def get_all_resources(self) -> List[Resource]:
        db = self.SessionLocal()
        try:
            return [_to_resource(db_res) for db_res in db.query(ResourceModel).all()]
        finally:
            db.close()

    def mark_unseen_resources_deleted(self, cutoff_time: datetime) -> None:
        db = self.SessionLocal()
        try:
            stmt = (
                update(ResourceModel)
                .where(ResourceModel.last_seen_at < cutoff_time)
                .values(lifecycle=ResourceLifecycle.DELETED)
            )
            db.execute(stmt)
            db.commit()
        finally:
            db.close()

    def save_finding(self, finding: Finding) -> bool:
        """
        Insert-or-refresh only. Status is written once at insert; after that
        it can only move via transition_finding — a re-scan can never
        resurrect a DENIED/REMEDIATED/EXPIRED finding.
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
                    last_seen_at=finding.last_seen_at,
                )
                db.add(db_f)
            else:
                db_f.last_seen_at = finding.last_seen_at
                db_f.protected = finding.protected
                db_f.est_monthly_cost_usd = finding.est_monthly_cost_usd
                db_f.evidence = json.dumps(finding.evidence, cls=DateTimeEncoder)
                db_f.tags_at_detection = json.dumps(
                    finding.tags_at_detection, cls=DateTimeEncoder
                )
            db.commit()
            return True
        finally:
            db.close()

    def transition_finding(
        self, finding_id: str, expected: FindingStatus, new: FindingStatus
    ) -> bool:
        db = self.SessionLocal()
        try:
            stmt = (
                update(FindingModel)
                .where(FindingModel.id == finding_id, FindingModel.status == expected)
                .values(status=new)
            )
            result = db.execute(stmt)
            db.commit()
            rowcount: int = getattr(result, "rowcount", 0)
            return rowcount == 1
        finally:
            db.close()

    def get_findings(self, status: Optional[FindingStatus] = None) -> List[Finding]:
        db = self.SessionLocal()
        try:
            query = db.query(FindingModel)
            if status is not None:
                query = query.filter(FindingModel.status == status)
            return [_to_finding(db_f) for db_f in query.all()]
        finally:
            db.close()

    def get_finding_by_id(self, finding_id: str) -> Optional[Finding]:
        db = self.SessionLocal()
        try:
            db_f = db.query(FindingModel).filter(FindingModel.id == finding_id).first()
            if not db_f:
                return None
            return _to_finding(db_f)
        finally:
            db.close()

    def record_decision(self, decision: Decision) -> None:
        db = self.SessionLocal()
        try:
            db.add(
                DecisionModel(
                    finding_id=decision.finding_id,
                    actor=decision.actor,
                    action=decision.action,
                    decided_at=decision.decided_at,
                    channel=decision.channel,
                )
            )
            db.commit()
        finally:
            db.close()

    def record_audit(self, event: AuditEvent) -> None:
        db = self.SessionLocal()
        try:
            db.add(
                AuditEventModel(
                    ts=event.ts,
                    event=event.event,
                    finding_id=event.finding_id,
                    detail=json.dumps(event.detail, cls=DateTimeEncoder),
                )
            )
            db.commit()
        finally:
            db.close()

    def get_audit_events(self, finding_id: Optional[str] = None) -> List[AuditEvent]:
        db = self.SessionLocal()
        try:
            query = db.query(AuditEventModel).order_by(AuditEventModel.id)
            if finding_id is not None:
                query = query.filter(AuditEventModel.finding_id == finding_id)
            return [
                AuditEvent(
                    ts=row.ts.replace(tzinfo=UTC),
                    event=row.event,
                    finding_id=row.finding_id,
                    detail=json.loads(row.detail),
                )
                for row in query.all()
            ]
        finally:
            db.close()

    def record_notification(
        self, finding_id: str, channel: str, message_ref: Optional[str], sent_at: datetime
    ) -> None:
        db = self.SessionLocal()
        try:
            db.add(
                NotificationModel(
                    finding_id=finding_id,
                    channel=channel,
                    message_ref=message_ref,
                    sent_at=sent_at,
                )
            )
            db.commit()
        finally:
            db.close()

    def get_latest_notification_time(self, finding_id: str) -> Optional[datetime]:
        db = self.SessionLocal()
        try:
            row = (
                db.query(NotificationModel)
                .filter(NotificationModel.finding_id == finding_id)
                .order_by(NotificationModel.sent_at.desc())
                .first()
            )
            if row is None:
                return None
            return row.sent_at.replace(tzinfo=UTC)
        finally:
            db.close()

    def record_remediation(
        self,
        finding_id: str,
        playbook: str,
        dry_run: bool,
        result: str,
        detail: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        db = self.SessionLocal()
        try:
            db.add(
                RemediationModel(
                    finding_id=finding_id,
                    playbook=playbook,
                    dry_run=dry_run,
                    result=result,
                    detail=json.dumps(detail, cls=DateTimeEncoder),
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            db.commit()
        finally:
            db.close()
