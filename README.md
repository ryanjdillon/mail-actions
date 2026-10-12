# mail-actions

A narrow, provider-neutral MCP service that files and labels mail without
reading it.

It is the only component allowed to change the mailbox. An agent passes a
message reference (`corpus.id/v1`, e.g. `gmail:personal::18c2f0a1b2c3d4e5`) and
an intent; the service checks it against every rule and applies it through a
provider adapter. No tool returns a subject, sender, body, header or snippet.

| Tool | Does |
| --- | --- |
| `file` | tag `linear` and archive; refused for protected mail |
| `unfile` | undo `file` |
| `label` | add an allow-listed `Auto/<group>` label |
| `sync_filters` | converge this service's server-side filters to an approved set |
| `capabilities` | what each mailbox supports, and whether archiving is on |

Gmail is the first provider; IMAP, JMAP and Microsoft Graph adapters fit the
same port.

```bash
mail-actions consent personal --client-secret client_secret.json  # once, locally
mail-actions check     # config, protected list, credentials; reads no mail
mail-actions serve     # MCP over streamable HTTP on :9000/mcp
```

Documentation: [docs/index.md](docs/index.md). Contributing: `nix develop`,
`just setup`, `just check`.

License: MPL-2.0.
