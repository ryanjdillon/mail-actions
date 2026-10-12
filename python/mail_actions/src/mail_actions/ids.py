"""Codec for ``corpus.id/v1`` message references: ``<kind>:<mailbox>::<provider-uid>``.

The reference format is the only contract shared with corpus; this module
implements it independently against its own test vectors, so neither project
imports the other. The uid grammar is per kind, and anything that does not
match it exactly is refused rather than repaired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTRACT = "corpus.id/v1"

# Both kind and mailbox share this grammar: no '.', no ':', no uppercase.
_NAME = re.compile(r"[a-z0-9_-]{1,32}")
# Generous for real ids, but bounded: a ref is attacker-supplied input.
_MAX_REF = 1024
# A canonical positive integer: no sign, no leading zero, never 0.
_POSINT = r"[1-9][0-9]*"
_UID = {
    "gmail": re.compile(r"[0-9a-f]{8,32}"),
    # <folder>:<uidvalidity>:<uid>. The two integers are the last two
    # ':'-separated parts, so a folder may itself contain ':' or '::';
    # control characters never belong in one.
    "imap": re.compile(rf"[^\x00-\x1f\x7f-\x9f]+:{_POSINT}:{_POSINT}"),
    "jmap": re.compile(r"[A-Za-z0-9_-]{1,255}"),
    "graph": re.compile(r"[A-Za-z0-9_=+/-]{1,512}"),
}
KINDS = frozenset(_UID)


class InvalidRef(ValueError):
    """A string that is not a valid ``corpus.id/v1`` reference."""


class UnknownKind(InvalidRef):
    """A well-formed kind this codec does not implement (e.g. a newer provider)."""


@dataclass(frozen=True, slots=True)
class MessageRef:
    kind: str
    mailbox: str
    uid: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.mailbox}::{self.uid}"

    @property
    def source(self) -> str:
        """``kind:mailbox``: the part of a ref that is safe to log verbatim."""
        return f"{self.kind}:{self.mailbox}"

    def imap_parts(self) -> tuple[str, int, int]:
        """``(folder, uidvalidity, uid)`` of an ``imap`` ref."""
        if self.kind != "imap":
            raise ValueError(f"not an imap ref: {self.kind}")
        folder, uidvalidity, uid = self.uid.rsplit(":", 2)
        return folder, int(uidvalidity), int(uid)


def parse_ref(value: str) -> MessageRef:
    """Parse *value* as a ``corpus.id/v1`` reference, or raise :class:`InvalidRef`.

    The whole string must match: no surrounding whitespace or newline. The kind
    ends at the first ':' and the mailbox at the first '::' after it; the rest is
    the uid. A kind that is well formed but not one of :data:`KINDS` raises
    :class:`UnknownKind`, so callers can tell it from garbage.
    """
    if not isinstance(value, str) or len(value) > _MAX_REF:
        raise InvalidRef("not a reference")
    kind, colon, rest = value.partition(":")
    mailbox, sep, uid = rest.partition("::")
    if not (colon and sep) or not _NAME.fullmatch(kind):
        raise InvalidRef("not a reference")
    if not _NAME.fullmatch(mailbox):
        raise InvalidRef("invalid mailbox")
    if kind not in KINDS:
        raise UnknownKind(f"unknown kind: {kind}")
    if not _UID[kind].fullmatch(uid):
        raise InvalidRef(f"invalid {kind} uid")
    return MessageRef(kind, mailbox, uid)
