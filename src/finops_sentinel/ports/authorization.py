from abc import ABC, abstractmethod


class Authorizer(ABC):
    """
    Port for deciding whether an actor may approve a remediation.

    Deliberately separate from transport authentication. A Slack signature
    proves a request came through the app; the signing secret is app-level, so
    that proof is shared by everyone who can see the message. It says nothing
    about whether the person who clicked holds the authority to delete
    infrastructure. This port answers that second question, and it is asked in
    the domain so it survives a swap to any other channel.

    Speaks domain language only: an actor is whatever string the channel
    adapter recorded on the Decision.
    """

    @abstractmethod
    def can_approve(self, actor: str) -> bool:
        """True if this actor may approve remediations."""
        ...  # pragma: no cover
