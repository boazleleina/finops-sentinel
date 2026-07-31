# Slack Integration Setup Guide

FinOps Sentinel uses Slack Block Kit and interactive webhooks to send cost optimization alerts directly to your channel, allowing you to Approve or Deny remediation actions with a single click.

Follow these steps to set up the Slack integration.

## 1. Create a Slack App

1. Go to [Slack API: Applications](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From scratch**.
3. Name your app (e.g., *FinOps Sentinel*) and select your workspace.
4. Click **Create App**.

## 2. Enable Incoming Webhooks

1. In your app settings menu, go to **Incoming Webhooks**.
2. Toggle **Activate Incoming Webhooks** to **On**.
3. Click **Add New Webhook to Workspace**.
4. Select the channel where you want Sentinel to post alerts (e.g., `#finops-alerts`) and click **Allow**.
5. Copy the generated **Webhook URL**. You will add this to your `.env` file as `SLACK_WEBHOOK_URL`.

## 3. Configure Interactivity (Action Buttons)

To allow Sentinel to receive the "Approve" or "Deny" button clicks:

1. In your app settings menu, go to **Interactivity & Shortcuts**.
2. Toggle **Interactivity** to **On**.
3. Under **Request URL**, enter the public URL for your FastAPI server callback route.
   - *For local development:* Use a tunnel like `ngrok` (e.g., `https://your-ngrok-url.ngrok-free.app/callbacks/slack`).
   - *For production:* Use your deployed API Gateway endpoint.
4. Click **Save Changes** in the bottom right corner.

## 4. Get the Signing Secret

Sentinel verifies that incoming button clicks are actually from Slack using a cryptographic signature.

1. Go to **Basic Information** in the app settings menu.
2. Scroll down to **App Credentials**.
3. Click **Show** next to **Signing Secret**.
4. Copy the secret. You will add this to your `.env` file as `SLACK_SIGNING_SECRET`.

## 5. Update your `.env` file

Add the webhook URL and signing secret to your `.env` file at the root of the project:

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/TXXXXX/BXXXXX/XXXXXXX
SLACK_SIGNING_SECRET=your_signing_secret_here
```

## 6. Decide who may approve

The signing secret is app-level. A valid signature proves a request came through your app — it does not prove the person who clicked is allowed to delete infrastructure, and everyone who can see the message can produce one.

Two settings close that gap, and they answer different questions:

```env
# Provenance: which install and which channels this endpoint accepts.
# Find the team id under Basic Information → App Credentials, and the channel
# id at the bottom of the channel's "View channel details" dialog.
SLACK_TEAM_ID=TXXXXXXXX
SLACK_ALLOWED_CHANNEL_IDS=CXXXXXXXX

# Authority: who may approve, by Slack username (user id where usernames are
# hidden). Checked in the domain, so it applies to the HTTP API too.
SENTINEL_APPROVERS=boaz,ops-oncall
```

Leave `SENTINEL_APPROVERS` empty and anyone who can see the message can approve; the app logs a WARNING at startup of the first approval saying so. A refused attempt is recorded as `approve_blocked_unauthorized` in the audit log, so denied clicks are as visible as accepted ones.

## Troubleshooting

- **401 Invalid Slack signature:** Ensure your `SLACK_SIGNING_SECRET` matches exactly what is in the Slack dashboard.
- **401 Callback from unexpected Slack workspace / channel:** `SLACK_TEAM_ID` or `SLACK_ALLOWED_CHANNEL_IDS` does not match the install the click came from. Clear them to accept any.
- **"@someone is not permitted to approve remediations":** that actor is not in `SENTINEL_APPROVERS`. Slack usernames, comma-separated, no `@`.
- **Buttons don't disappear after clicking:** Ensure your FastAPI app is running and your `ngrok` URL (or production URL) matches exactly the Interactivity Request URL in the Slack dashboard. 
- **Timeouts:** Slack expects a 200 OK within 3 seconds. Approvals answer in two messages for exactly that reason — the guardrails and the state change run inline (milliseconds), the message is edited to remove the buttons, and the playbook runs in the background, editing the message again when it finishes. A snapshot-then-delete can take minutes; the buttons are gone the whole time, and a click that slips through is refused because the finding has already left `NOTIFIED`.
