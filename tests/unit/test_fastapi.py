import pytest
from fastapi.testclient import TestClient
from finops_sentinel.adapters.inbound.fastapi_app import app
from finops_sentinel.config import settings

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_slack_callback_missing_payload():
    # Because we don't have a slack_signing_secret in the test environment
    # the signature verification is bypassed.
    settings.slack_signing_secret = None
    response = client.post("/callbacks/slack", data={})
    assert response.status_code == 400
    assert response.json()["detail"] == "No payload found"

def test_slack_callback_with_payload():
    import json
    settings.slack_signing_secret = None
    mock_payload = {
        "type": "block_actions",
        "actions": [{"value": "approve_f-123"}]
    }
    
    response = client.post("/callbacks/slack", data={"payload": json.dumps(mock_payload)})
    assert response.status_code == 200
    assert response.json()["message"] == "Interaction received"
