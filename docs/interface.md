# Interface

`mail-actions serve` exposes five MCP tools over streamable HTTP at `/mcp` on
port 9000. The server is stateless (`stateless_http=True`), so a long-lived
client never holds an expiring session. Every tool calls into
`MailboxActions`, which applies the rules below and writes one audit line.

## Tools

### `file(ref)`

Tags the message with the filing label (`linear` by default) and archives it.
It is idempotent: the tag is added only if it is missing, and archiving an
archived message changes nothing.

- **Refused** (`protected`) if the message carries a protected tag (`Family`,
  `Tax`, `Legal`, or one nested under them, such as `Tax/2025`). Reading the
  tags is the one read the service makes of a message.
- **While the protected list is missing or empty**, `file` tags the message and
  leaves it in the inbox. It returns `ok: true, action: "label_only", reason:
  "protected_list_empty"`. An unreadable list does the same with
  `reason: "protected_list_invalid"`. Without the list nothing can be recognised
  as protected, so nothing is archived.
- Once the list has entries, `file` first installs the protected filters and
  tags the protected senders' existing mail (the protected backfill) if that
  has not yet been done for the current list, then archives as normal. Until
  the backfill has completed, `file` tags without archiving and returns
  `action: "label_only", reason: "protected_backfill_pending"`; a provider
  error leaves it pending and the next call retries. The list is re-read
  whenever the file changes, so no restart is needed, and a changed list makes
  the backfill pending again.

### `unfile(ref)`

Returns the message to the inbox and removes the filing label. It works whether
or not a protected list is configured.

### `label(ref, group)`

Adds `Auto/<group>`, where `group` is one of the configured `groups`. It never
archives, so it is allowed on protected mail. An unlisted group is refused
(`group_not_allowed`) before any provider call and does not count against the
cap.

### `sync_filters(mailbox, rules)`

Converges this service's server-side filters in `mailbox` to `rules`, which is
the complete approved set. A managed filter that is not listed is deleted.

Each rule:

```json
{"match": "from_domain" | "from_addr" | "list_id",
 "value": "bank.example",
 "tag": "Auto/Banking",
 "archive": false,
 "category": "bulk" | "newsletter" | "promotional"}
```

- `tag` must be an allow-listed `Auto/<group>`.
- `archive: true` is refused unless `category` is `bulk`, `newsletter` or
  `promotional` (`archive_needs_bulk_category`), the sender is not covered by
  the protected list (`protected_sender`), and the protected list is not empty
  (`protected_list_empty`). A sender is covered if it is a protected entry or is
  at a protected domain or subdomain. A protected address covers only itself, not
  its domain.
- A rule for a sender that has a protected rule is refused (`protected_rule`).
  Protected rules come only from the protected list.
- A refused rule is reported in `refused: [{index, reason}]`, and any existing
  managed filter for that sender is left in place. The rest of the set is still
  applied.
- Filters this service did not create are never read back as managed, so they
  are never changed or deleted.
- Per-rule reasons: `invalid_shape` (not a mapping, or unknown keys),
  `invalid_match`, `invalid_value`, `invalid_archive`, `tag_not_allowed`,
  `archive_needs_bulk_category`, `protected_sender`, `protected_list_empty`,
  `protected_rule`, `duplicate` (a second rule for the same sender; the first
  wins).
- Changes to approved rules count against the weekly cap. If a sync would
  exceed it, nothing is changed and the call returns `reason: "cap_exceeded"`.
  Changes to protected rules do not count.

### `capabilities()`

```json
{"archive_enabled": false,
 "protected_list": "empty",
 "mailboxes": {"personal": {"tag_model": "labels", "archive": "remove_inbox_tag",
                            "server_rules": "gmail_filters", "protected_check": "tag"}}}
```

`archive_enabled` is false while the protected list is `empty` or `invalid`,
or while any mailbox's protected backfill for the current list is pending.

## Results and refusal reasons

Message tools return `{ok, ref, action, provider, new_ref, reason}`. `new_ref` is
set only when the provider changed the ref (an IMAP move). `sync_filters`
returns `{ok, mailbox, created, updated, deleted, refused, reason}`. Refusals are
results, not errors, so the caller always gets a reason:

| Reason | Meaning |
| --- | --- |
| `invalid_ref` | not a `corpus.id/v1` ref |
| `unknown_mailbox` | the mailbox is not configured here |
| `kind_mismatch` | the ref's kind is not the mailbox's provider |
| `protected` | `file` on a message with a protected tag |
| `group_not_allowed` | `label` with a group outside the allow-list |
| `cap_exceeded` | the daily action cap or the weekly filter-change cap is spent |
| `server_rules_unsupported` | the provider has no server-side rules |
| `rules_not_a_list`, `protected_list_invalid` | `sync_filters` could not start |
| `provider:<code>` | the provider call failed, e.g. `provider:notFound` |
| `internal` | an unexpected error; the detail is logged server-side, never returned |

## Caps

At most 200 `file`/`unfile`/`label` calls per rolling 24 hours, and 10
approved-filter changes per rolling 7 days. Both limits are configurable. The
counters live in memory, so a restart resets them. The gateway's rate limit is
the backstop for that.

## Audit

Each call writes one JSON line on the `mail_actions.audit` logger:

```json
{"event": "mail_actions.tool_call", "tool": "file", "principal": "orchestrator",
 "ref": {"source": "gmail:personal", "sha256": "3f1c0a9b2d4e"},
 "outcome": "file", "duration_ms": 41.2}
```

`principal` comes from the `x-client-id` header that the gateway sets. A
missing header gives `unknown`, a repeated one `ambiguous`, and a malformed one
`other`. The ref is logged as its `kind:mailbox` plus the first 12 hex digits
of its SHA-256. The uid itself is never logged. `sync_filters` logs the rule
count and the created, updated, deleted and refused counts, never the rules.

## Configuration

See `deploy/config.example.yaml` and `deploy/protected.example.yaml`.

| Setting | Source |
| --- | --- |
| config file | `--config` or `MAIL_ACTIONS_CONFIG` (default `/etc/mail-actions/config.yaml`) |
| protected list | `MAIL_ACTIONS_PROTECTED`, else `protected_path`, else `/etc/mail-actions/protected.yaml` |
| caps | `caps:` in the config; `MAIL_ACTIONS_ACTIONS_PER_DAY` / `MAIL_ACTIONS_FILTER_CHANGES_PER_WEEK` override |
| credentials | `<credentials_env_prefix>_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN}` |
| bind | `--host`/`--port` or `MAIL_ACTIONS_HOST`/`MAIL_ACTIONS_PORT` (default `0.0.0.0:9000`) |

## CLI

- `mail-actions serve` resolves labels, converges the protected filters, tags
  the protected senders' existing mail, and then serves.
- `mail-actions consent <mailbox> --client-secret <file> [--out <path>]` runs
  the one-time OAuth consent in a browser and writes the credentials to an env
  file (default `./mail-actions-<mailbox>.env`). The file is created with mode
  0600 and never overwritten; only its path and the variable names are
  printed. It needs the `consent` extra.
- `mail-actions check` validates the config and the protected list, then mints
  a token per mailbox to prove its credentials and scopes. It reads no mail. A
  missing or empty protected list is a warning; an invalid list, bad
  credentials or a missing scope fail the check. A token broader than needed
  is a warning.
