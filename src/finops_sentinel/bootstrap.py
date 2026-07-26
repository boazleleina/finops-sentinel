"""Composition root: reads config and wires concrete adapters into ports.

The ONLY file that knows which concrete adapters exist.
"""
from collections.abc import Callable

from finops_sentinel.adapters.advisor.ollama import OllamaAdvisor
from finops_sentinel.adapters.advisor.template import TemplateAdvisor
from finops_sentinel.adapters.aws.gateway import Boto3Gateway
from finops_sentinel.adapters.aws.pricing import StaticPricing
from finops_sentinel.adapters.aws.scanners.ebs import UnattachedEBSScanner
from finops_sentinel.adapters.aws.scanners.ebs_snapshots import OldEbsSnapshotScanner
from finops_sentinel.adapters.aws.scanners.ec2 import StoppedEC2Scanner
from finops_sentinel.adapters.aws.scanners.ec2_idle import IdleEC2Scanner
from finops_sentinel.adapters.aws.scanners.eip import OrphanedEIPScanner
from finops_sentinel.adapters.notifications.console import ConsoleNotifier
from finops_sentinel.adapters.notifications.slack import SlackAdapter
from finops_sentinel.adapters.persistence.sqlalchemy_repo import SqlAlchemyRepository
from finops_sentinel.config import settings
from finops_sentinel.ports.advisor import Advisor
from finops_sentinel.ports.cloud import CloudGateway
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.ports.pricing import Pricing
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


def get_scanners() -> list[Scanner]:
    pricing = get_pricing()
    return [
        UnattachedEBSScanner(region=settings.aws_region, pricing=pricing),
        OrphanedEIPScanner(region=settings.aws_region, pricing=pricing),
        StoppedEC2Scanner(
            region=settings.aws_region,
            pricing=pricing,
            threshold_days=settings.stopped_ec2_threshold_days,
        ),
        OldEbsSnapshotScanner(
            region=settings.aws_region,
            pricing=pricing,
            age_threshold_days=settings.snapshot_age_threshold_days,
        ),
        IdleEC2Scanner(
            region=settings.aws_region,
            pricing=pricing,
            observation_days=settings.ec2_idle_observation_days,
            cpu_threshold_percent=settings.ec2_idle_cpu_percent,
            network_threshold_bytes=settings.ec2_idle_network_bytes,
            min_datapoints=settings.ec2_idle_min_datapoints,
        ),
    ]
