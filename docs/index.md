# mail-actions documentation

| Page | Covers |
| --- | --- |
| [interface.md](interface.md) | the five MCP tools, their arguments, results and refusal reasons; config; the CLI |
| [threat-model.md](threat-model.md) | what an attacker who controls an email can and cannot do, and why |
| [message-ids.md](message-ids.md) | the `corpus.id/v1` reference format and its test vectors |
| [providers/gmail.md](providers/gmail.md) | how the Gmail adapter maps tags, archive and rules; scopes; consent |

Code layout: `python/mail_actions/src/mail_actions/`

- `actions.py`: `MailboxActions`, the one module that enforces rules, caps and
  the audit line.
- `provider.py`: the `Provider` port and its value types.
- `gmail.py`: the Gmail adapter and OAuth token handling.
- `ids.py`: the `corpus.id/v1` codec.
- `config.py`: the service config and the protected list.
- `server.py`: the FastMCP surface. `cli.py`: the `mail-actions` command.
