# Gmail adapter

`GmailAdapter` (`gmail.py`) implements the `Provider` port over the Gmail REST
API with `httpx`. It enforces no policy; it only translates.

| Port | Gmail |
| --- | --- |
| tags | labels; names resolved to ids once at startup (`GET /labels`), missing ones created (`POST /labels`) |
| `get_tags` | `GET /messages/{id}?format=minimal&fields=labelIds`, the only read of a message |
| `add_tag` / `remove_tag` | `POST /messages/{id}/modify` with `addLabelIds` / `removeLabelIds` |
| `archive` / `unarchive` | `modify` removing / adding `INBOX`; the ref does not change |
| `tag_matching` | `GET /messages?q=from:(<entry>)&fields=messages/id,nextPageToken` (paginated), then `POST /messages/batchModify` with `{ids, addLabelIds: [tag]}` in chunks of at most 1000 ids |
| rules | `GET/POST/DELETE /settings/filters` |

The `fields` mask matters: `format=minimal` alone still returns the snippet.
With `fields=labelIds` the response holds only label ids, so no content enters
the process. `modify` responses are masked to `fields=id` for the same reason.

The adapter never sends, trashes or deletes mail. Its only search is
`tag_matching`, the protected backfill: it searches by sender, the `fields`
mask keeps the listing to message ids, and the ids go straight into
`batchModify`. Nothing else about the matched messages is read. A tag that
was not resolved at startup is refused (`unknown_tag`) without a request. The
`Auto/` prefix is a plain label name; Gmail shows it nested.

## Filters

| Rule | Filter criteria | Action |
| --- | --- | --- |
| `from_domain: bank.example` | `from: "@bank.example"` | `addLabelIds: [tag]` |
| `from_addr: a@b.example` | `from: "a@b.example"` | `addLabelIds: [tag]` |
| `list_id: news.example.com` | `query: "list:news.example.com"` | `addLabelIds: [tag]` |
| `archive: true` | | also `removeLabelIds: ["INBOX"]` |

Every filter the service creates sets `criteria.negatedQuery` to the plain
token `mailactionsmanaged`. Gmail's filter documentation warns that `label:`,
`in:` and `is:` operators do not work reliably in filters, so the marker is an
ordinary word. Excluding mail that contains it changes nothing in practice: no
real mail carries the word, and only the matched sender could add it. On an
archive rule that would merely keep the sender's mail in the inbox. On a
protected rule, only the protected sender or a forger of that address could
skip the label.

A filter is read as managed only if it has the marker and exactly a shape this
service writes: criteria of `from` (one address or `@domain`) or `query` (one
`list:` term) plus the marker, and an action that adds exactly one label the
service resolved at startup and at most removes `INBOX`. Anything else,
including a marked filter someone edited by hand, is unmanaged and never
changed or deleted. Gmail has no filter update, so an update creates the new
filter first and then deletes the old one, leaving no window without a filter.

### Live verification before tagging a release

Run this against a real mailbox before tagging v0.1.0, and again whenever the
filter shape changes:

1. Add a test address you control to the protected list (for example under
   `family`) and start the service, so it installs the protected filter.
2. In Gmail settings, confirm the filter shows "Doesn't have:
   mailactionsmanaged" and applies the `Family` label.
3. Send yourself a mail from that test address and confirm it arrives with the
   `Family` label applied.
4. Call `file` on that message and confirm it is refused with `protected`.
5. Remove the test address from the list afterwards.

Gmail filters apply only to mail that arrives after they are created. So after
installing the protected filters, `MailboxActions` calls `tag_matching` for
every protected entry to label the mail already there, and `file` archives only
once that pass has completed for the current list. See the threat model.

## Credentials and scopes

The token needs `https://www.googleapis.com/auth/gmail.modify` and
`https://www.googleapis.com/auth/gmail.settings.basic`. These cover label
create and modify, the minimal read, the backfill's id-only search, and
filters. It must come from its own
consent, separate from corpus's read-only ingest token:

```bash
pip install 'mail-actions[consent]'
mail-actions consent personal --client-secret client_secret.json
```

This writes `MAIL_ACTIONS_GMAIL_PERSONAL_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN}`
to `./mail-actions-personal.env` (or `--out PATH`), created with mode 0600 and
never overwritten. Only the path and the variable names are printed, so the
refresh token never reaches the terminal or its scrollback. Load the file into
a Secret, then delete it. Run it on a workstation and do not keep the client
JSON or the env file in the repo (`.gitignore` excludes `client_secret*.json`
and `mail-actions-*.env`).

Access tokens are minted from the refresh token on first use, refreshed 60 s
before expiry, and refreshed once on a 401. `mail-actions check` mints a token
and compares its granted scopes with the two above.

## Errors

A failed call raises `ProviderError` carrying Google's error reason (e.g.
`notFound`, `rateLimitExceeded`) when it is a short code matching
`[A-Za-z0-9_.-]{1,64}`, else the status code, so no response text is echoed. A network failure gives
`transport` and a refused refresh token gives `token:<error>`.
`MailboxActions` turns these into `ok: false, reason: "provider:<code>"`.
