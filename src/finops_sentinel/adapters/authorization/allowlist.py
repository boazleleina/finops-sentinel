"""Allowlist implementation of the Authorizer port.

The list is a set of actor identifiers as the channel records them — Slack
usernames, or user ids when the workspace hides usernames. Membership is the
whole policy: there are no roles in v1, because a role model that nothing
consults is more code and the same guarantee.
"""
import logging

from finops_sentinel.ports.authorization import Authorizer

logger = logging.getLogger(__name__)


class AllowlistAuthorizer(Authorizer):
    def __init__(self, actors: frozenset[str], allow_all_when_empty: bool = True):
        """
        An empty allowlist means "no approvers configured".

        By default that is treated as an explicit opt-out and every actor is
        allowed, matching how an unset SLACK_SIGNING_SECRET bypasses signature
        verification for local runs — an install that has not configured either
        keeps working. It is logged at WARNING once per process, and
        allow_all_when_empty=False turns an unconfigured deployment into one
        that approves nothing at all.
        """
        self._actors = frozenset(a.strip() for a in actors if a.strip())
        self._allow_all_when_empty = allow_all_when_empty
        self._warned = False

    def can_approve(self, actor: str) -> bool:
        if not self._actors:
            if not self._allow_all_when_empty:
                return False
            if not self._warned:
                logger.warning(
                    "No approvers configured (SENTINEL_APPROVERS is empty) — any actor "
                    "with access to the notification channel can approve remediations."
                )
                self._warned = True
            return True
        return actor in self._actors
