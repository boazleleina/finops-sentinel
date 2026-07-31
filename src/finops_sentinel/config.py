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
    # Provenance, not authority. The signing secret is app-level, so a
    # well-signed request only proves it came through this app — including from
    # a second workspace the app was installed into, or a channel somebody
    # widened. These pin which workspace and which channels the callback
    # endpoint accepts. Unset means unrestricted, like an unset signing secret.
    slack_team_id: str | None = None
    slack_allowed_channel_ids: str = ""

    # Who may approve a remediation. Comma-separated, either bare identifiers
    # ("U024BE7LH") or identifier=role pairs
    # ("U024BE7LH=arn:aws:iam::123456789012:role/finops-approver").
    #
    # Prefer the channel's stable *user id* over a display name: usernames
    # change, display names are user-controlled, and this string decides who
    # gets AWS credentials.
    #
    # Authority, not provenance — checked in the domain, so it survives a
    # channel swap. Empty means unconfigured: see AllowlistAuthorizer.
    sentinel_approvers: str = ""

    # Run each playbook under a role assumed for the approver, with a session
    # policy narrowed to the one resource, instead of under Sentinel's own
    # credentials. This is what makes AWS — rather than Sentinel's approver
    # list — the thing that authorizes a deletion.
    #
    # Defaults to false because it needs IAM that a fresh clone does not have,
    # and because LocalStack Community does not evaluate IAM policies: an
    # assume-role run there proves the wiring and nothing about enforcement.
    # Turn it on in any account whose resources you would mind losing.
    sentinel_assume_role: bool = False
    # Shared secret required by the approver role's trust policy. The standard
    # confused-deputy control: the role cannot be assumed by anything that does
    # not hold it, even if its ARN leaks.
    sentinel_approver_external_id: str | None = None
    # Session lifetime. Fifteen minutes is the AssumeRole minimum and is far
    # more than any playbook needs — the EBS snapshot wait is the long one.
    sentinel_session_duration_seconds: int = 900


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

    # Idle RDS detection: a database nothing connects to is serving nobody.
    # DatabaseConnections is the signal, not CPU — a replica can be busy on CPU
    # and useless, or quiet on CPU and essential.
    rds_idle_observation_days: int = 14
    # Average connections at or below this is idle. Zero is the honest default;
    # raise it if monitoring agents or connection poolers keep a permanent
    # baseline open against every database.
    rds_idle_max_connections: float = 0.0
    rds_idle_min_datapoints: int = 24
    # There is no rds_stopped threshold on purpose: DescribeDBInstances exposes
    # no stopped-since timestamp, and AWS auto-restarts a stopped instance
    # after 7 days anyway, so the state itself is the finding.

    # S3. Below this size a missing lifecycle policy is not worth an alert —
    # the policy costs more argument than the storage does money.
    s3_min_bucket_size_gb: float = 50.0
    # Multipart uploads older than this are abandoned, not in flight. Used both
    # to detect and, re-checked, to decide what the abort playbook may touch.
    s3_incomplete_mpu_age_days: int = 7
    # Share of a bucket's storage cost a lifecycle policy could plausibly
    # recover. A bucket without a policy is NOT wholly waste, and reporting its
    # full cost as savings would let one large bucket dominate the total.
    s3_lifecycle_addressable_fraction: float = 0.20

    # Right-sizing digest. Advisory only — there is no playbook that resizes an
    # instance, and there will not be one: the change needs a stop/start and an
    # architecture decision (Graviton), which is a deploy, not a cleanup.
    rightsizing_observation_days: int = 14
    # PEAK CPU below this suggests a downsize. Peak, never average: a box that
    # spikes to 90% once an hour is correctly sized however low its mean is.
    # Paired with a candidate list that steps down at most one size — halving
    # vCPU roughly doubles utilisation, so 40% peak lands near 80% after.
    rightsizing_cpu_headroom_percent: float = 40.0
    rightsizing_min_datapoints: int = 24
    # Cap on digest suggestions. Readability, not cost: the ones worth acting
    # on sort to the top by saving.
    digest_max_items: int = 10

    # Spend anomaly. What is measured is estimated monthly WASTE (the total of
    # live findings), not billed spend — there is no billing data in this
    # system until a Cost Explorer adapter exists.
    anomaly_window_days: int = 14
    # Below this many days of history there is no verdict. An anomaly alert
    # that fires on three days of data is one people learn to ignore.
    anomaly_min_history_days: int = 7
    # |z| at or above this is an anomaly. 2.0 is roughly the top/bottom 5% of a
    # normal distribution — frequent enough to be useful, rare enough to read.
    anomaly_z_threshold: float = 2.0

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
    def approver_roles(self) -> dict[str, str | None]:
        """Actor -> approver role ARN, parsed from SENTINEL_APPROVERS.

        One table, two consumers: the Authorizer takes the keys, and the
        assume-role gateway factory takes the values. Parsing them separately
        is how the allowlist and the credential source drift apart.

        A bare entry maps to None — permitted to approve, no role to assume,
        which only works when SENTINEL_ASSUME_ROLE is off.
        """
        parsed: dict[str, str | None] = {}
        for entry in self.sentinel_approvers.split(","):
            actor, _, role_arn = entry.strip().partition("=")
            if actor.strip():
                parsed[actor.strip()] = role_arn.strip() or None
        return parsed

    @property
    def approver_actors(self) -> frozenset[str]:
        """Actors permitted to approve, parsed from SENTINEL_APPROVERS."""
        return frozenset(self.approver_roles)

    @property
    def allowed_slack_channels(self) -> frozenset[str]:
        """Channel ids the Slack callback endpoint accepts. Empty means any."""
        return frozenset(
            c.strip() for c in self.slack_allowed_channel_ids.split(",") if c.strip()
        )

    @property
    def scans_all_regions(self) -> bool:
        return ALL_REGIONS in {region.lower() for region in self.configured_regions}

settings = Settings()


def database_url() -> str:
    """The one place the findings database URL is built.

    Both the application (bootstrap.get_repository) and Alembic (alembic/env.py)
    call this. They used to build it independently — env.py read the raw
    environment with its own default while the app went through Settings, which
    reads .env too. The documented setup puts SENTINEL_DB_PATH in .env, so
    `alembic upgrade head` migrated .sentinel.db while `sentinel scan` used
    data/sentinel.db, and nothing failed until the app reached a table the
    migration had created somewhere else entirely.

    One function, so the two cannot drift again.
    """
    return f"sqlite:///{settings.sentinel_db_path}"
