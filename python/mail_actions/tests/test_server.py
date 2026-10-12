import asyncio
import json
from unittest.mock import create_autospec

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.requests import Request

from mail_actions.actions import ActionResult, MailboxActions, Status, SyncResult
from mail_actions.server import build_server, principal

REF = "gmail:personal::18c2f0a1b2c3d4e5"


@pytest.fixture
def actions():
    m = create_autospec(MailboxActions, instance=True)
    m.file.return_value = ActionResult(ok=True, ref=REF, action="file", provider="gmail")
    return m


@pytest.fixture
def headers():
    return []


@pytest.fixture
def request_headers(headers):
    """Run the test inside an MCP request carrying *headers*."""
    scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers]}
    context = RequestContext(
        request_id=1, meta=None, session=None, lifespan_context=None, request=Request(scope)
    )
    token = request_ctx.set(context)
    yield
    request_ctx.reset(token)


def call(server, tool, args):
    """The structured result of one tool call."""
    result = asyncio.run(server.call_tool(tool, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_exactly_five_tools(actions):
    tools = asyncio.run(build_server(actions).list_tools())

    assert sorted(t.name for t in tools) == [
        "capabilities",
        "file",
        "label",
        "sync_filters",
        "unfile",
    ]
    assert {t.name for t in tools if t.annotations.readOnlyHint} == {"capabilities"}


def test_server_is_stateless_streamable_http_on_9000(actions):
    server = build_server(actions)

    assert server.settings.stateless_http is True
    assert (server.settings.port, server.settings.streamable_http_path) == (9000, "/mcp")


def test_file_forwards_the_ref_and_returns_the_result(actions):
    result = call(build_server(actions), "file", {"ref": REF})

    assert result == {
        "ok": True,
        "ref": REF,
        "action": "file",
        "provider": "gmail",
        "new_ref": None,
        "reason": None,
    }
    actions.file.assert_called_once_with(REF, principal="unknown")


def test_each_tool_reaches_its_action(actions):
    actions.unfile.return_value = ActionResult(ok=True, ref=REF, action="unfile")
    actions.label.return_value = ActionResult(ok=True, ref=REF, action="label")
    actions.sync_filters.return_value = SyncResult(ok=True, mailbox="personal", created=1)
    actions.capabilities.return_value = Status(
        archive_enabled=False, protected_list="empty", mailboxes={}
    )
    server = build_server(actions)
    rules = [{"match": "from_domain", "value": "bank.example", "tag": "Auto/Banking"}]

    assert call(server, "unfile", {"ref": REF})["action"] == "unfile"
    assert call(server, "label", {"ref": REF, "group": "Banking"})["action"] == "label"
    assert call(server, "sync_filters", {"mailbox": "personal", "rules": rules})["created"] == 1
    assert call(server, "capabilities", {})["archive_enabled"] is False
    actions.label.assert_called_once_with(REF, "Banking", principal="unknown")
    actions.sync_filters.assert_called_once_with("personal", rules, principal="unknown")


def test_principal_outside_a_request_is_unknown():
    assert principal() == "unknown"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([("x-client-id", "orchestrator")], "orchestrator"),
        ([], "unknown"),
        ([("x-client-id", "")], "unknown"),
        ([("x-client-id", "a"), ("x-client-id", "b")], "ambiguous"),
        ([("x-client-id", "Robert'); DROP")], "other"),
    ],
)
def test_principal_from_the_request_header(request_headers, expected):
    assert principal() == expected
