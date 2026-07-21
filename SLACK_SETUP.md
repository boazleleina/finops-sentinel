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

## Troubleshooting

- **401 Invalid Slack signature:** Ensure your `SLACK_SIGNING_SECRET` matches exactly what is in the Slack dashboard.
- **Buttons don't disappear after clicking:** Ensure your FastAPI app is running and your `ngrok` URL (or production URL) matches exactly the Interactivity Request URL in the Slack dashboard. 
- **Timeouts:** Slack expects a 200 OK response within 3 seconds. FinOps Sentinel performs state updates instantly, but network latency (especially with `ngrok`) can sometimes cause timeouts.
