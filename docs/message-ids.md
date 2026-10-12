# Message references: `corpus.id/v1`

Every tool that acts on a message takes a reference in the format corpus
publishes in its index rows. The canonical spec is corpus's
[`docs/message-ids.md`](https://github.com/ryanjdillon/corpus/blob/main/docs/message-ids.md).
This service has no dependency on corpus: `ids.py` implements the grammar
below independently.

```
<kind>:<mailbox>::<provider-uid>
```

The whole string must match. Leading or trailing whitespace, including a
trailing newline, makes it invalid.

| Part | Grammar |
| --- | --- |
| `kind` | `[a-z0-9_-]{1,32}`; this service implements `gmail`, `imap`, `jmap` and `graph` |
| `mailbox` | `[a-z0-9_-]{1,32}` (no `.`, no uppercase), the configured mailbox name; credentials are looked up in this service's own config, never carried in the ref |
| `gmail` uid | the Gmail API message id, `[0-9a-f]{8,32}` (lowercase) |
| `imap` uid | `<folder>:<uidvalidity>:<uid>` (see below) |
| `jmap` uid | the JMAP Email id, `[A-Za-z0-9_-]{1,255}` |
| `graph` uid | the Graph immutable id, `[A-Za-z0-9_=+/-]{1,512}` |

The kind ends at the first `:`, and the first `::` after `<kind>:` ends the
mailbox. Everything after that `::` is the uid.

An `imap` uid's last two `:`-separated parts are the uidvalidity and the uid.
Both are canonical positive integers, `[1-9][0-9]*`, so a leading zero or a
`0` is invalid. Everything before them is the folder. A folder is any non-empty
text without control characters, and it may contain spaces, `:` and `::`. So
`imap:x::A::B:1:7` has folder `A::B`, and `imap:kbl::::1:7` has folder `:`.

`str(parse_ref(ref)) == ref` for every valid ref. The whole ref is at most
1024 characters. A well-formed kind that is not implemented raises
`UnknownKind`, a subclass of `InvalidRef`, so callers can tell it apart from a
string that is not a reference at all. Both are refused with `invalid_ref`.
Nothing is repaired or normalised.

## Stability

`gmail`, `jmap` and `graph` refs survive label changes and moves. An `imap` ref
embeds its folder, so a move changes it. `file` and `unfile` return the new
ref in `new_ref` when this happens. Callers must use that value for later
calls on the message.

A message's `uri` (a human link) is not a reference and is never parsed; the
vectors include one as an invalid id.

## Vectors

`python/mail_actions/tests/vectors/message_ids.json` holds synthetic example
refs. They are maintained here, independently of corpus's own vectors, and
checked against the canonical spec linked above. `tests/test_ids.py` pins the
codec to every valid id (its parts, the IMAP folder, uidvalidity and uid, and
the exact round trip) and every invalid one. When the spec changes, update the
vectors here by hand; a grammar change is a new contract version.

```json
{"version": "corpus.id/v1",
 "valid":   [{"id": "...", "kind": "...", "mailbox": "...", "uid": "...",
              "imap": {"folder": "...", "uidvalidity": 1, "uid": 7}}],
 "invalid": [{"id": "...", "why": "..."}]}
```
