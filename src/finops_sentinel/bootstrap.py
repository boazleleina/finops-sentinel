import os
from finops_sentinel.config import settings
from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.adapters.persistence.sqlalchemy_repo import SqlAlchemyRepository
from finops_sentinel.adapters.aws.scanners.ebs import UnattachedEBSScanner
from finops_sentinel.adapters.aws.scanners.eip import OrphanedEIPScanner
from finops_sentinel.adapters.aws.scanners.ec2 import StoppedEC2Scanner

def get_cloud_gateway():
    return Boto3Gateway(
        region=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key
    )

def get_repository():
    db_path = os.getenv("SENTINEL_DB_PATH", ".sentinel.db")
    db_url = f"sqlite:///{db_path}"
    return SqlAlchemyRepository(db_url=db_url)

def get_scanners():
    return [
        UnattachedEBSScanner(
            region=settings.aws_region,
            gp2_price=settings.gp2_price_per_gb_month,
            gp3_price=settings.gp3_price_per_gb_month
        ),
        OrphanedEIPScanner(
            region=settings.aws_region,
            eip_price=settings.eip_price_per_month
        ),
        StoppedEC2Scanner(
            region=settings.aws_region
        )
    ]
