"""The MCP surface: exactly five tools over streamable HTTP.

Each tool is a thin call into :class:`MailboxActions`, which owns every rule and
the audit line; this module adds only the caller's principal.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .actions import MailboxActions

#: The request header carrying the caller's principal. The gateway overwrites it
#: from the authenticated API key, so a client cannot choose its own.
PRINCIPAL_HEADER = "x-client-id"
UNKNOWN_PRINCIPAL = "unknown"  # no header: a caller that bypassed the gateway
AMBIGUOUS_PRINCIPAL = "ambiguous"  # the header sent more than once
OTHER_PRINCIPAL = "other"  # not shaped like a client id
_PRINCIPAL = re.compile(r"[a-z0-9_-]{1,32}")

_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def principal() -> str:
    """The calling principal from the current MCP request's ``x-client-id``."""
    from mcp.server.lowlevel.server import request_ctx

    try:
        request = request_ctx.get().request
    except LookupError:
        return UNKNOWN_PRINCIPAL
    headers = getattr(request, "headers", None)
    values = headers.getlist(PRINCIPAL_HEADER) if headers is not None else []
    if not values or not values[0]:
        return UNKNOWN_PRINCIPAL
    if len(values) > 1:
        return AMBIGUOUS_PRINCIPAL
    return values[0] if _PRINCIPAL.fullmatch(values[0]) else OTHER_PRINCIPAL


def build_server(actions: MailboxActions, *, host: str = "0.0.0.0", port: int = 9000) -> FastMCP:
    mcp = FastMCP(
        "mail-actions",
        host=host,
        port=port,
        # Every tool is a self-contained call, so no session state is needed. A
        # stateful server expires idle sessions, and a long-lived client then
        # gets 404 on its next call.
        stateless_http=True,
        instructions=(
            "mail-actions files and labels the user's mail by message id "
            "(corpus.id/v1, e.g. 'gmail:personal::18c2f0a1b2c3d4e5'). It never returns "
            "message content. file = tag 'linear' and archive; unfile reverses it; "
            "label = add an allow-listed Auto/<group> label; sync_filters = converge "
            "approved server-side rules; capabilities = what each mailbox supports and "
            "whether archiving is enabled."
        ),
    )

    @mcp.tool(annotations=_WRITE)
    def file(ref: str) -> dict[str, Any]:
        """Tag the message 'linear' and take it out of the inbox. Idempotent.

        Refused for protected (family, tax, legal) mail. While no protected list is
        configured, or its senders' existing mail is not yet tagged, the message
        is tagged but stays in the inbox (action='label_only'). Returns {ok, ref,
        action, provider, new_ref, reason}.
        """
        return asdict(actions.file(ref, principal=principal()))

    @mcp.tool(annotations=_WRITE)
    def unfile(ref: str) -> dict[str, Any]:
        """Undo file: return the message to the inbox and remove 'linear'. Idempotent."""
        return asdict(actions.unfile(ref, principal=principal()))

    @mcp.tool(annotations=_WRITE)
    def label(ref: str, group: str) -> dict[str, Any]:
        """Add the label Auto/<group>, where group is one of the configured groups.

        Never archives. Returns {ok, ref, action, provider, new_ref, reason}.
        """
        return asdict(actions.label(ref, group, principal=principal()))

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        )
    )
    def sync_filters(mailbox: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        """Converge this service's server-side filters in `mailbox` to `rules`.

        `rules` is the complete approved set; a managed filter not listed is
        deleted, and filters made by hand are never touched. Each rule is
        {"match": "from_domain"|"from_addr"|"list_id", "value": str,
        "tag": "Auto/<group>", "archive": bool (optional),
        "category": "bulk"|"newsletter"|"promotional" (required to archive)}.
        Protected-sender filters are managed from the protected list only.
        Returns {ok, mailbox, created, updated, deleted, refused: [{index, reason}],
        reason}.
        """
        return asdict(actions.sync_filters(mailbox, rules, principal=principal()))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def capabilities() -> dict[str, Any]:
        """Whether archiving is enabled (it is off until a protected list is
        configured and its senders' existing mail is tagged) and what each
        mailbox's provider supports."""
        return asdict(actions.capabilities(principal=principal()))

    return mcp
