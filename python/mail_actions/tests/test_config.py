import os

import pytest

from mail_actions.config import (
    DEFAULT_PROTECTED,
    Caps,
    ConfigError,
    Mailbox,
    Protected,
    ProtectedFile,
    load_config,
    parse_config,
    parse_protected,
)


@pytest.fixture
def raw():
    return {
        "mailboxes": [{"name": "personal", "kind": "gmail"}],
        "label": "linear",
        "groups": ["Banking", "Insurance"],
    }


def test_parse_config_applies_defaults(raw):
    config = parse_config(raw, {})

    assert config.mailboxes == (Mailbox("personal", "gmail", "MAIL_ACTIONS_GMAIL_PERSONAL"),)
    assert config.caps == Caps(200, 10)
    assert config.protected_path == DEFAULT_PROTECTED
    assert config.group_tags == {"Auto/Banking", "Auto/Insurance"}
    assert config.tags == {"Auto/Banking", "Auto/Insurance", "linear", "Family", "Tax", "Legal"}


def test_env_overrides_caps_and_protected_path(raw):
    raw["caps"] = {"actions_per_day": 50}
    env = {"MAIL_ACTIONS_FILTER_CHANGES_PER_WEEK": "3", "MAIL_ACTIONS_PROTECTED": "/p.yaml"}

    config = parse_config(raw, env)

    assert config.caps == Caps(50, 3)
    assert config.protected_path == "/p.yaml"


def test_explicit_credentials_prefix(raw):
    raw["mailboxes"][0]["credentials_env_prefix"] = "GMAIL_ME"

    assert parse_config(raw, {}).mailboxes[0].credentials_env_prefix == "GMAIL_ME"


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(mailboxes=[]),
        lambda r: r.update(mailboxes=["personal"]),
        lambda r: r["mailboxes"].append({"name": "personal", "kind": "gmail"}),
        lambda r: r["mailboxes"][0].update(name="Personal"),
        lambda r: r["mailboxes"][0].update(kind="pop3"),
        lambda r: r["mailboxes"][0].update(credentials_env_prefix="bad prefix"),
        lambda r: r.update(groups=["Bad/Group"]),
        lambda r: r.update(groups="Banking"),
        lambda r: r.update(label="Family"),
        lambda r: r.update(caps={"actions_per_day": "many"}),
        lambda r: r.update(caps={"actions_per_day": -1}),
    ],
)
def test_invalid_configs_are_rejected(raw, change):
    change(raw)

    with pytest.raises(ConfigError):
        parse_config(raw, {})


def test_config_must_be_a_mapping():
    with pytest.raises(ConfigError):
        parse_config(["x"], {})


def test_load_config_reads_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mailboxes: [{name: personal, kind: gmail}]\ngroups: [Banking]\n")

    assert load_config(path, {}).groups == {"Banking"}


@pytest.mark.parametrize("content", [None, "mailboxes: [unclosed"])
def test_load_config_reports_unreadable_files(tmp_path, content):
    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content)

    with pytest.raises(ConfigError):
        load_config(path, {})


def test_parse_protected_normalises_entries():
    protected = parse_protected({"family": ["Mom@Example.org"], "tax": ["tax.example.net"]})

    assert protected == Protected(
        family=frozenset({"mom@example.org"}), tax=frozenset({"tax.example.net"})
    )
    assert protected.entries() == [("mom@example.org", "Family"), ("tax.example.net", "Tax")]
    assert not protected.empty


@pytest.mark.parametrize(
    "raw",
    [
        ["mom@example.org"],
        {"friends": []},
        {"family": "mom@example.org"},
        {"family": ["not an address"]},
    ],
)
def test_parse_protected_rejects_bad_documents(raw):
    with pytest.raises(ConfigError):
        parse_protected(raw)


@pytest.mark.parametrize(
    ("sender", "covered"),
    [
        ("mom@example.org", True),
        # A protected address does not protect its domain.
        ("example.org", False),
        ("someone@example.org", False),
        ("someone@tax.example.net", True),
        ("mail.tax.example.net", True),
        ("<list.tax.example.net>", True),
        ("tax.example.net.evil.example", False),
        ("nottax.example.net", False),
        ("shop.example", False),
    ],
)
def test_protected_covers(sender, covered):
    protected = Protected(family=frozenset({"mom@example.org"}), tax=frozenset({"tax.example.net"}))

    assert protected.covers(sender) is covered


def test_a_protected_address_at_a_shared_provider_does_not_cover_the_provider():
    protected = Protected(family=frozenset({"mom@gmail.com"}))

    assert protected.covers("mom@gmail.com")
    assert not protected.covers("gmail.com")
    assert not protected.covers("shop@gmail.com")


def test_protected_file_missing_is_empty(tmp_path):
    assert ProtectedFile(tmp_path / "absent.yaml").current().empty


def test_protected_file_is_reread_when_it_changes(tmp_path):
    path = tmp_path / "protected.yaml"
    source = ProtectedFile(path)
    assert source.current().empty

    path.write_text("family: [mom@example.org]\n")
    assert source.current().family == {"mom@example.org"}

    path.write_text("family: [mom@example.org]\nlegal: [lawyer@firm.example]\n")
    os.utime(path, ns=(1, 1))  # a distinct stamp even on a coarse-mtime filesystem
    assert source.current().legal == {"lawyer@firm.example"}

    path.unlink()
    assert source.current().empty


@pytest.mark.parametrize("content", ["family: [unclosed", "family: [not-an-address!]"])
def test_protected_file_invalid_raises_on_every_read(tmp_path, content):
    path = tmp_path / "protected.yaml"
    path.write_text(content)
    source = ProtectedFile(path)

    for _ in range(2):
        with pytest.raises(ConfigError):
            source.current()


def test_an_empty_protected_file_is_empty(tmp_path):
    path = tmp_path / "protected.yaml"
    path.write_text("")

    assert ProtectedFile(path).current().empty
