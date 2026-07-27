from pydantic_settings import BaseSettings, SettingsConfigDict

# AWS_REGIONS sentinel: scan every region the account has enabled, discovered
# at runtime via ec2:DescribeRegions rather than hardcoded.
ALL_REGIONS = "all"


class Settings(BaseSettings):
    dry_run: bool = True
    aws_endpoint_url: str | None = "http://localhost:4566"
    # The home region: used for region discovery, for anything that needs a
    # single endpoint, and as the fallback when AWS_REGIONS is unset.
    aws_region: str = "us-east-1"
    # Regions to scan. Comma-separated ("us-east-1,eu-west-1,ap-southeast-2"),
    # or "all" to scan every enabled region. Left empty, only AWS_REGION is
    # scanned — so a single-region setup needs no new config.
    aws_regions: str = ""
    # Regions are scanned concurrently. Wall clock is dominated by per-instance
    # CloudWatch calls, so a serial sweep of a dozen regions takes minutes.
    # Set to 1 to disable the pool.
    scan_max_workers: int = 8
    aws_access_key_id: str | None = "test"
    aws_secret_access_key: str | None = "test"

    # Slack Settings
    slack_webhook_url: str | None = None
    slack_signing_secret: str | None = None
    
    # Path to the local SQLite database for finding persistence
    sentinel_db_path: str = ".sentinel.db"
    
    # Prices are NOT configured here. Every rate lives in one place:
    # adapters/aws/pricing.py, behind the Pricing port, with sources cited.
    # That keeps a live pricing adapter a drop-in replacement.

    # Thresholds
    stopped_ec2_threshold_days: int = 7
    snapshot_age_threshold_days: int = 30

    # LLM Advisor. Which backend implements the Advisor port; see
    # bootstrap.ADVISOR_PROVIDERS for the registered names. "template" needs no
    # model at all and is the safe fallback everything else degrades to.
    advisor_provider: str = "ollama"

    # Ollama settings. It runs natively on the host: Docker on Apple Silicon
    # has no GPU passthrough, so a containerized Ollama would be CPU-only.
    # From inside the app container the host daemon is host.docker.internal.
    # Swapping models is just OLLAMA_MODEL — any tag `ollama list` shows works.
    ollama_base_url: str = "http://localhost:11434"
    # Primary model. The 30b MoE activates ~3B params per token, so it runs at
    # roughly 8b latency on a 64GB unified-memory Mac. Set OLLAMA_MODEL to
    # qwen3:8b on smaller hosts (or in CI) — any tag `ollama list` shows works.
    ollama_model: str = "qwen3:30b-a3b"
    ollama_timeout_seconds: float = 30.0
    # Max LLM calls per notify pass, spent on the costliest findings first.
    # Local inference is seconds per finding, so an account with hundreds of
    # findings would otherwise make `sentinel scan` run for hours.
    advisor_max_findings_per_scan: int = 25

    # Idle EC2 detection: an instance is idle only if BOTH average CPU and
    # average network throughput stay under threshold for the whole window.
    ec2_idle_observation_days: int = 14
    ec2_idle_cpu_percent: float = 5.0
    ec2_idle_network_bytes: float = 1_000_000.0
    # Refuse to judge an instance on a near-empty metric series (just-launched
    # instances, or CloudWatch gaps) — too few datapoints means no verdict.
    ec2_idle_min_datapoints: int = 24

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def configured_regions(self) -> list[str]:
        """Regions named by AWS_REGIONS, deduplicated, order preserved.

        Falls back to [AWS_REGION] when unset. May contain the literal "all";
        resolving that needs an AWS call, so it happens in bootstrap, not here.
        """
        ordered = dict.fromkeys(
            region.strip() for region in self.aws_regions.split(",") if region.strip()
        )
        return list(ordered) or [self.aws_region]

    @property
    def scans_all_regions(self) -> bool:
        return ALL_REGIONS in {region.lower() for region in self.configured_regions}

settings = Settings()
