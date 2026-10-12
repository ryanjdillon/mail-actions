# Threat model

## Setting

An orchestrating agent reads summaries of the user's mail and decides what to
file, label or filter. The mail is written by third parties, so any tool
argument may be chosen by an attacker through prompt injection. The agent's
host has no hard gate on MCP tool calls. That makes this service's small tool
surface and its in-code refusals the real control.

The service holds a Gmail token that could read mail. It is the only component
with write access to the mailbox.

## Guarantees

| Guarantee | Where it holds |
| --- | --- |
| No tool returns message content | Results carry refs, actions and reasons only. The adapter's only read is `labelIds` with a `fields` mask, and a test pins the adapter's public methods to the port. |
| Every rule is enforced in one place | `MailboxActions`. Adapters only translate, so a new adapter cannot drop a rule. |
| Refs are validated | `corpus.id/v1` grammar, configured mailbox, and matching provider kind, all before any provider call. |
| Protected mail is not archived | `file` refuses a message with a protected tag. With no protected list it never archives. |
| Labels are allow-listed | `label` and filter rules may only apply `Auto/<group>` from config. |
| Filters are constrained | Only `from_domain`, `from_addr` or `list_id` → tag. Skipping the inbox requires a bulk/newsletter/promotional category, a non-protected sender and a configured protected list. Protected filters come only from the protected list. |
| Hand-made filters are untouched | Only filters with the service's marker and exactly a shape it writes are managed; any other filter is never changed or deleted. |
| Volume is bounded | 200 actions per day and 10 approved-filter changes per week, plus the gateway's rate limit. |
| Every call is audited | One JSON line per call with principal, tool, hashed ref and outcome. Raw refs and rules are never logged. |

## What an attacker who controls an email can do

- **File or label personal mail.** `unfile` reverses a file, and the daily cap
  bounds the volume. Protected mail cannot be archived.
- **Propose a filter.** It can only tag an allow-listed label, and it can skip
  the inbox only for a bulk category from a non-protected sender. In the
  intended workflow a human approves filter changes before the orchestrator
  calls `sync_filters`. Each change is audited and capped.
- **Not** read mail, send mail, delete or trash mail, change labels outside the
  allow-list, touch hand-made filters, or alter protected filters.

## Running without a protected list

The protected list may be supplied after deployment. Until it is present and
non-empty:

- `file` tags but does not archive (`action: "label_only"`).
- `sync_filters` refuses any rule that archives.
- `capabilities` reports `archive_enabled: false`.
- Existing protected filters are left exactly as they are. An empty or missing
  list means "not known yet", never "protect no one", so neither startup nor
  `sync_filters` deletes or changes a protected filter, and no approved rule may
  take over a sender that has one.

The list is re-read whenever its file changes. When entries appear or change,
the next `file` (or startup) installs the protected filters and then tags the
protected senders' existing mail (the protected backfill) before archiving
resumes. Until that pass completes for the current list, `file` tags without
archiving (`reason: "protected_backfill_pending"`); a provider error leaves it
pending and the next `file` retries. A list that exists but does not parse is treated the same way and
reported as `invalid`, so the service fails closed rather than running on a
stale copy.

## Residual risks

- **Mail that predates the protected filters.** Gmail filters label only new
  mail, so the protected backfill searches each protected sender and tags
  their existing messages before any archive. It reads message ids only. Mail
  the search misses (a sender writing from an address outside its listed
  entry) carries no protected label, so `file` can archive it. Archiving is
  reversible and does not delete.
- **Caps reset on restart.** The counters are in memory. The gateway rate
  limit is the durable bound.
- **Compromised service.** The token can read mail. The mitigations are
  deployment-level: its own namespace and Secret, a small reviewed image pinned
  by digest, ingress only from the gateway, and egress only to the provider.
  Broad egress on 443 remains a residual risk where the network layer cannot
  allowlist by domain.
- **Over-broad token.** `mail-actions check` warns when the token carries
  scopes beyond the two it needs.
