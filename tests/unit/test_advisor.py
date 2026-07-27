"""Advisor port: Ollama adapter happy path and every degradation route.

The port promises summarize() never raises, so each failure mode has to land
on the deterministic domain template instead of propagating.
"""
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from finops_sentinel.adapters.advisor.ollama import AdvisorResponse, OllamaAdvisor
from finops_sentinel.adapters.advisor.template import TemplateAdvisor
from finops_sentinel.config import settings
from finops_sentinel.domain.models import (
    Finding,
    FindingStatus,
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from finops_sentinel.domain.summaries import render_template_summary

BASE_URL = "http://localhost:11434"
CHAT_URL = f"{BASE_URL}/api/chat"


@pytest.fixture
def resource():
    now = datetime.now(UTC)
    return Resource(
        id="res-1",
        resource_id="i-abc123",
        resource_type=ResourceType.EC2_INSTANCE,
        resource_arn="arn:aws:ec2:us-east-1:account:instance/i-abc123",
        region="us-east-1",
        current_tags={"env": "staging"},
        lifecycle=ResourceLifecycle.ACTIVE,
        first_seen_at=now,
        last_seen_at=now,
    )


@pytest.fixture
def finding(resource):
    now = datetime.now(UTC)
    return Finding(
        id="ec2_idle|i-abc123",
        resource_ref=resource.id,
        rule="ec2_idle",
        evidence={
            "InstanceId": "i-abc123",
            "InstanceType": "m5.large",
            "avg_cpu_percent": 0.5,
            "observation_days": 14,
            # Deliberately noisy: must be stripped from the prompt.
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1"}],
        },
        tags_at_detection={"env": "staging"},
        est_monthly_cost_usd=Decimal("70.08"),
        status=FindingStatus.OPEN,
        protected=False,
        detected_at=now,
        last_seen_at=now,
    )


def _ok_body(content: str) -> dict:
    return {"model": "qwen3:8b", "message": {"role": "assistant", "content": content}}


def _valid_content() -> str:
    return json.dumps(
        {
            "summary": "Instance i-abc123 has averaged 0.5% CPU for 14 days.",
            "risk": "low",
            "recommended_action": "Confirm it is not a warm standby, then stop it.",
        }
    )


def advisor() -> OllamaAdvisor:
    return OllamaAdvisor(base_url=BASE_URL, model="qwen3:8b", timeout_seconds=5.0)


@respx.mock
def test_happy_path_uses_model_output(finding, resource):
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_ok_body(_valid_content()))
    )

    summary = advisor().summarize(finding, resource)

    assert route.called
    assert "0.5% CPU" in summary
    assert "risk: low" in summary
    assert "warm standby" in summary


@respx.mock
def test_prompt_omits_unlisted_evidence_keys(finding, resource):
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_ok_body(_valid_content()))
    )

    advisor().summarize(finding, resource)

    sent = json.loads(route.calls[0].request.content)
    prompt = sent["messages"][1]["content"]
    assert "avg_cpu_percent" in prompt
    assert "BlockDeviceMappings" not in prompt
    # observation_days is relabelled: qwen3 read the raw name as uptime
    # ("has been running for 14 days").
    assert "metric_window_days" in prompt
    assert "observation_days" not in prompt
    # Structured output is requested, and reasoning is suppressed by default.
    assert sent["format"]["properties"]["risk"]
    assert sent["think"] is False


@respx.mock
def test_prompt_carries_the_region_and_asks_for_it_back(finding, resource):
    """Scanning many regions makes the same finding shape recur in each; the
    operator needs to know which one they are looking at."""
    resource.region = "ap-southeast-2"
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_ok_body(_valid_content()))
    )

    advisor().summarize(finding, resource)

    sent = json.loads(route.calls[0].request.content)
    assert "ap-southeast-2" in sent["messages"][1]["content"]
    assert "region" in sent["messages"][0]["content"].lower()


def test_template_fallback_always_names_the_region(finding, resource):
    """The deterministic floor must carry the region too — it is what every
    LLM failure degrades to."""
    resource.region = "eu-west-1"

    assert "eu-west-1" in render_template_summary(finding, resource)


@respx.mock
def test_thinking_blocks_are_stripped(finding, resource):
    content = "<think>let me reason about this</think>" + _valid_content()
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_ok_body(content)))

    summary = advisor().summarize(finding, resource)

    assert "let me reason" not in summary
    assert "risk: low" in summary


@respx.mock
def test_400_retries_once_without_think_flag(finding, resource):
    """Models that reject the thinking toggle still get a real answer."""
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "model does not support thinking"}),
            httpx.Response(200, json=_ok_body(_valid_content())),
        ]
    )

    summary = advisor().summarize(finding, resource)

    assert route.call_count == 2
    assert "think" not in json.loads(route.calls[1].request.content)
    assert "risk: low" in summary


@respx.mock
def test_timeout_falls_back_to_template(finding, resource):
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    assert advisor().summarize(finding, resource) == render_template_summary(finding, resource)


@respx.mock
def test_connection_error_falls_back_to_template(finding, resource):
    """Ollama killed mid-run: the pipeline must keep going."""
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    assert advisor().summarize(finding, resource) == render_template_summary(finding, resource)


@respx.mock
def test_server_error_falls_back_to_template(finding, resource):
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, text="boom"))

    assert advisor().summarize(finding, resource) == render_template_summary(finding, resource)


@respx.mock
def test_non_json_content_falls_back_to_template(finding, resource):
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_ok_body("Sure! Here's my advice:"))
    )

    assert advisor().summarize(finding, resource) == render_template_summary(finding, resource)


@respx.mock
def test_schema_violation_falls_back_to_template(finding, resource):
    """Valid JSON, wrong shape (risk outside the enum) is still a failure."""
    bad = json.dumps({"summary": "ok", "risk": "catastrophic", "recommended_action": "x"})
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_ok_body(bad)))

    assert advisor().summarize(finding, resource) == render_template_summary(finding, resource)


@respx.mock
def test_malformed_envelope_falls_back_to_template(finding, resource):
    """Response without the message/content envelope."""
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"done": True}))

    assert advisor().summarize(finding, resource) == render_template_summary(finding, resource)


@respx.mock
def test_advise_raises_so_smoke_llm_can_see_failures(finding, resource):
    """summarize() hides failures by design; advise() must not."""
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(httpx.ConnectError):
        advisor().advise(finding, resource)


def test_advisor_response_rejects_empty_summary():
    with pytest.raises(ValueError):
        AdvisorResponse(summary="", risk="low", recommended_action="x")


def test_template_advisor_matches_domain_renderer(finding, resource):
    assert TemplateAdvisor().summarize(finding, resource) == render_template_summary(
        finding, resource
    )


def test_template_summary_covers_every_rule_and_unknowns(finding, resource):
    for rule in ("ebs_unattached", "eip_orphaned", "ec2_stopped", "ebs_snapshot_old", "ec2_idle"):
        finding.rule = rule
        assert len(render_template_summary(finding, resource)) > 40

    finding.rule = "brand_new_rule"
    summary = render_template_summary(finding, resource)
    assert "brand_new_rule" in summary
    assert "i-abc123" in summary


def test_provider_registry_builds_each_backend():
    """ADVISOR_PROVIDER is the swap point; every registered name must build."""
    from finops_sentinel import bootstrap
    from finops_sentinel.ports.advisor import Advisor

    original = settings.advisor_provider
    try:
        for name in bootstrap.ADVISOR_PROVIDERS:
            settings.advisor_provider = name
            assert isinstance(bootstrap.get_advisor(), Advisor)
    finally:
        settings.advisor_provider = original


def test_unknown_provider_fails_loudly_with_valid_names():
    from finops_sentinel import bootstrap

    original = settings.advisor_provider
    settings.advisor_provider = "gpt-9"
    try:
        with pytest.raises(ValueError, match="Unknown ADVISOR_PROVIDER") as exc:
            bootstrap.get_advisor()
        assert "ollama" in str(exc.value)
        assert "template" in str(exc.value)
    finally:
        settings.advisor_provider = original


def test_model_is_swappable_without_code_changes(finding, resource):
    """A different OLLAMA_MODEL must change nothing but the request body."""
    original_model = settings.ollama_model
    settings.ollama_model = "llama3:8b"
    try:
        from finops_sentinel import bootstrap

        with respx.mock:
            route = respx.post(f"{settings.ollama_base_url}/api/chat").mock(
                return_value=httpx.Response(200, json=_ok_body(_valid_content()))
            )
            bootstrap._build_ollama_advisor().summarize(finding, resource)

        assert json.loads(route.calls[0].request.content)["model"] == "llama3:8b"
    finally:
        settings.ollama_model = original_model
