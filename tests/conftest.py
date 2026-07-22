import os
import pytest
import boto3
from moto import mock_aws
from finops_sentinel.adapters.persistence.sqlalchemy_repo import SqlAlchemyRepository, Base
from finops_sentinel.config import settings

@pytest.fixture(autouse=True)
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    
    # Also override settings to not point to LocalStack during testing
    # Moto intercepts boto3 calls natively when endpoint_url is None
    settings.aws_endpoint_url = None
    settings.aws_access_key_id = "testing"
    settings.aws_secret_access_key = "testing"

    # Never talk to a real Slack workspace from unit tests, and keep the
    # safe default regardless of the developer's .env.
    settings.slack_webhook_url = None
    settings.slack_signing_secret = None
    settings.dry_run = True

@pytest.fixture
def mock_aws_env():
    """Starts moto mock_aws and yields a raw ec2 client."""
    with mock_aws():
        yield boto3.client("ec2", region_name="us-east-1")

@pytest.fixture
def repository():
    """Provides an in-memory SqlAlchemyRepository with a fresh schema."""
    repo = SqlAlchemyRepository(db_url="sqlite:///:memory:")
    # Create tables
    Base.metadata.create_all(repo.engine)
    yield repo
    # Teardown
    Base.metadata.drop_all(repo.engine)
    repo.engine.dispose()
