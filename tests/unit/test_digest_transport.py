"""The digest transport: Notifier.send_digest and Advisor.narrate.

Both are advisory by contract, and both tests here exist to pin that down: a
digest must carry no approve affordance, and narration must never be able to
take a notification down or change a number the domain computed.
"""
import httpx
import pytest
import respx

from finops_sentinel.adapters.advisor.ollama import OllamaAdvisor
from finops_sentinel.adapters.advisor.template import TemplateAdvisor
from finops_sentinel.adapters.notifications.console import ConsoleNotifier
from finops_sentinel.adapters.notifications.slack import SlackAdapter
from finops_sentinel.config import settings
from finops_sentinel.domain.summaries import render_template_narration

ANOMALY_FACTS = {
    "date": "2026-07-27",
    "value": "412.50",
    "mean": "180.00",
    "z_score": 3.1,
    "window_days": 14,
    "direction": "increase",
}


# --------------------------------------------------------------------------
# The deterministic floor
# --------------------------------------------------------------------------


def test_template_narration_uses_only_the_facts_it_was_given():
    text = render_template_narration("spend_anomaly", ANOMALY_FACTS)

    assert "412.50" in text
    assert "180.00" in text
    assert "3.1" in text
    assert "14 days" in text


def test_template_narration_covers_the_rightsizing_topic():
    text = render_template_narration(
        "rightsizing", {"count": 4, "window_days": 14, "total_saving": "312.40"}
    )

    assert "4 instance(s)" in text
    assert "312.40" in text
    # Says out loud that peak, not average, drove it — the judgement an
    # operator needs in order to disagree with the suggestion.
    assert "peak utilisation" in text


def test_template_narration_survives_an_unknown_topic():
    """A digest must not die because a new section type has no copy yet."""
    text = render_template_narration("something_new", {"count": 3})

    assert "Something new" in text
    assert "count: 3" in text


def test_template_narration_survives_missing_facts():
    """Half a sentence beats an exception in the middle of a notification."""
    assert render_template_narration("spend_anomaly", {}) != ""


def test_template_advisor_narrates_without_a_model():
    advisor = TemplateAdvisor()

    assert advisor.narrate("spend_anomaly", ANOMALY_FACTS) == render_template_narration(
        "spend_anomaly", ANOMALY_FACTS
    )


# --------------------------------------------------------------------------
# The LLM path, and every way it is allowed to fail
# --------------------------------------------------------------------------


def _advisor() -> OllamaAdvisor:
    return OllamaAdvisor(base_url="http://ollama.test", model="test-model", timeout_seconds=1)


@respx.mock
def test_narrate_returns_model_prose_when_the_schema_holds():
    respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"content": '{"narrative": "Waste more than doubled."}'}}
        )
    )

    assert _advisor().narrate("spend_anomaly", ANOMALY_FACTS) == "Waste more than doubled."


@respx.mock
def test_narrate_strips_reasoning_tags():
    """qwen3 wraps output in <think> blocks even with thinking disabled."""
    respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content": '<think>hmm</think>{"narrative": "Spend rose sharply."}'
                }
            },
        )
    )

    assert _advisor().narrate("spend_anomaly", ANOMALY_FACTS) == "Spend rose sharply."


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="upstream exploded"),
        httpx.Response(200, json={"message": {"content": "not json at all"}}),
        httpx.Response(200, json={"message": {"content": '{"wrong_field": "x"}'}}),
        httpx.Response(200, json={"message": {"content": '{"narrative": ""}'}}),
    ],
    ids=["http-error", "not-json", "schema-violation", "empty-narrative"],
)
@respx.mock
def test_every_narration_failure_degrades_to_the_template(response):
    """The port forbids raising: a dead model costs prose, never the digest."""
    respx.post("http://ollama.test/api/chat").mock(return_value=response)

    assert _advisor().narrate("spend_anomaly", ANOMALY_FACTS) == render_template_narration(
        "spend_anomaly", ANOMALY_FACTS
    )


@respx.mock
def test_narration_transport_failure_degrades_to_the_template():
    respx.post("http://ollama.test/api/chat").mock(side_effect=httpx.ConnectError("refused"))

    assert _advisor().narrate("spend_anomaly", ANOMALY_FACTS) == render_template_narration(
        "spend_anomaly", ANOMALY_FACTS
    )


@respx.mock
def test_narration_and_summaries_use_different_schemas():
    """A narration carries no risk verdict — there is nothing to act on."""
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": '{"narrative": "ok"}'}})
    )

    _advisor().narrate("spend_anomaly", ANOMALY_FACTS)

    schema = route.calls[0].request.read().decode()
    assert "narrative" in schema
    assert "recommended_action" not in schema


# --------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------


def test_console_notifier_logs_the_digest(caplog):
    with caplog.at_level("INFO"):
        assert ConsoleNotifier().send_digest("Weekly digest", ["one", "two"]) is None

    assert "Weekly digest" in caplog.text
    assert "one" in caplog.text
    assert "two" in caplog.text


def test_slack_digest_carries_no_approve_affordance(monkeypatch):
    """The whole point of the method. A digest has no finding id to decide on.

    Rendering a button here would offer an action the domain could not perform
    even if someone clicked it.
    """
    monkeypatch.setattr(settings, "slack_webhook_url", "https://hooks.slack.test/x")
    sent: dict = {}

    class FakeWebhookClient:
        def __init__(self, url):
            self.url = url

        def send(self, text, blocks):
            sent["text"] = text
            sent["blocks"] = blocks
            return type("R", (), {"status_code": 200, "body": "ok"})()

    monkeypatch.setattr(
        "finops_sentinel.adapters.notifications.slack.WebhookClient", FakeWebhookClient
    )

    SlackAdapter().send_digest("Weekly digest", ["section one", "section two"])

    block_types = [block["type"] for block in sent["blocks"]]
    assert "actions" not in block_types
    assert block_types[0] == "header"
    assert block_types.count("section") == 2
    assert sent["text"] == "Weekly digest"
    rendered = str(sent["blocks"])
    assert "Approve" not in rendered
    assert "Deny" not in rendered


def test_slack_digest_refuses_without_a_webhook(monkeypatch):
    monkeypatch.setattr(settings, "slack_webhook_url", None)

    with pytest.raises(RuntimeError, match="SLACK_WEBHOOK_URL"):
        SlackAdapter().send_digest("Weekly digest", ["section"])
