<!--
  GENERATED FILE — do not edit.
  Source: <base>/AGENTS.base.md (shared conventions)
  Source: AGENTS.repo.md (this repo)
  Regenerate with: just agents
-->

# Agent conventions

Shared across all my repos. Repo-specific detail lives in the overlay below the
separator. The deeper reasoning behind each rule is in `docs/` — read a doc when
you need the *why* or an edge case, not by default.

## Plan on Linear

Linear is the source of truth for what is being worked on. The repo records how
the code works; Linear records why work is happening and what is left.

- **File, ship, close.** Every non-trivial change has an issue. Move it to
  In Progress when you start and close it when the change ships.
- **Tier the tracking to the size.** A one-line fix needs no issue. A change
  worth reviewing needs an issue. A body of work spanning several changes needs
  a project with a dependency graph.
- **Parents before subs.** Create the parent issue first, then decompose.
- **Close on ship, in the same turn.** An issue left open after its change ships
  makes the whole board untrustworthy. The exception is an explicit "leave it,
  I want to look first".
- **Reference the issue in the commit message** so history and Linear stay linked.
- **Do not invent issues** to look busy, and do not silently work off-issue.

See `docs/planning-on-linear.md`.

## Plan strategically before decomposing

Work descends from an objective, not from whatever is nearest to hand.

1. Start from the **initiative** — the goal in one sentence.
2. Decompose into **projects** with explicit acceptance criteria.
3. Map the **critical path to the objective** and the **path to the target
   state** independently. They are not the same path.
4. Classify every body of work as one of:
   - **strategic-critical** — on the critical path; do it now.
   - **drift-guard** — not on the path, but deferring it causes rework; do it now.
   - **target-state-deferrable** — improves the end state, costs nothing to
     defer; file it and move on.

The rule: no shortsighted shortcuts, and no gold-plating off the critical path.

See `docs/planning-strategically.md`.

## Stay on target

When a fix or a discovery spawns work that is not on the current critical path:
**file it as a Linear issue and continue.** Do not rabbit-hole, and do not
silently expand scope.

- Distinguish must-do-now (on the path, or a drift-guard) from file-for-later.
- Filing is not dropping — Linear is durable, so deferring loses nothing.
- If the new work invalidates the current plan, say so and stop; do not
  improvise a new plan mid-change.

See `docs/staying-on-target.md`.

## Protect the main context

Delegate to a sub-agent whatever produces bulk you do not need to keep: broad
searches, multi-file reads, research spikes, large tool output.

- The sub-agent returns **the conclusion**, not the raw material.
- The main thread keeps the decision and the Linear update, never the dumps.
- State the return contract in the sub-agent's prompt (what shape, what fields).
- Sub-agent context loss is harmless because Linear holds the durable record.

See `docs/subagents.md`.

## Keep modules deep

A small, narrow interface with as much depth behind it as the domain needs —
for source and docs alike. One unit per domain; one topic per doc, standing
alone rather than assembled from fragments across five files.

See `docs/module-organisation.md`.

## Repository layout

Group by language first, then by package — `python/<package>/src/`,
`rust/<crate>/src/`, `go/<module>/` — each following that ecosystem's own
idiomatic internal layout. A bare `src/` at the repo root does not survive a
second language: it either gets overloaded or a later addition invents an
inconsistent exception. Decide the top-level split even in a repo that is
single-language today, if a second language is plausible later.

See `docs/repository-layout.md`.

## Keep docs honest

- **Docs alongside code, in the same commit.** A change that invalidates a doc
  updates that doc in the same commit — never as a follow-up.
- **Write back what you learn.** A non-obvious fact discovered while working
  (an invariant, a gotcha, why an approach failed) goes into the relevant doc.
- Every `docs/` tree has an index; every substantial directory has a README.

See `docs/doc-discipline.md`.

## Code and comments

- Comments explain **why**, not what. No comment narrates a change, references a
  conversation, or notes that something was removed.
- Keep comments general to the code, not to one deployment — describe what the
  code does, not the author's hosts or environment.
- Match the surrounding code's naming, idiom, and comment density.
- Destructure imports where the language supports it
  (`import { foo } from 'bar'`).

## Commits

- Logical, atomic, rebase-able, and succinct. One concern per commit.
- Strip trailing whitespace before staging.
- No references to the tooling or model that produced the change.
- Reference the Linear issue.
- **Never assume git state.** Check `git status` / `git log` before acting —
  do not assume the user has already committed, pushed, or pulled.
- Stage files by name, not by glob.

## Handing off

Work that outlives a session goes in an untracked `<TOPIC>-HANDOFF.md` written
for a reader with no prior context — settled decisions, what is verified (with
SHAs), what remains, and what must not be touched. Never secret values.

See `docs/session-handoff.md`.

## Memory

Record what is durable and not derivable from the repo: user preferences,
guidance you were given and why, project constraints, external references.
Do not record what the code, git history, or this file already says.

See `docs/memory.md`.

---

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
