"""Composition root: reads config and wires concrete adapters into ports.

The ONLY file that knows which concrete adapters exist.
"""
import logging
from collections.abc import Callable

from finops_sentinel.adapters.advisor.ollama import OllamaAdvisor
from finops_sentinel.adapters.advisor.template import TemplateAdvisor
from finops_sentinel.adapters.authorization.allowlist import AllowlistAuthorizer
from finops_sentinel.adapters.aws.approval_credentials import AssumeRoleGatewayFactory
from finops_sentinel.adapters.aws.gateway import Boto3Gateway, list_enabled_regions
from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.adapters.aws.scanners.ebs import UnattachedEBSScanner
from finops_sentinel.adapters.aws.scanners.ebs_snapshots import OldEbsSnapshotScanner
from finops_sentinel.adapters.aws.scanners.ec2 import StoppedEC2Scanner
from finops_sentinel.adapters.aws.scanners.ec2_idle import IdleEC2Scanner
from finops_sentinel.adapters.aws.scanners.eip import OrphanedEIPScanner
from finops_sentinel.adapters.aws.scanners.rds import IdleRDSScanner, StoppedRDSScanner
from finops_sentinel.adapters.aws.scanners.s3 import S3LifecycleScanner
from finops_sentinel.adapters.notifications.console import ConsoleNotifier
from finops_sentinel.adapters.notifications.slack import SlackAdapter
from finops_sentinel.adapters.persistence.sqlalchemy_repo import SqlAlchemyRepository
from finops_sentinel.config import database_url, settings
from finops_sentinel.domain.services import ApprovalPlan, MetricTarget, ScanTarget
from finops_sentinel.ports.advisor import Advisor
from finops_sentinel.ports.authorization import Authorizer
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.pricing import Pricing
from finops_sentinel.ports.repository import FindingsRepository
from finops_sentinel.ports.scanner import Scanner

logger = logging.getLogger(__name__)


def get_regions() -> list[str]:
    """The regions a scan will cover.

    AWS_REGIONS=all is resolved live against the account, so a newly enabled
    region is picked up without a config change. If that lookup fails (no
    ec2:DescribeRegions grant, endpoint that does not implement it) the scan
    still runs, but only over the home region — so the failure is logged at
    ERROR rather than swallowed: a silent narrowing to one region would look
    exactly like an account with nothing to find.
    """
    if not settings.scans_all_regions:
        return settings.configured_regions

    try:
        regions = list_enabled_regions(
            region=settings.aws_region,
            endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    except Exception as exc:  # noqa: BLE001 — any discovery failure degrades the same way
        logger.error(
            "AWS_REGIONS=all but region discovery failed (%s); scanning only %s. "
            "Grant ec2:DescribeRegions, or list regions explicitly in AWS_REGIONS.",
            exc,
            settings.aws_region,
        )
        return [settings.aws_region]

    return regions or [settings.aws_region]


def get_cloud_gateway(region: str | None = None) -> CloudGateway:
    """A gateway bound to one region; defaults to the home region.

    Sentinel's own identity: read-only in any account configured the way
    docs/iam-policies.md describes. Scanning uses this. Remediation does not —
    see get_approval_gateway_factory.
    """
    return Boto3Gateway(
        region=region or settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        mpu_age_days=settings.s3_incomplete_mpu_age_days,
    )


def get_repository() -> FindingsRepository:
    # Same builder Alembic uses, so a migration can never target a different
    # file than the one the app reads and writes.
    return SqlAlchemyRepository(db_url=database_url())


def get_authorizer() -> Authorizer:
    """Who may approve.

    With SENTINEL_ASSUME_ROLE on, an approver without a mapped role is not an
    approver: the remediation would have no credentials to run under, and
    finding that out after the CAS has committed means a finding stranded in
    FAILED for a configuration mistake. Refuse it at the guardrail instead,
    where it is audited as approve_blocked_unauthorized like every other
    refusal.
    """
    roles = settings.approver_roles
    if not settings.sentinel_assume_role:
        return AllowlistAuthorizer(frozenset(roles))

    with_roles = frozenset(actor for actor, role_arn in roles.items() if role_arn)
    missing = sorted(set(roles) - with_roles)
    if missing:
        logger.warning(
            "SENTINEL_ASSUME_ROLE is on and these approvers have no role ARN, so they "
            "cannot approve: %s. Use actor=arn:aws:iam::<account>:role/<role> entries.",
            ", ".join(missing),
        )
    return AllowlistAuthorizer(with_roles)


def get_approval_gateway_factory() -> Callable[[ApprovalPlan], CloudGateway]:
    """The credentials an approved playbook runs under.

    With SENTINEL_ASSUME_ROLE on, each approval assumes the approver's role
    with a session policy scoped to that one resource, so AWS authorizes the
    deletion against a principal that names a person, and Sentinel's own role
    never needs a destructive permission at all.

    Off, everything runs under Sentinel's own credentials — which means the
    approver list is Sentinel's own gate and nothing more. That is the right
    default for LocalStack (Community evaluates no IAM policies) and the wrong
    one for an account you would mind losing, so it says so out loud.
    """
    if not settings.sentinel_assume_role:
        logger.warning(
            "SENTINEL_ASSUME_ROLE is off: remediations run under Sentinel's own AWS "
            "credentials, so approver identity is not enforced by IAM."
        )
        return lambda plan: get_cloud_gateway(plan.region)

    return AssumeRoleGatewayFactory(
        role_for_actor={
            actor: role_arn
            for actor, role_arn in settings.approver_roles.items()
            if role_arn is not None
        },
        external_id=settings.sentinel_approver_external_id,
        session_duration_seconds=settings.sentinel_session_duration_seconds,
        endpoint_url=settings.aws_endpoint_url,
        mpu_age_days=settings.s3_incomplete_mpu_age_days,
    )


def get_notifier() -> Notifier:
    if settings.slack_webhook_url:
        return SlackAdapter()
    return ConsoleNotifier()


def _build_ollama_advisor() -> Advisor:
    return OllamaAdvisor(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
    )


# The Advisor swap point. Two levels of pluggability:
#
#   1. Different model, same backend — set OLLAMA_MODEL to any tag `ollama list`
#      shows. No code change; nothing here needs touching.
#   2. Different backend entirely (a hosted API, vLLM, llama.cpp) — write one
#      adapter implementing ports.advisor.Advisor and add one entry below, then
#      set ADVISOR_PROVIDER to its name. Nothing outside this dict and the new
#      adapter file changes, because callers only ever see the port.
ADVISOR_PROVIDERS: dict[str, Callable[[], Advisor]] = {
    "ollama": _build_ollama_advisor,
    "template": TemplateAdvisor,
}


def get_advisor() -> Advisor:
    """Build the configured Advisor.

    All providers satisfy the same port, so nothing downstream branches on
    which one is in play — OllamaAdvisor already degrades to the deterministic
    template internally when its backend misbehaves.
    """
    try:
        factory = ADVISOR_PROVIDERS[settings.advisor_provider]
    except KeyError:
        valid = ", ".join(sorted(ADVISOR_PROVIDERS))
        raise ValueError(
            f"Unknown ADVISOR_PROVIDER {settings.advisor_provider!r}. Valid: {valid}"
        ) from None
    return factory()


def get_pricing() -> Pricing:
    """Every price the system uses comes from here.

    Swapping to live AWS rates means adding an adapter that implements the
    Pricing port and returning it from this function — no scanner changes.
    """
    return StaticPricing()


def get_scanners(region: str | None = None) -> list[Scanner]:
    """A fresh scanner set for one region.

    Never share these between regions: each stamps its region onto every
    Resource it discovers, and IdleEC2Scanner caches metric series on itself
    between discover() and evaluate().
    """
    region = region or settings.aws_region
    pricing = get_pricing()
    return [
        UnattachedEBSScanner(region=region, pricing=pricing),
        OrphanedEIPScanner(region=region, pricing=pricing),
        StoppedEC2Scanner(
            region=region,
            pricing=pricing,
            threshold_days=settings.stopped_ec2_threshold_days,
        ),
        OldEbsSnapshotScanner(
            region=region,
            pricing=pricing,
            age_threshold_days=settings.snapshot_age_threshold_days,
        ),
        IdleEC2Scanner(
            region=region,
            pricing=pricing,
            observation_days=settings.ec2_idle_observation_days,
            cpu_threshold_percent=settings.ec2_idle_cpu_percent,
            network_threshold_bytes=settings.ec2_idle_network_bytes,
            min_datapoints=settings.ec2_idle_min_datapoints,
        ),
        IdleRDSScanner(
            region=region,
            pricing=pricing,
            observation_days=settings.rds_idle_observation_days,
            max_connections=settings.rds_idle_max_connections,
            min_datapoints=settings.rds_idle_min_datapoints,
        ),
        StoppedRDSScanner(region=region, pricing=pricing),
        S3LifecycleScanner(
            region=region,
            pricing=pricing,
            min_bucket_size_gb=settings.s3_min_bucket_size_gb,
            incomplete_mpu_age_days=settings.s3_incomplete_mpu_age_days,
            addressable_fraction=settings.s3_lifecycle_addressable_fraction,
        ),
    ]


def get_digest_targets() -> list[MetricTarget]:
    """One gateway per configured region, with no scanners attached.

    The digest reads CloudWatch and discovers nothing, so building eight
    scanners per region to make one metric call each would be pure waste.
    """
    return [MetricTarget(region=region, gateway=get_cloud_gateway(region)) for region in get_regions()]


def get_scan_targets() -> list[ScanTarget]:
    """One gateway + scanner set per configured region.

    Built eagerly on the calling thread: boto3 client construction is not
    thread-safe, and run_scan discovers regions in parallel.
    """
    return [
        ScanTarget(
            region=region,
            gateway=get_cloud_gateway(region),
            scanners=get_scanners(region),
        )
        for region in get_regions()
    ]
