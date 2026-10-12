"""Service configuration and the protected-sender list.

The config file (``MAIL_ACTIONS_CONFIG``) names the mailboxes, the filing tag,
the allow-listed label groups and the caps. The protected list
(``MAIL_ACTIONS_PROTECTED``) is a separate, secret file of family, tax and legal
senders; it is optional and re-read whenever it changes, so it can be supplied
after deployment without a restart.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .ids import KINDS

DEFAULT_CONFIG = "/etc/mail-actions/config.yaml"
DEFAULT_PROTECTED = "/etc/mail-actions/protected.yaml"

#: Protected categories and the tag each one's filter applies. Fixed, so every
#: protected label can be resolved at startup even before the list arrives.
PROTECTED_TAGS = {"family": "Family", "tax": "Tax", "legal": "Legal"}
GROUP_PREFIX = "Auto/"

_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_GROUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}")
_ENV_PREFIX = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
ADDRESS = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@" + DOMAIN.pattern)


class ConfigError(ValueError):
    """The configuration or the protected list is invalid."""


@dataclass(frozen=True, slots=True)
class Mailbox:
    name: str
    kind: str
    credentials_env_prefix: str


@dataclass(frozen=True, slots=True)
class Caps:
    actions_per_day: int = 200
    filter_changes_per_week: int = 10


@dataclass(frozen=True, slots=True)
class Config:
    mailboxes: tuple[Mailbox, ...]
    groups: frozenset[str]
    label: str = "linear"
    caps: Caps = field(default_factory=Caps)
    protected_path: str = DEFAULT_PROTECTED

    @property
    def group_tags(self) -> frozenset[str]:
        return frozenset(GROUP_PREFIX + g for g in self.groups)

    @property
    def tags(self) -> frozenset[str]:
        """Every tag this service may apply, resolved once at startup."""
        return self.group_tags | {self.label} | frozenset(PROTECTED_TAGS.values())


@dataclass(frozen=True, slots=True)
class Protected:
    """Protected senders by category; each entry is an address or a domain."""

    family: frozenset[str] = frozenset()
    tax: frozenset[str] = frozenset()
    legal: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.family or self.tax or self.legal)

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(PROTECTED_TAGS.values())

    def entries(self) -> list[tuple[str, str]]:
        """``(entry, tag)`` pairs in a stable order."""
        return [
            (entry, PROTECTED_TAGS[category])
            for category in PROTECTED_TAGS
            for entry in sorted(getattr(self, category))
        ]

    def covers(self, sender: str) -> bool:
        """Whether *sender* (an address, a domain or a list id) is or falls under a
        protected entry.

        A protected address covers only itself; a domain is protected only by a
        domain entry. A family address at a shared provider (``@gmail.com``) must
        not block rules for every other sender there.
        """
        sender = sender.lower().strip("<>")
        domain = sender.rpartition("@")[2]
        for entry, _ in self.entries():
            if sender == entry:
                return True
            if "@" not in entry and (domain == entry or domain.endswith("." + entry)):
                return True
        return False


def parse_protected(raw: Any) -> Protected:
    """Validate a parsed protected-list document. ``None`` (an empty file) is empty."""
    if raw is None:
        return Protected()
    if not isinstance(raw, Mapping):
        raise ConfigError("protected list must be a mapping of category to entries")
    unknown = set(raw) - set(PROTECTED_TAGS)
    if unknown:
        raise ConfigError(f"unknown protected categories: {sorted(unknown)}")
    lists: dict[str, frozenset[str]] = {}
    for category in PROTECTED_TAGS:
        entries = raw.get(category) or []
        if not isinstance(entries, list):
            raise ConfigError(f"protected.{category} must be a list")
        cleaned = set()
        for entry in entries:
            value = str(entry).strip().lower()
            if not (ADDRESS.fullmatch(value) or DOMAIN.fullmatch(value)):
                raise ConfigError(
                    f"protected.{category} has an entry that is not an address or a domain"
                )
            cleaned.add(value)
        lists[category] = frozenset(cleaned)
    return Protected(**lists)


class ProtectedFile:
    """The protected list on disk, re-read when the file changes.

    A missing file is an empty list: the service starts and runs safely without
    one (see ``MailboxActions``, which stops archiving while it is empty). A file
    that exists but does not parse raises :class:`ConfigError` on every read
    until it is fixed, so callers fail closed rather than run on a stale list.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._value = Protected()

    def current(self) -> Protected:
        with self._lock:
            try:
                st = self._path.stat()
            except FileNotFoundError:
                self._stamp, self._value = None, Protected()
                return self._value
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                try:
                    raw = yaml.safe_load(self._path.read_text())
                except yaml.YAMLError as exc:
                    raise ConfigError("protected list is not valid YAML") from exc
                self._value = parse_protected(raw)
                self._stamp = stamp
            return self._value


def _int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if number < 0:
        raise ConfigError(f"{name} must not be negative")
    return number


def parse_config(raw: Any, env: Mapping[str, str]) -> Config:
    """Validate a parsed config document; caps may be overridden from *env*."""
    if not isinstance(raw, Mapping):
        raise ConfigError("config must be a mapping")
    mailboxes = []
    for item in raw.get("mailboxes") or []:
        if not isinstance(item, Mapping):
            raise ConfigError("each mailbox must be a mapping")
        name, kind = str(item.get("name", "")), str(item.get("kind", ""))
        if not _NAME.fullmatch(name):
            raise ConfigError(f"mailbox name {name!r} must match {_NAME.pattern}")
        if kind not in KINDS:
            raise ConfigError(f"mailbox {name}: unknown kind {kind!r}")
        prefix = str(item.get("credentials_env_prefix") or f"MAIL_ACTIONS_{kind}_{name}").upper()
        if not _ENV_PREFIX.fullmatch(prefix):
            raise ConfigError(f"mailbox {name}: invalid credentials_env_prefix")
        mailboxes.append(Mailbox(name, kind, prefix))
    if not mailboxes:
        raise ConfigError("at least one mailbox is required")
    if len({m.name for m in mailboxes}) != len(mailboxes):
        raise ConfigError("mailbox names must be unique")

    groups = raw.get("groups") or []
    if not isinstance(groups, list) or not all(
        isinstance(g, str) and _GROUP.fullmatch(g) for g in groups
    ):
        raise ConfigError(f"groups must be a list of names matching {_GROUP.pattern}")
    label = str(raw.get("label", "linear"))
    if not _TAG.fullmatch(label) or label in PROTECTED_TAGS.values():
        raise ConfigError("label must be a plain tag name, not a protected one")

    caps_raw = raw.get("caps") or {}
    caps = Caps(
        actions_per_day=_int(
            env.get("MAIL_ACTIONS_ACTIONS_PER_DAY", caps_raw.get("actions_per_day", 200)),
            "actions_per_day",
        ),
        filter_changes_per_week=_int(
            env.get(
                "MAIL_ACTIONS_FILTER_CHANGES_PER_WEEK", caps_raw.get("filter_changes_per_week", 10)
            ),
            "filter_changes_per_week",
        ),
    )
    protected_path = env.get("MAIL_ACTIONS_PROTECTED") or str(
        raw.get("protected_path") or DEFAULT_PROTECTED
    )
    return Config(tuple(mailboxes), frozenset(groups), label, caps, protected_path)


def load_config(path: str | os.PathLike[str], env: Mapping[str, str]) -> Config:
    """Read and validate the config file at *path*."""
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config {path} is not valid YAML") from exc
    return parse_config(raw, env)
