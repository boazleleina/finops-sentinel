from finops_sentinel.config import settings
from finops_sentinel.adapters.notifications.slack import SlackAdapter
from finops_sentinel.adapters.persistence.sqlalchemy_repo import SqlAlchemyRepository
from finops_sentinel.domain.models import Finding, FindingStatus, Resource, ResourceType, ResourceLifecycle
from datetime import datetime, UTC
from decimal import Decimal

# Force it into live mode so the Slack message actually sends
settings.dry_run = False

# Connect to the real local database
repo = SqlAlchemyRepository(f"sqlite:///{settings.sentinel_db_path}")

# 1. Create a PERFECTLY MATCHING Resource and save it
resource = Resource(
    id="res-perfect-123",             # Primary Key
    resource_id="vol-perfect-123",    # AWS ID
    resource_type=ResourceType.EBS_VOLUME,
    resource_arn="arn:aws:ec2:us-east-1:123:volume/vol-perfect-123",
    region="us-east-1",
    current_tags={},
    lifecycle=ResourceLifecycle.ACTIVE,
    first_seen_at=datetime.now(UTC),
    last_seen_at=datetime.now(UTC)
)
repo.upsert_resource(resource)

# 2. Create a PERFECTLY MATCHING Finding and save it as NOTIFIED
finding = Finding(
    id="finding-perfect-123",         # Primary Key
    resource_ref="res-perfect-123",   # Must match resource.id exactly!
    rule="unused_ebs",
    evidence={},
    tags_at_detection={},
    est_monthly_cost_usd=Decimal("5.00"),
    status=FindingStatus.NOTIFIED,    # Must be NOTIFIED to be approved!
    protected=False,
    detected_at=datetime.now(UTC),
    last_seen_at=datetime.now(UTC)
)
repo.save_finding(finding)

# 3. Send the Slack Alert
adapter = SlackAdapter()
adapter.send_finding_alert(finding, resource)
print("✅ Test finding injected and Slack alert sent!")
