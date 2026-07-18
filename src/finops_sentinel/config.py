from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    dry_run: bool = True
    aws_endpoint_url: str | None = "http://localhost:4566"
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = "test"
    aws_secret_access_key: str | None = "test"
    
    # Static pricing table for cost estimation (based on us-east-1 standard pricing)
    # Source: https://aws.amazon.com/ebs/pricing/
    gp3_price_per_gb_month: float = 0.08
    gp2_price_per_gb_month: float = 0.10
    
    # Source: https://aws.amazon.com/vpc/pricing/ (Public IPv4 addresses)
    # $0.005 per hour per IP. 730 hours/month * 0.005 = 3.65
    eip_price_per_month: float = 3.65
    
    # Thresholds
    stopped_ec2_threshold_days: int = 7
    
    class Config:
        env_file = ".env"

settings = Settings()
