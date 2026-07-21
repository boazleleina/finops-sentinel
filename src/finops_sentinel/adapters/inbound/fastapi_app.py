import time
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from slack_sdk.signature import SignatureVerifier

from finops_sentinel.config import settings
from finops_sentinel.domain.services import approve_finding, deny_finding
from finops_sentinel.bootstrap import get_repository, get_cloud_gateway

app = FastAPI(title="FinOps Sentinel API", version="0.1.0")

# Setup Slack Signature Verifier if secret is provided
verifier: Optional[SignatureVerifier] = None
if settings.slack_signing_secret:
    verifier = SignatureVerifier(settings.slack_signing_secret)

async def verify_slack_signature(request: Request) -> bool:
    """FastAPI Dependency to verify Slack's webhook signature."""
    if not verifier:
        # If no secret is configured, bypass (useful for local testing without ngrok)
        return True
        
    body = await request.body()
    headers = request.headers
    
    # Slack requires these specific headers
    timestamp = headers.get("X-Slack-Request-Timestamp")
    signature = headers.get("X-Slack-Signature")
    
    if not timestamp or not signature:
        raise HTTPException(status_code=400, detail="Missing Slack signature headers")
        
    # Check for replay attacks (older than 5 minutes)
    if abs(time.time() - int(timestamp)) > 60 * 5:
        raise HTTPException(status_code=400, detail="Slack request timestamp expired")
        
    # Verify the cryptographic signature
    if not verifier.is_valid_request(body, headers):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
        
    return True

@app.get("/health")
def health_check() -> Dict[str, str]:
    return {"status": "ok"}

@app.post("/callbacks/slack")
async def slack_interaction(
    request: Request, 
    _verified: bool = Depends(verify_slack_signature)
) -> Any:
    """
    Webhook endpoint for Slack interactive components (like Approve/Deny buttons).
    """
    # Slack sends interaction payloads as URL-encoded form data where the 
    # actual JSON is inside a 'payload' field.
    form_data = await request.form()
    payload_str = form_data.get("payload")
    
    if not payload_str:
        raise HTTPException(status_code=400, detail="No payload found")
        
    import json
    import httpx
    payload = json.loads(str(payload_str))
    response_url = payload.get("response_url")
    
    # In a full app, these would be injected via FastAPI Depends
    repo = get_repository()
    gateway = get_cloud_gateway()
    
    # Extract the action from the Block Kit payload
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse(content={"message": "No actions to process"})
        
    action = actions[0]
    action_value = action.get("value", "")
    
    if action_value.startswith("approve_"):
        finding_id = action_value.replace("approve_", "")
        success = approve_finding(finding_id, repo, gateway)
        if success:
            original_blocks = payload.get("message", {}).get("blocks", [])
            new_blocks = []
            if original_blocks:
                new_blocks.append(original_blocks[0]) # The text block
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "✅ *Action taken:* Approved and Remediated."
                }
            })
            if response_url:
                httpx.post(response_url, json={
                    "replace_original": True,
                    "blocks": new_blocks
                })
            return JSONResponse(content={"message": "ok"})
        else:
            return JSONResponse(content={"message": f"Failed to approve finding {finding_id}"}, status_code=400)
            
    elif action_value.startswith("deny_"):
        finding_id = action_value.replace("deny_", "")
        success = deny_finding(finding_id, repo)
        if success:
            original_blocks = payload.get("message", {}).get("blocks", [])
            new_blocks = []
            if original_blocks:
                new_blocks.append(original_blocks[0]) # The text block
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "❌ *Action taken:* Denied."
                }
            })
            
            if response_url:
                httpx.post(response_url, json={
                    "replace_original": True,
                    "blocks": new_blocks
                })
            return JSONResponse(content={"message": "ok"})
        else:
            return JSONResponse(content={"message": f"Failed to deny finding {finding_id}"}, status_code=400)
            
    return JSONResponse(content={"message": "Interaction received but not recognized"})

# Run via: uvicorn finops_sentinel.adapters.inbound.fastapi_app:app --reload
