import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from finops_sentinel.ports.notifier import Notifier
from finops_sentinel.domain.models import Finding, Resource
from finops_sentinel.config import settings

logger = logging.getLogger(__name__)

class SlackAdapter(Notifier):
    """
    Implements the Notifier port for Slack.
    Uses Block Kit to send interactive messages with Approve/Deny buttons.
    """
    def __init__(self, channel: str = "#finops-alerts"):
        self.channel = channel
        
        # In a real app, you'd use a bot token. Since we only have a webhook URL
        # in the config currently, we can either use the webhook or the WebClient.
        # Interactive buttons (Block Kit actions) require updating the message 
        # later, which is best done with a bot token and the WebClient.
        # But if you only provided a webhook_url, we can use the WebhookClient.
        from slack_sdk.webhook import WebhookClient
        if settings.slack_webhook_url:
            self.client = WebhookClient(settings.slack_webhook_url)
        else:
            self.client = None
            logger.warning("No SLACK_WEBHOOK_URL provided. Slack notifications disabled.")

    def send_finding_alert(self, finding: Finding, resource: Resource) -> None:
        if not self.client:
            return
            
        if settings.dry_run:
            logger.info(f"[DRY RUN] Would send Slack alert for finding: {finding.id}")
            return
            
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🚨 *FinOps Alert: Waste Detected*\n\n*Rule:* {finding.rule}\n*Resource:* `{resource.resource_id}` ({resource.resource_type})\n*Cost Impact:* ${finding.est_monthly_cost_usd}/mo"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve Remediation"
                        },
                        "style": "primary",
                        "value": f"approve_{finding.id}",
                        "action_id": "approve_remediation"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Deny"
                        },
                        "style": "danger",
                        "value": f"deny_{finding.id}",
                        "action_id": "deny_remediation"
                    }
                ]
            }
        ]

        try:
            response = self.client.send(
                text=f"FinOps Alert: {finding.rule} on {resource.resource_id}",
                blocks=blocks
            )
            logger.info(f"Successfully sent Slack alert for finding {finding.id}")
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {str(e)}")
