# mail-actions

A small MCP server that is the only component allowed to change the user's
mailbox: it files (tag + archive), unfiles, labels, and converges server-side
filter rules. It is provider-neutral: one intent interface (`MailboxActions`)
in front of provider adapters (`GmailAdapter` first). It never returns message
content.

## Linear

- Team: `software` (issue prefix `DIL`)
- Parent: DIL-650 (Hermes mail steward); message-id contract: DIL-655

## Threat model (summary)

The caller is an LLM agent that reads untrusted email, so every tool argument
may be attacker-chosen. The full model is in `docs/threat-model.md`.

- No tool and no adapter method returns subject, sender, body, headers or
  snippet. The only read is a message's tag ids, for the protected check.
- Every rule lives in `MailboxActions`, not in adapters: ref validation,
  protected-tag refusal, the label allow-list, filter-rule validation, caps and
  the audit line. An adapter that lacks a rule cannot weaken it.
- Protected mail (family, tax, legal) is never archived, and protected filter
  rules come only from the mounted protected list, never from a tool call.
- Filters the user made by hand are never touched.

## Layout

- `python/mail_actions/` — the package (`src/mail_actions/`, `tests/`).
- `docs/` — one page per concern, indexed from `docs/index.md`.
- `deploy/Dockerfile` — the runtime image (non-root, uid 1000).

## Common commands

Enter the dev shell first (`nix develop`), then:

```bash
just setup   # create the venv and install test + dev deps
just test    # unit tests
just check   # ruff (lint + format), tests, coverage ratchet, commitlint
```

Run `just check` and fix everything it reports before pushing. Coverage is
ratcheted (`.github/coverage-baseline.txt`): it may not drop, and a rise must
raise the baseline in the same change.

## Commits

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`build:`, `ci:`, `chore:`), enforced by `commitlint.config.mjs` in CI and in
`just check`. Reference the Linear issue in the body.

## Drift discipline

| When you change | Also update |
| --- | --- |
| a tool's name, arguments or result | `docs/interface.md` |
| the Gmail request shapes or scopes | `docs/providers/gmail.md` |
| a refusal rule, cap or the audit line | `docs/threat-model.md` |
| the ref codec or `tests/vectors/message_ids.json` | `docs/message-ids.md` |
| a metric, its labels or an outcome value | `docs/observability.md` |
