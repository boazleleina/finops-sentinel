"""Composition root: reads config and wires concrete adapters into ports.

The ONLY file that knows which concrete adapters exist.
"""
from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.adapters.aws.scanners.ebs import UnattachedEBSScanner
from finops_sentinel.adapters.aws.scanners.ec2 import StoppedEC2Scanner
from finops_sentinel.adapters.aws.scanners.eip import OrphanedEIPScanner
from finops_sentinel.adapters.notifications.console import ConsoleNotifier
from finops_sentinel.adapters.notifications.slack import SlackAdapter
from finops_sentinel.adapters.persistence.sqlalchemy_repo import SqlAlchemyRepository
from finops_sentinel.config import settings
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner


def get_cloud_gateway() -> CloudGateway:
    return Boto3Gateway(
        region=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def get_repository() -> FindingsRepository:
    return SqlAlchemyRepository(db_url=f"sqlite:///{settings.sentinel_db_path}")


def get_notifier() -> Notifier:
    if settings.slack_webhook_url:
        return SlackAdapter()
    return ConsoleNotifier()


def get_scanners() -> list[Scanner]:
    return [
        UnattachedEBSScanner(
            region=settings.aws_region,
            gp2_price=settings.gp2_price_per_gb_month,
            gp3_price=settings.gp3_price_per_gb_month,
        ),
        OrphanedEIPScanner(
            region=settings.aws_region,
            eip_price=settings.eip_price_per_month,
        ),
        StoppedEC2Scanner(
            region=settings.aws_region,
            threshold_days=settings.stopped_ec2_threshold_days,
        ),
    ]
