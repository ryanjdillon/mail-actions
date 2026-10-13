import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, call, create_autospec

import pytest

from mail_actions.actions import MailboxActions, emit_audit, hash_ref, utcnow
from mail_actions.config import Caps, Config, ConfigError, Mailbox, Protected, ProtectedFile
from mail_actions.ids import parse_ref
from mail_actions.provider import (
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
from mail_actions.telemetry import Metrics

REF = "gmail:personal::18c2f0a1b2c3d4e5"
MSG = parse_ref(REF)
T0 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
GMAIL = Capabilities(
    tag_model=TagModel.labels,
    archive=ArchiveModel.remove_inbox_tag,
    server_rules=ServerRules.gmail_filters,
    protected_check=ProtectedCheck.tag,
)
LISTED = Protected(family=frozenset({"mom@example.org"}), tax=frozenset({"tax.example.net"}))
MOM = Rule(Match.from_addr, "mom@example.org", "Family")
TAX = Rule(Match.from_domain, "tax.example.net", "Tax")


def managed(rule_id, rule):
    return ServerRule(id=rule_id, managed=True, rule=rule)


@pytest.fixture
def config():
    return Config(
        mailboxes=(Mailbox("personal", "gmail", "MAIL_ACTIONS_GMAIL_PERSONAL"),),
        groups=frozenset({"Banking", "Insurance"}),
        caps=Caps(actions_per_day=200, filter_changes_per_week=10),
    )


@pytest.fixture
def provider():
    m = create_autospec(Provider, instance=True)
    m.kind = "gmail"
    m.capabilities.return_value = GMAIL
    m.get_tags.return_value = frozenset({"INBOX"})
    m.archive.side_effect = lambda ref: ref
    m.unarchive.side_effect = lambda ref: ref
    # Protected rules already in place, so message tests see no filter writes.
    m.get_rules.return_value = [managed("f-mom", MOM), managed("f-tax", TAX)]
    m.create_rule.return_value = "f-new"
    m.tag_matching.return_value = 0
    return m


@pytest.fixture
def protected():
    m = create_autospec(ProtectedFile, instance=True)
    m.current.return_value = LISTED
    return m


@pytest.fixture
def clock():
    m = create_autospec(utcnow)
    m.return_value = T0
    return m


@pytest.fixture
def audit():
    return create_autospec(emit_audit)


@pytest.fixture
def metrics():
    return create_autospec(Metrics, instance=True)


@pytest.fixture
def actions(config, provider, protected, clock, audit, metrics):
    return MailboxActions(
        config,
        {"personal": provider},
        protected=protected,
        clock=clock,
        audit=audit,
        metrics=metrics,
    )


def events(audit):
    return [c.args[0] for c in audit.call_args_list]


# --- refs -----------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    ["gmail:personal:18c2f0a1b2c3d4e5", "gmail:personal::18C2F0A1B2C3D4E5", "", "imap:x:::1:2"],
)
def test_invalid_refs_are_refused_before_the_provider(actions, provider, audit, ref):
    result = actions.file(ref, principal="orchestrator")

    assert (result.ok, result.reason) == (False, "invalid_ref")
    provider.get_tags.assert_not_called()
    assert events(audit)[0]["outcome"] == "refused:invalid_ref"


def test_unconfigured_mailbox_is_refused(actions, provider):
    result = actions.file("gmail:work::18c2f0a1b2c3d4e5")

    assert (result.ok, result.reason) == (False, "unknown_mailbox")
    provider.get_tags.assert_not_called()


def test_ref_of_another_kind_is_refused(actions, provider):
    result = actions.label("imap:personal::INBOX:1:2", "Banking")

    assert (result.ok, result.reason) == (False, "kind_mismatch")
    provider.add_tag.assert_not_called()


# --- file / unfile ----------------------------------------------------------


def test_file_tags_and_archives(actions, provider):
    result = actions.file(REF)

    assert result.ok and result.action == "file" and result.provider == "gmail"
    assert result.new_ref is None
    provider.add_tag.assert_called_once_with(MSG, "linear")
    provider.archive.assert_called_once_with(MSG)


def test_file_is_refused_for_protected_mail(actions, provider):
    provider.get_tags.return_value = frozenset({"INBOX", "Family"})

    result = actions.file(REF)

    assert (result.ok, result.reason) == (False, "protected")
    provider.add_tag.assert_not_called()
    provider.archive.assert_not_called()


def test_file_is_refused_for_a_nested_protected_label(actions, provider):
    provider.get_tags.return_value = frozenset({"Tax/2025"})

    assert actions.file(REF).reason == "protected"


def test_label_is_allowed_on_protected_mail(actions, provider):
    provider.get_tags.return_value = frozenset({"INBOX", "Family"})

    result = actions.label(REF, "Insurance")

    assert result.ok
    provider.add_tag.assert_called_once_with(MSG, "Auto/Insurance")
    provider.archive.assert_not_called()


def test_file_twice_is_idempotent(actions, provider):
    first = actions.file(REF)
    provider.get_tags.return_value = frozenset({"linear"})  # what the first call left
    second = actions.file(REF)

    assert first == second
    provider.add_tag.assert_called_once_with(MSG, "linear")
    assert provider.archive.call_count == 2  # archive is idempotent at the provider


def test_unfile_restores(actions, provider):
    result = actions.unfile(REF)

    assert result.ok and result.action == "unfile"
    provider.unarchive.assert_called_once_with(MSG)
    provider.remove_tag.assert_called_once_with(MSG, "linear")


def test_moved_ref_is_returned(actions, provider):
    moved = parse_ref("gmail:personal::aaaaaaaabbbbbbbb")
    provider.archive.side_effect = None
    provider.archive.return_value = moved

    assert actions.file(REF).new_ref == str(moved)


def test_provider_error_is_reported_not_raised(actions, provider, audit):
    provider.get_tags.side_effect = ProviderError("notFound")

    result = actions.file(REF)

    assert (result.ok, result.reason) == (False, "provider:notFound")
    assert events(audit)[0]["outcome"] == "refused:provider:notFound"


def test_unexpected_errors_are_refused_without_their_text(actions, provider, audit):
    provider.get_tags.side_effect = RuntimeError("secret detail")

    result = actions.file(REF)

    assert (result.ok, result.reason) == (False, "internal")
    assert "secret detail" not in repr(result)
    assert events(audit)[0]["outcome"] == "refused:internal"


# --- protected list absent or invalid -----------------------------------------


def test_file_only_labels_while_the_protected_list_is_empty(actions, provider, protected):
    protected.current.return_value = Protected()

    result = actions.file(REF)

    assert (result.ok, result.action, result.reason) == (True, "label_only", "protected_list_empty")
    provider.add_tag.assert_called_once_with(MSG, "linear")
    provider.archive.assert_not_called()
    provider.create_rule.assert_not_called()


def test_file_only_labels_while_the_protected_list_is_invalid(actions, provider, protected):
    protected.current.side_effect = ConfigError("bad")

    result = actions.file(REF)

    assert (result.action, result.reason) == ("label_only", "protected_list_invalid")
    provider.archive.assert_not_called()


def test_unfile_works_while_the_protected_list_is_empty(actions, provider, protected):
    protected.current.return_value = Protected()

    assert actions.unfile(REF).ok
    provider.unarchive.assert_called_once_with(MSG)


def test_archiving_resumes_once_the_list_arrives(actions, provider, protected):
    protected.current.return_value = Protected()
    provider.get_rules.return_value = []
    assert actions.file(REF).action == "label_only"

    protected.current.return_value = LISTED
    result = actions.file(REF)

    assert result.action == "file"
    provider.archive.assert_called_once_with(MSG)
    # The protected filters were installed, and existing protected mail tagged,
    # before the first archive.
    assert provider.create_rule.call_args_list == [call(MOM), call(TAX)]
    names = [c[0] for c in provider.mock_calls]
    assert names.index("tag_matching") < names.index("archive")


# --- protected backfill -------------------------------------------------------

BACKFILL = [
    call(Match.from_addr, "mom@example.org", "Family"),
    call(Match.from_domain, "tax.example.net", "Tax"),
]


def test_completed_backfill_lets_file_archive(actions, provider):
    actions.ensure_protected()

    result = actions.file(REF)

    assert result.action == "file"
    provider.archive.assert_called_once_with(MSG)
    assert provider.tag_matching.call_args_list == BACKFILL  # once, not per call
    assert actions.capabilities().archive_enabled is True


def test_pending_backfill_only_labels(actions, provider):
    provider.tag_matching.side_effect = ProviderError("rateLimitExceeded")

    result = actions.file(REF)

    assert (result.ok, result.action, result.reason) == (
        True,
        "label_only",
        "protected_backfill_pending",
    )
    provider.add_tag.assert_called_once_with(MSG, "linear")
    provider.archive.assert_not_called()
    assert actions.capabilities().archive_enabled is False


def test_pending_backfill_still_refuses_protected_mail(actions, provider):
    provider.tag_matching.side_effect = ProviderError("rateLimitExceeded")
    provider.get_tags.return_value = frozenset({"Family"})

    assert actions.file(REF).reason == "protected"


def test_backfill_error_leaves_it_pending_until_a_retry_succeeds(actions, provider, audit):
    provider.tag_matching.side_effect = [0, ProviderError("backendError")]

    actions.ensure_protected()

    assert events(audit)[0]["outcome"] == "refused:provider:backendError"
    assert actions.capabilities().archive_enabled is False

    provider.tag_matching.side_effect = None
    assert actions.file(REF).action == "file"
    assert actions.capabilities().archive_enabled is True


def test_a_changed_list_makes_the_backfill_pending_again(actions, provider, protected):
    actions.ensure_protected()
    assert actions.capabilities().archive_enabled is True

    protected.current.return_value = replace(LISTED, legal=frozenset({"law.example"}))
    assert actions.capabilities().archive_enabled is False
    provider.tag_matching.side_effect = ProviderError("backendError")

    assert actions.file(REF).reason == "protected_backfill_pending"
    provider.archive.assert_not_called()

    provider.tag_matching.side_effect = None
    assert actions.file(REF).action == "file"
    provider.tag_matching.assert_called_with(Match.from_domain, "law.example", "Legal")


def test_capabilities_report_archiving_state(actions, protected, audit):
    assert actions.capabilities().archive_enabled is False  # backfill not yet run
    actions.ensure_protected()
    assert actions.capabilities().archive_enabled is True

    protected.current.return_value = Protected()
    status = actions.capabilities(principal="orchestrator")

    assert (status.archive_enabled, status.protected_list) == (False, "empty")
    assert status.mailboxes == {"personal": GMAIL}
    assert events(audit)[-1]["tool"] == "capabilities"

    protected.current.side_effect = ConfigError("bad")
    assert actions.capabilities().protected_list == "invalid"


# --- label ------------------------------------------------------------------


@pytest.mark.parametrize("group", ["Family", "Banking/../Family", "", None])
def test_label_outside_the_allow_list_is_refused(actions, provider, group):
    result = actions.label(REF, group)

    assert (result.ok, result.reason) == (False, "group_not_allowed")
    provider.add_tag.assert_not_called()


# --- caps -------------------------------------------------------------------


def test_daily_cap_holds_and_rolls(config, provider, protected, clock, audit, metrics):
    actions = MailboxActions(
        replace(config, caps=Caps(actions_per_day=2)),
        {"personal": provider},
        protected=protected,
        clock=clock,
        audit=audit,
        metrics=metrics,
    )
    assert actions.label(REF, "Banking").ok
    assert actions.unfile(REF).ok

    assert actions.file(REF).reason == "cap_exceeded"

    clock.return_value = T0 + timedelta(days=1)
    assert actions.file(REF).ok


def test_refused_arguments_do_not_spend_the_cap(config, provider, protected, clock, audit, metrics):
    actions = MailboxActions(
        replace(config, caps=Caps(actions_per_day=1)),
        {"personal": provider},
        protected=protected,
        clock=clock,
        audit=audit,
        metrics=metrics,
    )
    actions.file("not-a-ref")
    actions.label(REF, "Nope")

    assert actions.label(REF, "Banking").ok


# --- audit ------------------------------------------------------------------


def test_audit_carries_a_hashed_ref_never_the_raw_one(actions, audit):
    actions.file(REF, principal="orchestrator")
    actions.label(REF, "Banking", principal="orchestrator")
    actions.label(REF, "Secret", principal="orchestrator")

    lines = [json.dumps(e) for e in events(audit)]
    assert len(lines) == 3
    assert all(MSG.uid not in line for line in lines)
    first = events(audit)[0]
    assert first["ref"] == {"source": "gmail:personal", "sha256": hash_ref(REF)["sha256"]}
    assert (first["tool"], first["principal"], first["outcome"]) == ("file", "orchestrator", "file")
    assert events(audit)[1]["group"] == "Banking"
    assert events(audit)[2]["group"] is None  # an unlisted group is not echoed


# --- metrics ----------------------------------------------------------------


def outcomes(metrics):
    return [c.kwargs["outcome"] for c in metrics.tool_call.call_args_list]


def test_a_call_records_its_metrics(actions, metrics):
    actions.file(REF, principal="orchestrator")

    metrics.tool_call.assert_called_once_with(
        tool="file", outcome="file", principal="orchestrator", duration_ms=ANY
    )
    assert metrics.tool_call.call_args.kwargs["duration_ms"] >= 0


def test_protected_refusal_is_recorded(actions, provider, metrics):
    provider.get_tags.return_value = frozenset({"INBOX", "Family"})

    actions.file(REF)

    assert outcomes(metrics) == ["refused:protected"]


def test_label_only_is_recorded(actions, protected, metrics):
    protected.current.return_value = Protected()

    actions.file(REF)

    assert outcomes(metrics) == ["label_only"]


def test_internal_error_is_recorded(actions, provider, metrics):
    provider.get_tags.side_effect = RuntimeError("secret detail")

    actions.file(REF)

    assert outcomes(metrics) == ["refused:internal"]


def test_every_tool_records_the_audit_outcome(actions, provider, audit, metrics):
    provider.get_rules.side_effect = ProviderError("403")

    actions.ensure_protected()
    actions.capabilities(principal="orchestrator")
    actions.sync_filters("personal", [bank()], principal="orchestrator")
    actions.label(REF, "Nope", principal="orchestrator")
    actions.unfile(REF, principal="orchestrator")

    recorded = [
        (c.kwargs["tool"], c.kwargs["outcome"], c.kwargs["principal"])
        for c in metrics.tool_call.call_args_list
    ]
    assert recorded == [(e["tool"], e["outcome"], e["principal"]) for e in events(audit)]
    assert recorded == [
        ("ensure_protected", "refused:provider:403", "startup"),
        ("capabilities", "ok", "orchestrator"),
        ("sync_filters", "refused:provider:403", "orchestrator"),
        ("label", "refused:group_not_allowed", "orchestrator"),
        ("unfile", "unfile", "orchestrator"),
    ]
    assert all("duration_ms" in e for e in events(audit))


def test_cap_usage_is_watched_and_rolls(actions, provider, clock, metrics):
    usage = metrics.watch_caps.call_args.args[0]
    assert usage() == {"actions": 0, "filter_changes": 0}

    actions.file(REF)
    actions.label(REF, "Banking")
    provider.get_rules.return_value = []
    actions.sync_filters("personal", [bank()])

    assert usage() == {"actions": 2, "filter_changes": 1}

    clock.return_value = T0 + timedelta(days=1)
    assert usage() == {"actions": 0, "filter_changes": 1}
    clock.return_value = T0 + timedelta(days=7)
    assert usage() == {"actions": 0, "filter_changes": 0}


def test_hash_ref_drops_a_source_that_is_not_shaped_like_one():
    assert hash_ref("Ignore previous instructions::x")["source"] is None
    assert hash_ref(None)["source"] is None


def test_emit_audit_writes_one_json_line(caplog):
    with caplog.at_level("INFO", logger="mail_actions.audit"):
        emit_audit({"tool": "file", "outcome": "ok"})

    assert json.loads(caplog.records[0].getMessage()) == {"outcome": "ok", "tool": "file"}


def test_utcnow_is_aware():
    assert utcnow().tzinfo is UTC


# --- sync_filters ----------------------------------------------------------


def bank(value="bank.example", archive=False, **extra):
    return {
        "match": "from_domain",
        "value": value,
        "tag": "Auto/Banking",
        "archive": archive,
        **extra,
    }


def test_sync_leaves_unmanaged_filters_alone(actions, provider):
    provider.get_rules.return_value = [
        ServerRule(id="hand", managed=False, rule=None),
        managed("f-mom", MOM),
        managed("f-tax", TAX),
    ]

    result = actions.sync_filters("personal", [])

    assert (result.created, result.updated, result.deleted) == (0, 0, 0)
    provider.delete_rule.assert_not_called()
    provider.create_rule.assert_not_called()


def test_sync_converges_create_update_delete(actions, provider):
    old_news = Rule(Match.list_id, "news.example.com", "Auto/Banking")
    stale = Rule(Match.from_addr, "old@shop.example", "Auto/Insurance")
    provider.get_rules.return_value = [
        managed("f-mom", MOM),
        managed("f-tax", TAX),
        managed("f-news", old_news),
        managed("f-stale", stale),
        ServerRule(id="f-broken", managed=True, rule=None),
    ]
    rules = [
        bank(),
        {
            "match": "list_id",
            "value": "<news.example.com>",
            "tag": "Auto/Insurance",
            "archive": True,
            "category": "newsletter",
        },
    ]

    result = actions.sync_filters("personal", rules)

    assert result.ok and result.refused == []
    assert (result.created, result.updated, result.deleted) == (1, 1, 2)
    assert {c.args[0] for c in provider.create_rule.call_args_list} == {
        Rule(Match.from_domain, "bank.example", "Auto/Banking"),
        Rule(Match.list_id, "news.example.com", "Auto/Insurance", archive=True),
    }
    assert {c.args[0] for c in provider.delete_rule.call_args_list} == {
        "f-news",
        "f-stale",
        "f-broken",
    }


def test_sync_installs_protected_rules(actions, provider):
    provider.get_rules.return_value = []

    result = actions.sync_filters("personal", [])

    assert result.created == 2
    assert provider.create_rule.call_args_list == [call(MOM), call(TAX)]


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        (bank("tax.example.net", archive=True, category="bulk"), "protected_sender"),
        (bank("mail.tax.example.net", archive=True, category="bulk"), "protected_sender"),
        (
            bank("mom@example.org", archive=True, category="bulk", match="from_addr"),
            "protected_sender",
        ),
        (bank("shop.example", archive=True), "archive_needs_bulk_category"),
        (bank("shop.example", archive=True, category="personal"), "archive_needs_bulk_category"),
        (bank(tag="Family"), "tag_not_allowed"),
        ({**bank(), "tag": "Family"}, "tag_not_allowed"),
        ({**bank(), "match": "subject"}, "invalid_match"),
        ({**bank(), "value": "not a domain"}, "invalid_value"),
        ({**bank(), "value": 7}, "invalid_value"),
        ({**bank(), "archive": "yes"}, "invalid_archive"),
        ({**bank(), "query": "from:*"}, "invalid_shape"),
        ("from:*", "invalid_shape"),
        (
            {"match": "from_domain", "value": "tax.example.net", "tag": "Auto/Banking"},
            "protected_rule",
        ),
    ],
)
def test_sync_refuses_unapproved_rules(actions, provider, rule, reason):
    result = actions.sync_filters("personal", [rule])

    assert result.ok
    assert result.refused == [{"index": 0, "reason": reason}]
    provider.create_rule.assert_not_called()


def test_sync_refuses_archive_rules_while_the_list_is_empty(actions, provider, protected):
    protected.current.return_value = Protected()
    provider.get_rules.return_value = []

    result = actions.sync_filters(
        "personal", [bank(archive=True, category="bulk"), bank("b.example")]
    )

    assert result.refused == [{"index": 0, "reason": "protected_list_empty"}]
    provider.create_rule.assert_called_once_with(
        Rule(Match.from_domain, "b.example", "Auto/Banking")
    )


def test_a_protected_address_does_not_block_archive_rules_for_its_domain(actions, provider):
    result = actions.sync_filters("personal", [bank("example.org", archive=True, category="bulk")])

    assert result.refused == []
    provider.create_rule.assert_called_once_with(
        Rule(Match.from_domain, "example.org", "Auto/Banking", archive=True)
    )


def test_empty_list_leaves_existing_protected_filters_alone(actions, provider, protected):
    protected.current.return_value = Protected()
    stale = Rule(Match.from_addr, "old@shop.example", "Auto/Banking")
    provider.get_rules.return_value = [
        managed("f-mom", MOM),
        managed("f-tax", TAX),
        managed("f-stale", stale),
    ]

    result = actions.sync_filters("personal", [bank(), bank("mom@example.org", match="from_addr")])

    assert result.refused == [{"index": 1, "reason": "protected_rule"}]
    provider.create_rule.assert_called_once_with(
        Rule(Match.from_domain, "bank.example", "Auto/Banking")
    )
    provider.delete_rule.assert_called_once_with("f-stale")


def test_refused_rule_keeps_its_existing_filter(actions, provider):
    kept = Rule(Match.from_domain, "shop.example", "Auto/Banking", archive=True)
    provider.get_rules.return_value = [
        managed("f-mom", MOM),
        managed("f-tax", TAX),
        managed("f-shop", kept),
    ]

    result = actions.sync_filters("personal", [bank("shop.example", archive=True)])

    assert result.refused == [{"index": 0, "reason": "archive_needs_bulk_category"}]
    provider.delete_rule.assert_not_called()


def test_duplicate_rules_keep_the_first(actions, provider):
    result = actions.sync_filters("personal", [bank(), bank(tag="Auto/Insurance")])

    assert result.refused == [{"index": 1, "reason": "duplicate"}]
    provider.create_rule.assert_called_once_with(
        Rule(Match.from_domain, "bank.example", "Auto/Banking")
    )


def test_protected_rule_overrides_a_managed_rule_for_the_same_sender(actions, provider):
    provider.get_rules.return_value = [
        managed("f-mom", Rule(Match.from_addr, "mom@example.org", "Auto/Banking", archive=True)),
        managed("f-tax", TAX),
    ]

    result = actions.sync_filters("personal", [bank("mom@example.org")])

    assert result.updated == 1
    provider.create_rule.assert_called_once_with(MOM)
    provider.delete_rule.assert_called_once_with("f-mom")


def test_weekly_filter_cap_holds_but_exempts_protected_rules(
    config, provider, protected, clock, audit, metrics
):
    actions = MailboxActions(
        replace(config, caps=Caps(filter_changes_per_week=2)),
        {"personal": provider},
        protected=protected,
        clock=clock,
        audit=audit,
        metrics=metrics,
    )
    provider.get_rules.return_value = []  # two protected creates: exempt

    assert actions.sync_filters("personal", [bank("a.example"), bank("b.example")]).created == 4

    result = actions.sync_filters("personal", [bank("c.example")])
    assert (result.ok, result.reason) == (False, "cap_exceeded")

    clock.return_value = T0 + timedelta(days=7)
    assert actions.sync_filters("personal", [bank("c.example")]).ok


@pytest.mark.parametrize(
    ("mailbox", "reason"), [("work", "unknown_mailbox"), (None, "unknown_mailbox")]
)
def test_sync_refuses_unknown_mailboxes(actions, mailbox, reason):
    assert actions.sync_filters(mailbox, []).reason == reason


def test_sync_refuses_without_server_rules(actions, provider):
    provider.capabilities.return_value = replace(GMAIL, server_rules=ServerRules.none)

    assert actions.sync_filters("personal", []).reason == "server_rules_unsupported"


def test_sync_refuses_rules_that_are_not_a_list(actions):
    assert actions.sync_filters("personal", "from:*").reason == "rules_not_a_list"


def test_sync_refuses_while_the_protected_list_is_invalid(actions, protected):
    protected.current.side_effect = ConfigError("bad")

    assert actions.sync_filters("personal", []).reason == "protected_list_invalid"


def test_sync_reports_provider_errors(actions, provider, audit):
    provider.get_rules.side_effect = ProviderError("rateLimitExceeded")

    result = actions.sync_filters("personal", [bank()])

    assert result.reason == "provider:rateLimitExceeded"
    event = events(audit)[0]
    assert (event["tool"], event["mailbox"], event["rules"]) == ("sync_filters", "personal", 1)
    assert event["outcome"] == "refused:provider:rateLimitExceeded"


# --- ensure_protected --------------------------------------------------------


def test_ensure_protected_installs_and_keeps_approved_rules(actions, provider, audit):
    approved = Rule(Match.from_domain, "bank.example", "Auto/Banking")
    provider.get_rules.return_value = [
        managed("f-bank", approved),
        managed("f-gone", Rule(Match.from_addr, "x@y.example", "Tax")),
    ]

    actions.ensure_protected()

    assert provider.create_rule.call_args_list == [call(MOM), call(TAX)]
    provider.delete_rule.assert_called_once_with("f-gone")
    assert events(audit)[0]["outcome"] == "ok"

    actions.ensure_protected()  # already converged to this list: no further reads
    provider.get_rules.assert_called_once()


def test_ensure_protected_records_provider_errors(actions, provider, audit):
    provider.get_rules.side_effect = ProviderError("403")

    actions.ensure_protected()

    assert events(audit)[0]["outcome"] == "refused:provider:403"


def test_ensure_protected_skips_an_invalid_list(actions, provider, protected):
    protected.current.side_effect = ConfigError("bad")

    actions.ensure_protected()

    provider.get_rules.assert_not_called()


def test_ensure_protected_with_an_empty_list_leaves_existing_protected_filters_alone(
    actions, provider, protected
):
    protected.current.return_value = Protected()

    actions.ensure_protected()

    provider.get_rules.assert_not_called()
    provider.delete_rule.assert_not_called()


def test_ensure_protected_skips_providers_without_rules(actions, provider):
    provider.capabilities.return_value = replace(GMAIL, server_rules=ServerRules.none)

    actions.ensure_protected()

    provider.get_rules.assert_not_called()
