"""The provider port: what varies between mail systems, and nothing more.

An adapter translates tags, archiving and server-side rules into one provider's
API. It enforces no policy; every rule lives in :mod:`mail_actions.actions`, so an
adapter cannot weaken one by omission.

No method returns message content. The only read of a message is its tags, which
is what the protected check needs; the protected backfill finds messages by
sender but sees only their ids.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from .ids import MessageRef


class TagModel(StrEnum):
    labels = "labels"
    keywords = "keywords"
    folders = "folders"
    categories = "categories"


class ArchiveModel(StrEnum):
    remove_inbox_tag = "remove_inbox_tag"
    move_to_folder = "move_to_folder"


class ServerRules(StrEnum):
    gmail_filters = "gmail_filters"
    sieve = "sieve"
    graph_rules = "graph_rules"
    none = "none"


class ProtectedCheck(StrEnum):
    tag = "tag"
    folder = "folder"


class Match(StrEnum):
    """The sender criterion a server-side rule keys on."""

    from_domain = "from_domain"
    from_addr = "from_addr"
    list_id = "list_id"


@dataclass(frozen=True, slots=True)
class Capabilities:
    tag_model: TagModel
    archive: ArchiveModel
    server_rules: ServerRules
    protected_check: ProtectedCheck


@dataclass(frozen=True, slots=True)
class Rule:
    """A server-side rule: mail matching ``match``/``value`` gets ``tag``, and
    optionally leaves the inbox."""

    match: Match
    value: str
    tag: str
    archive: bool = False

    @property
    def key(self) -> tuple[Match, str]:
        return (self.match, self.value.lower())


@dataclass(frozen=True, slots=True)
class ServerRule:
    """A rule as it exists on the server.

    ``managed`` is true only for rules this service created: the adapter
    recognises its marker and a shape it writes. Unmanaged rules are never
    changed. ``rule`` is None for an unmanaged rule, or when an adapter cannot
    read a managed rule's shape back (such a rule is deleted on the next sync).
    """

    id: str
    managed: bool
    rule: Rule | None


class ProviderError(Exception):
    """A provider call failed; ``reason`` is a short machine-readable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Provider(ABC):
    """One mailbox on one mail system."""

    #: The ``corpus.id/v1`` kind this adapter's refs carry.
    kind: str

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def get_tags(self, ref: MessageRef) -> frozenset[str]:
        """The message's tag names: metadata only, never content."""

    @abstractmethod
    def add_tag(self, ref: MessageRef, tag: str) -> None:
        """Add *tag*; a no-op if it is already present."""

    @abstractmethod
    def remove_tag(self, ref: MessageRef, tag: str) -> None:
        """Remove *tag*; a no-op if it is absent."""

    @abstractmethod
    def archive(self, ref: MessageRef) -> MessageRef:
        """Take the message out of the inbox; idempotent. Returns the ref, which
        changes on providers where archiving moves the message."""

    @abstractmethod
    def unarchive(self, ref: MessageRef) -> MessageRef:
        """Return the message to the inbox; idempotent. Returns the ref."""

    @abstractmethod
    def tag_matching(self, match: Match, value: str, tag: str) -> int:
        """Add *tag* to every existing message whose sender matches
        ``match``/``value``, and return how many matched. Reads message ids only.
        Used to protect mail that predates its protected rule."""

    @abstractmethod
    def get_rules(self) -> list[ServerRule]:
        """Every server-side rule, managed or not. Only when ``server_rules`` is
        not ``none``."""

    @abstractmethod
    def create_rule(self, rule: Rule) -> str:
        """Create a managed rule and return its id."""

    @abstractmethod
    def delete_rule(self, rule_id: str) -> None:
        """Delete the rule with *rule_id*."""
