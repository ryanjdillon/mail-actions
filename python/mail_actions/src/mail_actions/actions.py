"""MailboxActions: the one place every mailbox rule is enforced.

Callers (an LLM agent acting on untrusted mail) may pass anything. Before a
provider is touched, a call must name a well-formed ref in a configured mailbox,
stay within the caps, and pass the protected check; filter rules must have an
approved shape. Each call writes exactly one audit line, which carries a hashed
ref and never the ref itself, and records its metrics from the same event.

Results describe what was done to which ref. They never carry message content:
no provider method returns any.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import ADDRESS, DOMAIN, Config, ConfigError, Protected, ProtectedFile
from .ids import InvalidRef, MessageRef, parse_ref
from .provider import (
    Capabilities,
    Match,
    Provider,
    ProviderError,
    Rule,
    ServerRule,
    ServerRules,
)
from .telemetry import Metrics

log = logging.getLogger(__name__)
audit_log = logging.getLogger("mail_actions.audit")
# Always on: the audit must not vanish if the root level is later raised.
audit_log.setLevel(logging.INFO)

#: Categories a rule must carry for it to take mail out of the inbox.
ARCHIVABLE_CATEGORIES = frozenset({"bulk", "newsletter", "promotional"})
_RULE_KEYS = frozenset({"match", "value", "tag", "archive", "category"})
_LIST_ID = re.compile(r"<?[a-z0-9][a-z0-9._-]{0,200}[a-z0-9]>?")
_SOURCE = re.compile(r"[a-z0-9_-]{1,32}:[a-z0-9_-]{1,32}")
_DAY = timedelta(days=1)
_WEEK = timedelta(days=7)


def utcnow() -> datetime:
    return datetime.now(UTC)


def emit_audit(event: Mapping[str, Any]) -> None:
    """Write one audit event as a JSON line on the ``mail_actions.audit`` logger."""
    audit_log.info(json.dumps(event, sort_keys=True, default=str))


def hash_ref(value: str) -> dict[str, Any]:
    """A ref in a form safe to log: its ``kind:mailbox`` and a short digest.

    The uid is never logged; an imap uid embeds a folder name. The source is kept
    only when it is shaped like one, so an arbitrary string is not echoed.
    """
    source = value.partition("::")[0] if isinstance(value, str) else ""
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:12]
    return {"source": source if _SOURCE.fullmatch(source) else None, "sha256": digest}


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    ref: str
    action: str
    provider: str | None = None
    new_ref: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    ok: bool
    mailbox: str
    created: int = 0
    updated: int = 0
    deleted: int = 0
    #: One entry per refused input rule: its index and why.
    refused: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Status:
    """What callers can rely on right now."""

    #: False while the protected list is empty or invalid, or while any mailbox's
    #: protected backfill for the current list is pending: ``file`` then tags only.
    archive_enabled: bool
    protected_list: str
    mailboxes: dict[str, Capabilities]


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Window:
    """A rolling-window counter: at most ``limit`` events per ``span``."""

    def __init__(self, limit: int, span: timedelta) -> None:
        self.limit, self.span = limit, span
        self._events: deque[datetime] = deque()

    def take(self, now: datetime, n: int = 1) -> None:
        while self._events and now - self._events[0] >= self.span:
            self._events.popleft()
        if len(self._events) + n > self.limit:
            raise _Refused("cap_exceeded")
        self._events.extend([now] * n)

    def used(self, now: datetime) -> int:
        """Events counted in the window ending at *now*."""
        return sum(1 for t in self._events if now - t < self.span)


class MailboxActions:
    """File, unfile and label messages, and converge server-side filter rules.

    Collaborators are injected: *providers* by mailbox name, the *protected* list
    source, the *clock*, the *audit* sink and the *metrics* recorder.
    """

    def __init__(
        self,
        config: Config,
        providers: Mapping[str, Provider],
        *,
        protected: ProtectedFile,
        clock: Callable[[], datetime] = utcnow,
        audit: Callable[[Mapping[str, Any]], None] = emit_audit,
        metrics: Metrics | None = None,
    ) -> None:
        self._config = config
        self._providers = dict(providers)
        self._protected = protected
        self._clock = clock
        self._audit = audit
        self._lock = threading.Lock()
        self._actions = _Window(config.caps.actions_per_day, _DAY)
        self._filter_changes = _Window(config.caps.filter_changes_per_week, _WEEK)
        # The protected list each mailbox's filters were last converged to.
        self._converged: dict[str, Protected] = {}
        # The protected list whose senders' existing mail each mailbox has had
        # tagged. ``file`` archives only while this matches the current list.
        self._backfilled: dict[str, Protected] = {}
        self._metrics = metrics or Metrics()
        self._metrics.watch_caps(self._cap_usage)

    # --- message actions ---------------------------------------------------

    def file(self, ref: str, *, principal: str = "unknown") -> ActionResult:
        """Tag the message with the filing label and archive it. Idempotent.

        Refused if the message carries a protected tag. While the protected list
        is empty or unreadable, the message is tagged but not archived
        (``action="label_only"``), since nothing could then be recognised as
        protected. The same holds until the protected senders' existing mail
        has been tagged for the current list (``protected_backfill_pending``);
        each call retries that pass first.
        """

        def run(msg: MessageRef, provider: Provider) -> ActionResult:
            try:
                protected = self._protected.current()
            except ConfigError:
                protected = None
            reason: str | None = None
            if protected is None:
                reason = "protected_list_invalid"
            elif protected.empty:
                reason = "protected_list_empty"
            else:
                try:
                    # Before the tags are read, so this message is covered too.
                    self._protect_existing(msg.mailbox, provider, protected)
                except ProviderError:
                    reason = "protected_backfill_pending"
            tags = provider.get_tags(msg)
            if (
                protected is not None
                and not protected.empty
                and any(_is_protected(t, protected) for t in tags)
            ):
                raise _Refused("protected")
            if self._config.label not in tags:
                provider.add_tag(msg, self._config.label)
            if reason is not None:
                return self._ok(ref, "label_only", provider, reason=reason)
            new = provider.archive(msg)
            return self._ok(ref, "file", provider, new_ref=new)

        return self._message_call("file", ref, principal, run)

    def unfile(self, ref: str, *, principal: str = "unknown") -> ActionResult:
        """Return the message to the inbox and remove the filing label. Idempotent."""

        def run(msg: MessageRef, provider: Provider) -> ActionResult:
            new = provider.unarchive(msg)
            provider.remove_tag(new, self._config.label)
            return self._ok(ref, "unfile", provider, new_ref=new)

        return self._message_call("unfile", ref, principal, run)

    def label(self, ref: str, group: str, *, principal: str = "unknown") -> ActionResult:
        """Tag the message ``Auto/<group>``. Never archives, so protected mail may
        be labelled."""
        tag = f"Auto/{group}" if isinstance(group, str) else ""

        def allowed() -> None:
            if tag not in self._config.group_tags:
                raise _Refused("group_not_allowed")

        def run(msg: MessageRef, provider: Provider) -> ActionResult:
            provider.add_tag(msg, tag)
            return self._ok(ref, "label", provider)

        return self._message_call("label", ref, principal, run, precheck=allowed, group=group)

    # --- server-side rules -------------------------------------------------

    def sync_filters(
        self, mailbox: str, rules: Sequence[Any], *, principal: str = "unknown"
    ) -> SyncResult:
        """Converge this service's server-side rules in *mailbox* to *rules* plus
        the protected rules.

        *rules* is the complete approved set: a managed rule not in it is
        deleted. Rules the user made by hand are never touched. An input rule
        that is refused is reported and leaves any existing rule with the same
        sender criterion in place. Changes to protected rules come only from the
        protected list and do not count against the weekly cap. While the list
        is empty or missing, existing protected rules are left exactly as they
        are.
        """
        start = time.perf_counter()
        try:
            result = self._sync(mailbox, rules)
        except _Refused as exc:
            result = SyncResult(ok=False, mailbox=str(mailbox)[:32], reason=exc.reason)
        except ProviderError as exc:
            result = SyncResult(ok=False, mailbox=mailbox, reason=f"provider:{exc.reason}")
        self._record(
            start,
            {
                "event": "mail_actions.tool_call",
                "tool": "sync_filters",
                "principal": principal,
                "mailbox": result.mailbox if result.mailbox in self._providers else None,
                "rules": len(rules) if isinstance(rules, Sequence) else None,
                "created": result.created,
                "updated": result.updated,
                "deleted": result.deleted,
                "refused": len(result.refused),
                "outcome": "ok" if result.ok else f"refused:{result.reason}",
            },
        )
        return result

    def ensure_protected(self, *, principal: str = "startup") -> None:
        """Converge every mailbox's protected rules to the current protected list
        and tag the protected senders' existing mail.

        Run at startup; a list that arrives or changes later is handled by the
        next ``file``. Approved rules are left as they are. A mailbox whose pass
        fails stays pending, and ``file`` there only labels until it completes.
        """
        try:
            protected = self._protected.current()
        except ConfigError:
            return
        for name, provider in self._providers.items():
            start = time.perf_counter()
            outcome = "ok"
            try:
                self._protect_existing(name, provider, protected)
            except ProviderError as exc:
                outcome = f"refused:provider:{exc.reason}"
            self._record(
                start,
                {
                    "event": "mail_actions.tool_call",
                    "tool": "ensure_protected",
                    "principal": principal,
                    "mailbox": name,
                    "outcome": outcome,
                },
            )

    def capabilities(self, *, principal: str = "unknown") -> Status:
        """What each mailbox supports, and whether ``file`` archives right now."""
        start = time.perf_counter()
        try:
            protected = self._protected.current()
            state = "empty" if protected.empty else "ok"
        except ConfigError:
            state = "invalid"
        backfilled = state == "ok" and all(
            self._backfilled.get(name) == protected for name in self._providers
        )
        status = Status(
            archive_enabled=backfilled,
            protected_list=state,
            mailboxes={name: p.capabilities() for name, p in self._providers.items()},
        )
        self._record(
            start,
            {
                "event": "mail_actions.tool_call",
                "tool": "capabilities",
                "principal": principal,
                "protected_list": state,
                "outcome": "ok",
            },
        )
        return status

    # --- internals ---------------------------------------------------------

    def _record(self, start: float, event: dict[str, Any]) -> None:
        """Write a call's audit line and its metrics, from the one event."""
        duration_ms = (time.perf_counter() - start) * 1000
        event["duration_ms"] = round(duration_ms, 1)
        self._audit(event)
        self._metrics.tool_call(
            tool=event["tool"],
            outcome=event["outcome"],
            principal=event["principal"],
            duration_ms=duration_ms,
        )

    def _cap_usage(self) -> dict[str, int]:
        now = self._clock()
        with self._lock:
            return {
                "actions": self._actions.used(now),
                "filter_changes": self._filter_changes.used(now),
            }

    def _ok(
        self,
        ref: str,
        action: str,
        provider: Provider,
        *,
        new_ref: MessageRef | None = None,
        reason: str | None = None,
    ) -> ActionResult:
        changed = new_ref is not None and str(new_ref) != ref
        return ActionResult(
            ok=True,
            ref=ref,
            action=action,
            provider=provider.kind,
            new_ref=str(new_ref) if changed else None,
            reason=reason,
        )

    def _message_call(
        self,
        tool: str,
        ref: str,
        principal: str,
        run: Callable[[MessageRef, Provider], ActionResult],
        *,
        precheck: Callable[[], None] | None = None,
        **extra: Any,
    ) -> ActionResult:
        start = time.perf_counter()
        provider: Provider | None = None
        try:
            try:
                msg = parse_ref(ref)
            except InvalidRef as exc:
                raise _Refused("invalid_ref") from exc
            provider = self._providers.get(msg.mailbox)
            if provider is None:
                raise _Refused("unknown_mailbox")
            if provider.kind != msg.kind:
                raise _Refused("kind_mismatch")
            # Checks that need no provider run before the cap, so a refused
            # argument does not spend the day's budget.
            if precheck is not None:
                precheck()
            with self._lock:
                self._actions.take(self._clock())
            result = run(msg, provider)
        except _Refused as exc:
            result = self._refused(ref, tool, provider, exc.reason)
        except ProviderError as exc:
            result = self._refused(ref, tool, provider, f"provider:{exc.reason}")
        except Exception:
            # Fail closed with a fixed reason: exception text can carry provider
            # or message detail and must not reach the MCP client.
            log.exception("unexpected error in %s", tool)
            result = self._refused(ref, tool, provider, "internal")
        event: dict[str, Any] = {
            "event": "mail_actions.tool_call",
            "tool": tool,
            "principal": principal,
            "ref": hash_ref(ref),
            "outcome": result.action if result.ok else f"refused:{result.reason}",
        }
        if "group" in extra:
            group = extra["group"]
            event["group"] = group if group in self._config.groups else None
        self._record(start, event)
        return result

    @staticmethod
    def _refused(ref: Any, tool: str, provider: Provider | None, reason: str) -> ActionResult:
        return ActionResult(
            ok=False,
            ref=ref if isinstance(ref, str) else "",
            action=tool,
            provider=provider.kind if provider else None,
            reason=reason,
        )

    def _provider_with_rules(self, mailbox: Any) -> Provider:
        provider = self._providers.get(mailbox) if isinstance(mailbox, str) else None
        if provider is None:
            raise _Refused("unknown_mailbox")
        if provider.capabilities().server_rules == ServerRules.none:
            raise _Refused("server_rules_unsupported")
        return provider

    def _sync(self, mailbox: str, rules: Sequence[Any]) -> SyncResult:
        provider = self._provider_with_rules(mailbox)
        if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
            raise _Refused("rules_not_a_list")
        try:
            protected = self._protected.current()
        except ConfigError as exc:
            raise _Refused("protected_list_invalid") from exc

        current = [r for r in provider.get_rules() if r.managed]
        if protected.empty:
            # An empty or missing list means "not known yet", not "protect no
            # one": existing protected rules are frozen, neither deleted nor
            # changed, and no input rule may take over their senders.
            frozen = [r for r in current if r.rule is not None and r.rule.tag in protected.tags]
            current = [r for r in current if r not in frozen]
            protected_rules: list[Rule] = []
            protected_keys = {r.rule.key for r in frozen if r.rule is not None}
        else:
            protected_rules = _protected_rules(protected)
            protected_keys = {r.key for r in protected_rules}
        approved: dict[tuple[Match, str], Rule] = {}
        refused: list[dict[str, Any]] = []
        held: set[tuple[Match, str]] = set()
        for index, raw in enumerate(rules):
            try:
                rule = self._validate_rule(raw, protected)
                if rule.key in protected_keys:
                    raise _Refused("protected_rule")
            except _Refused as exc:
                refused.append({"index": index, "reason": exc.reason})
                key = _raw_key(raw)
                if key is not None:
                    held.add(key)
                continue
            if rule.key in approved:
                refused.append({"index": index, "reason": "duplicate"})
                continue
            approved[rule.key] = rule

        plan = _plan(current, approved, protected_rules, protected.tags, held)
        with self._lock:
            if plan.approved_changes:
                self._filter_changes.take(self._clock(), plan.approved_changes)
        _apply(provider, plan)
        if not protected.empty:
            self._converged[mailbox] = protected
        return SyncResult(
            ok=True,
            mailbox=mailbox,
            created=plan.created,
            updated=plan.updated,
            deleted=plan.deleted,
            refused=refused,
        )

    def _protect_existing(self, mailbox: str, provider: Provider, protected: Protected) -> None:
        """Install the protected rules, then tag mail already in the mailbox from
        each protected sender, since rules apply only to new mail. Recorded as
        done for *protected* only once every entry succeeded; any error leaves
        the mailbox pending, so ``file`` fails closed."""
        if protected.empty or self._backfilled.get(mailbox) == protected:
            return
        self._converge_protected(mailbox, provider, protected)
        for rule in _protected_rules(protected):
            provider.tag_matching(rule.match, rule.value, rule.tag)
        self._backfilled[mailbox] = protected

    def _converge_protected(self, mailbox: str, provider: Provider, protected: Protected) -> None:
        """Bring the protected rules in line with *protected*, leaving approved
        rules alone. Skipped when already converged to this list, and while the
        list is empty: an empty list is unknown, so existing protected rules
        stay as they are."""
        if protected.empty or self._converged.get(mailbox) == protected:
            return
        if provider.capabilities().server_rules == ServerRules.none:
            return
        current = [r for r in provider.get_rules() if r.managed]
        tags = protected.tags
        keep = {
            r.rule.key: r.rule for r in current if r.rule is not None and r.rule.tag not in tags
        }
        plan = _plan(current, keep, _protected_rules(protected), tags, held=set())
        _apply(provider, plan)
        self._converged[mailbox] = protected

    def _validate_rule(self, raw: Any, protected: Protected) -> Rule:
        if not isinstance(raw, Mapping) or set(raw) - _RULE_KEYS:
            raise _Refused("invalid_shape")
        try:
            match = Match(raw.get("match"))
        except ValueError as exc:
            raise _Refused("invalid_match") from exc
        value = raw.get("value")
        if not isinstance(value, str):
            raise _Refused("invalid_value")
        value = value.strip().lower()
        pattern = {Match.from_domain: DOMAIN, Match.from_addr: ADDRESS, Match.list_id: _LIST_ID}[
            match
        ]
        if not pattern.fullmatch(value):
            raise _Refused("invalid_value")
        value = value.strip("<>")
        tag = raw.get("tag")
        if tag not in self._config.group_tags:
            raise _Refused("tag_not_allowed")
        archive = raw.get("archive", False)
        if not isinstance(archive, bool):
            raise _Refused("invalid_archive")
        category = raw.get("category")
        if archive:
            if protected.empty:
                raise _Refused("protected_list_empty")
            if category not in ARCHIVABLE_CATEGORIES:
                raise _Refused("archive_needs_bulk_category")
            if protected.covers(value):
                raise _Refused("protected_sender")
        return Rule(match, value, tag, archive)


def _is_protected(tag: str, protected: Protected) -> bool:
    return any(tag == p or tag.startswith(p + "/") for p in protected.tags)


def _raw_key(raw: Any) -> tuple[Match, str] | None:
    """The sender criterion of a refused input rule, if it has a readable one."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("value"), str):
        return None
    try:
        return (Match(raw.get("match")), raw["value"].strip().lower().strip("<>"))
    except ValueError:
        return None


def _protected_rules(protected: Protected) -> list[Rule]:
    rules: dict[tuple[Match, str], Rule] = {}
    for entry, tag in protected.entries():
        match = Match.from_addr if "@" in entry else Match.from_domain
        rule = Rule(match, entry, tag)
        # An entry listed under two categories keeps the first; one rule per sender.
        rules.setdefault(rule.key, rule)
    return list(rules.values())


@dataclass
class _Plan:
    create: list[Rule] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    deleted: int = 0
    approved_changes: int = 0


def _plan(
    current: list[ServerRule],
    approved: Mapping[tuple[Match, str], Rule],
    protected_rules: list[Rule],
    protected_tags: frozenset[str],
    held: set[tuple[Match, str]],
) -> _Plan:
    """Diff the managed rules on the server against the desired set.

    A rule is identified by its sender criterion. Changes that touch a protected
    rule (on either side) are exempt from the cap; everything else counts.
    """
    desired = {r.key: r for r in protected_rules}
    desired.update({k: r for k, r in approved.items() if k not in desired})
    # A refused input rule keeps its existing server rule, unless that sender is
    # now covered by a desired rule (a protected one), which wins.
    held = held - desired.keys()
    plan = _Plan()
    seen: set[tuple[Match, str]] = set()
    for server in current:
        rule = server.rule
        if rule is None:
            plan.delete.append(server.id)
            plan.deleted += 1
            plan.approved_changes += 1
            continue
        is_protected = rule.tag in protected_tags
        if rule.key in held and not is_protected:
            seen.add(rule.key)
            continue
        want = desired.get(rule.key)
        if want is None or rule.key in seen:
            plan.delete.append(server.id)
            plan.deleted += 1
            plan.approved_changes += 0 if is_protected else 1
            continue
        seen.add(rule.key)
        if want != rule:
            plan.create.append(want)
            plan.delete.append(server.id)
            plan.updated += 1
            exempt = is_protected or want.tag in protected_tags
            plan.approved_changes += 0 if exempt else 1
    for key, want in desired.items():
        if key not in seen:
            plan.create.append(want)
            plan.created += 1
            plan.approved_changes += 0 if want.tag in protected_tags else 1
    return plan


def _apply(provider: Provider, plan: _Plan) -> None:
    # Create before delete, so an updated rule never has a gap with no filter.
    for rule in plan.create:
        provider.create_rule(rule)
    for rule_id in plan.delete:
        provider.delete_rule(rule_id)
