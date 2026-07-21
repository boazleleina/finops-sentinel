from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    dry_run: bool = True
    aws_endpoint_url: str | None = "http://localhost:4566"
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = "test"
    aws_secret_access_key: str | None = "test"
    
    # Slack Settings
    slack_webhook_url: str | None = None
    slack_signing_secret: str | None = None
    
    # Path to the local SQLite database for finding persistence
    sentinel_db_path: str = ".sentinel.db"
    
    # Static pricing table for cost estimation (based on us-east-1 standard pricing)
    # Source: https://aws.amazon.com/ebs/pricing/
    gp3_price_per_gb_month: float = 0.08
    gp2_price_per_gb_month: float = 0.10
    
    # Source: https://aws.amazon.com/vpc/pricing/ (Public IPv4 addresses)
    # $0.005 per hour per IP. 730 hours/month * 0.005 = 3.65
    eip_price_per_month: float = 3.65
    
    # Thresholds
    stopped_ec2_threshold_days: int = 7
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
