"""Gmail adapter: tags are labels, archive removes ``INBOX``, rules are filters.

Requests are confined to these endpoints of the Gmail API:

* ``GET/POST labels`` — resolve tag names to label ids at startup, creating any
  that are missing;
* ``GET messages/{id}?format=minimal&fields=labelIds`` — the protected check. The
  ``fields`` mask keeps even the snippet out of the response;
* ``POST messages/{id}/modify`` — add or remove labels;
* ``GET messages?q=from:(<entry>)&fields=messages/id,nextPageToken`` and
  ``POST messages/batchModify`` — the protected backfill: find a protected
  sender's existing mail and add its protected label. The ``fields`` mask keeps
  the listing to message ids;
* ``GET/POST/DELETE settings/filters`` — server-side rules.

Nothing here sends, trashes or deletes mail, the only search is the protected
backfill's by sender, and no method returns message content. The token needs only ``gmail.modify`` and
``gmail.settings.basic``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass

import httpx

from .config import ADDRESS, DOMAIN, ConfigError
from .ids import MessageRef
from .provider import (
    ArchiveModel,
    Capabilities,
    Match,
    ProtectedCheck,
    Provider,
    ProviderError,
    Rule,
    ServerRule,
    ServerRules,
    TagModel,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
)
INBOX = "INBOX"
#: Marks a filter as created by this service, as its ``criteria.negatedQuery``.
#: A plain token rather than ``label:``/``in:``/``is:``, which Gmail documents as
#: unreliable in filters. It excludes only mail containing the word, which no
#: real mail does by accident. Only the matched sender can add it, so on an
#: archive rule it can only keep that sender's mail in the inbox; on a protected
#: rule only the protected sender (or a forger of that address) could skip the
#: label. A marked filter counts as managed only if its shape is also one this
#: service writes (see ``_read_filter``).
MARKER = "mailactionsmanaged"
_LIST = re.compile(r"list:([a-z0-9][a-z0-9._-]{0,200}[a-z0-9])")
_REASON = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: Gmail's page size cap for ``messages.list`` and id cap for ``batchModify``.
_PAGE = 500
_BATCH = 1000
# Refresh a little before expiry so a token never lapses mid-request.
_EXPIRY_MARGIN = 60.0


@dataclass(frozen=True, slots=True)
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls, prefix: str, env: Mapping[str, str]) -> Credentials:
        values = {
            k: env.get(f"{prefix}_{k.upper()}", "")
            for k in ("client_id", "client_secret", "refresh_token")
        }
        missing = sorted(f"{prefix}_{k.upper()}" for k, v in values.items() if not v)
        if missing:
            raise ConfigError(f"missing credentials: {', '.join(missing)}")
        return cls(**values)


def _error_reason(resp: httpx.Response) -> str:
    """Google's error reason for a failed response, or its status code.

    The reason reaches the MCP client, so anything not shaped like a short code
    falls back to the status code rather than echo the response body.
    """
    try:
        error = resp.json().get("error", {})
        if isinstance(error, str):  # the OAuth endpoint: {"error": "invalid_grant"}
            reason = error
        else:
            errors = error.get("errors") or []
            reason = (errors[0].get("reason") if errors else None) or error.get("status")
    except (ValueError, AttributeError):
        reason = None
    if isinstance(reason, str) and _REASON.fullmatch(reason):
        return reason
    return str(resp.status_code)


class GoogleAuth(httpx.Auth):
    """Bearer auth from a refresh token, minted on first use and on expiry.

    A 401 forces one refresh and a single retry, covering a token revoked or
    rotated before its stated expiry.
    """

    requires_response_body = True

    def __init__(self, credentials: Credentials, *, clock: Callable[[], float] = time.monotonic):
        self._credentials = credentials
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0
        self.scopes: frozenset[str] = frozenset()

    def refresh(self, send: Callable[[httpx.Request], httpx.Response]) -> frozenset[str]:
        """Mint a new access token through *send* and return its granted scopes."""
        try:
            self._accept(send(self._token_request()))
        except httpx.HTTPError as exc:
            raise ProviderError("transport") from exc
        return self.scopes

    def _token_request(self) -> httpx.Request:
        return httpx.Request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "refresh_token": self._credentials.refresh_token,
                "grant_type": "refresh_token",
            },
        )

    def _accept(self, resp: httpx.Response) -> None:
        if not resp.is_success:
            raise ProviderError(f"token:{_error_reason(resp)}")
        body = resp.json()
        self._token = body["access_token"]
        self._expires = self._clock() + float(body.get("expires_in", 3600)) - _EXPIRY_MARGIN
        self.scopes = frozenset(str(body.get("scope", "")).split())

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self._token is None or self._clock() >= self._expires:
            self._accept((yield self._token_request()))
        request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401:
            self._accept((yield self._token_request()))
            request.headers["Authorization"] = f"Bearer {self._token}"
            yield request


def gmail_client(
    credentials: Credentials, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """An ``httpx.Client`` rooted at the Gmail API for the signed-in user."""
    return httpx.Client(
        base_url=API, auth=GoogleAuth(credentials), transport=transport, timeout=30.0
    )


def verify_credentials(client: httpx.Client) -> frozenset[str]:
    """Mint an access token and return the scopes it was granted.

    Touches only the OAuth endpoint, never mail. Raises :class:`ProviderError`
    if the refresh token is refused.
    """
    auth = client.auth
    if not isinstance(auth, GoogleAuth):
        raise TypeError("client was not built by gmail_client")
    return auth.refresh(lambda request: client.send(request, auth=None))


class GmailAdapter(Provider):
    """One Gmail mailbox. Call :meth:`prepare` once before use."""

    kind = "gmail"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._ids: dict[str, str] = {}
        self._names: dict[str, str] = {}
        # Ids of the labels this service resolved in ``prepare``: the only ones a
        # managed filter may add.
        self._resolved: frozenset[str] = frozenset()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            tag_model=TagModel.labels,
            archive=ArchiveModel.remove_inbox_tag,
            server_rules=ServerRules.gmail_filters,
            protected_check=ProtectedCheck.tag,
        )

    def prepare(self, tags: Iterable[str]) -> None:
        """Resolve every tag name this service may apply to a label id, creating
        missing labels. Later calls never create labels."""
        labels = self._call("GET", "/labels").get("labels", [])
        self._names = {x["id"]: x["name"] for x in labels}
        self._ids = {name: id_ for id_, name in self._names.items()}
        for tag in sorted(set(tags) - self._ids.keys()):
            created = self._call(
                "POST",
                "/labels",
                json={
                    "name": tag,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            self._ids[tag], self._names[created["id"]] = created["id"], tag
        self._resolved = frozenset(self._ids[t] for t in tags)

    def get_tags(self, ref: MessageRef) -> frozenset[str]:
        body = self._call(
            "GET", f"/messages/{ref.uid}", params={"format": "minimal", "fields": "labelIds"}
        )
        # System labels (INBOX, UNREAD, ...) are their own ids.
        return frozenset(self._names.get(i, i) for i in body.get("labelIds", []))

    def add_tag(self, ref: MessageRef, tag: str) -> None:
        self._modify(ref, add=[self._label_id(tag)])

    def remove_tag(self, ref: MessageRef, tag: str) -> None:
        self._modify(ref, remove=[self._label_id(tag)])

    def archive(self, ref: MessageRef) -> MessageRef:
        self._modify(ref, remove=[INBOX])
        return ref

    def unarchive(self, ref: MessageRef) -> MessageRef:
        self._modify(ref, add=[INBOX])
        return ref

    def tag_matching(self, match: Match, value: str, tag: str) -> int:
        label = self._label_id(tag)
        query = f"list:({value})" if match == Match.list_id else f"from:({value})"
        ids: list[str] = []
        page: str | None = None
        while True:
            params: dict[str, str | int] = {"q": query, "maxResults": _PAGE}
            if page:
                params["pageToken"] = page
            body = self._call("GET", "/messages", params=params, fields="messages/id,nextPageToken")
            ids.extend(m["id"] for m in body.get("messages", []))
            page = body.get("nextPageToken")
            if not page:
                break
        for start in range(0, len(ids), _BATCH):
            self._call(
                "POST",
                "/messages/batchModify",
                json={"ids": ids[start : start + _BATCH], "addLabelIds": [label]},
            )
        return len(ids)

    def get_rules(self) -> list[ServerRule]:
        filters = self._call("GET", "/settings/filters").get("filter", [])
        return [self._read_filter(f) for f in filters]

    def create_rule(self, rule: Rule) -> str:
        criteria: dict[str, str] = {"negatedQuery": MARKER}
        if rule.match == Match.list_id:
            criteria["query"] = f"list:{rule.value}"
        else:
            criteria["from"] = rule.value if rule.match == Match.from_addr else f"@{rule.value}"
        action: dict[str, list[str]] = {"addLabelIds": [self._label_id(rule.tag)]}
        if rule.archive:
            action["removeLabelIds"] = [INBOX]
        return self._call(
            "POST", "/settings/filters", json={"criteria": criteria, "action": action}
        )["id"]

    def delete_rule(self, rule_id: str) -> None:
        self._call("DELETE", f"/settings/filters/{rule_id}")

    # --- internals ---------------------------------------------------------

    def _label_id(self, tag: str) -> str:
        try:
            return self._ids[tag]
        except KeyError:
            raise ProviderError("unknown_tag") from None

    def _modify(
        self, ref: MessageRef, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> None:
        body: dict[str, list[str]] = {}
        if add:
            body["addLabelIds"] = list(add)
        if remove:
            body["removeLabelIds"] = list(remove)
        # The response is the modified message; the mask keeps it to the id.
        self._call("POST", f"/messages/{ref.uid}/modify", json=body, fields="id")

    def _read_filter(self, raw: Mapping) -> ServerRule:
        """Read a filter back. It is managed only if it carries the marker and has
        exactly a shape :meth:`create_rule` writes; anything else is unmanaged
        and so never changed or deleted."""
        unmanaged = ServerRule(id=raw["id"], managed=False, rule=None)
        criteria, action = raw.get("criteria") or {}, raw.get("action") or {}
        if not isinstance(criteria, Mapping) or not isinstance(action, Mapping):
            return unmanaged
        if criteria.get("negatedQuery") != MARKER:
            return unmanaged
        added = action.get("addLabelIds")
        removed = action.get("removeLabelIds", [INBOX])
        if (
            set(action) - {"addLabelIds", "removeLabelIds"}
            or not isinstance(added, list)
            or len(added) != 1
            or added[0] not in self._resolved
            or removed != [INBOX]
        ):
            return unmanaged
        tag, archive = self._names[added[0]], "removeLabelIds" in action
        keys = set(criteria) - {"negatedQuery"}
        sender, query = str(criteria.get("from", "")), str(criteria.get("query", ""))
        if keys == {"from"} and sender.startswith("@") and DOMAIN.fullmatch(sender[1:]):
            rule = Rule(Match.from_domain, sender[1:], tag, archive)
        elif keys == {"from"} and ADDRESS.fullmatch(sender):
            rule = Rule(Match.from_addr, sender, tag, archive)
        elif keys == {"query"} and (listed := _LIST.fullmatch(query)):
            rule = Rule(Match.list_id, listed.group(1), tag, archive)
        else:
            return unmanaged
        return ServerRule(id=raw["id"], managed=True, rule=rule)

    def _call(self, method: str, path: str, *, fields: str | None = None, **kwargs) -> dict:
        if fields is not None:
            kwargs.setdefault("params", {})["fields"] = fields
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError("transport") from exc
        if not resp.is_success:
            raise ProviderError(_error_reason(resp))
        return resp.json() if resp.content else {}
