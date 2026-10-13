"""``mail-actions``: serve the MCP tools, run the one-time consent, or check setup."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import click
import httpx
from mcp.server.fastmcp import FastMCP
from opentelemetry.sdk.metrics import MeterProvider

from .actions import MailboxActions
from .config import DEFAULT_CONFIG, Config, ConfigError, Mailbox, ProtectedFile, load_config
from .gmail import SCOPES, Credentials, GmailAdapter, gmail_client, verify_credentials
from .provider import Provider, ProviderError
from .telemetry import configure


def open_provider(
    mailbox: Mailbox,
    config: Config,
    env: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> Provider:
    """Build and prepare the adapter for *mailbox*."""
    if mailbox.kind != "gmail":
        raise ConfigError(f"mailbox {mailbox.name}: no {mailbox.kind} adapter yet")
    creds = Credentials.from_env(mailbox.credentials_env_prefix, env)
    adapter = GmailAdapter(gmail_client(creds, transport=transport))
    adapter.prepare(config.tags)
    return adapter


def check_credentials(
    mailbox: Mailbox, env: Mapping[str, str], *, transport: httpx.BaseTransport | None = None
) -> frozenset[str]:
    """Mint a token for *mailbox* and return its granted scopes; reads no mail."""
    if mailbox.kind != "gmail":
        raise ConfigError(f"mailbox {mailbox.name}: no {mailbox.kind} adapter yet")
    creds = Credentials.from_env(mailbox.credentials_env_prefix, env)
    with gmail_client(creds, transport=transport) as client:
        return verify_credentials(client)


def run_server(mcp: FastMCP) -> None:
    mcp.run(transport="streamable-http")


def consent_flow(client_secret: str, scopes: Sequence[str]) -> Any:
    """Run the installed-app OAuth flow in a local browser; returns the credentials."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret, list(scopes))
    return flow.run_local_server(port=0)


@dataclass
class Wiring:
    """The I/O each command performs, injectable for tests."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    open_provider: Callable[[Mailbox, Config, Mapping[str, str]], Provider] = open_provider
    check_credentials: Callable[[Mailbox, Mapping[str, str]], frozenset[str]] = check_credentials
    run_server: Callable[[FastMCP], None] = run_server
    consent_flow: Callable[[str, Sequence[str]], Any] = consent_flow
    configure_telemetry: Callable[[Mapping[str, str]], MeterProvider | None] = configure


@click.group()
@click.option(
    "--config",
    "config_path",
    envvar="MAIL_ACTIONS_CONFIG",
    default=DEFAULT_CONFIG,
    show_default=True,
    help="Path to the service config.",
)
@click.pass_context
def main(ctx: click.Context, config_path: str) -> None:
    """Narrow, provider-neutral mailbox actions over MCP."""
    ctx.ensure_object(Wiring)
    ctx.meta["config_path"] = config_path


def _config(ctx: click.Context) -> Config:
    try:
        return load_config(ctx.meta["config_path"], ctx.obj.env)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--host", envvar="MAIL_ACTIONS_HOST", default="0.0.0.0", show_default=True)
@click.option("--port", envvar="MAIL_ACTIONS_PORT", default=9000, show_default=True, type=int)
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> None:
    """Serve the MCP tools over streamable HTTP at /mcp."""
    from .server import build_server

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    wiring: Wiring = ctx.obj
    config = _config(ctx)
    try:
        providers = {m.name: wiring.open_provider(m, config, wiring.env) for m in config.mailboxes}
    except (ConfigError, ProviderError) as exc:
        raise click.ClickException(f"cannot open mailboxes: {exc}") from exc
    # First, so every call from ensure_protected on is exported.
    meter_provider = wiring.configure_telemetry(wiring.env)
    try:
        actions = MailboxActions(config, providers, protected=ProtectedFile(config.protected_path))
        actions.ensure_protected()
        wiring.run_server(build_server(actions, host=host, port=port))
    finally:
        if meter_provider is not None:
            meter_provider.shutdown()


@main.command()
@click.argument("mailbox")
@click.option(
    "--client-secret",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="OAuth Desktop-app client JSON from the Google Cloud project.",
)
@click.option("--prefix", help="Credential env prefix [default: MAIL_ACTIONS_GMAIL_<MAILBOX>].")
@click.option(
    "--out",
    type=click.Path(dir_okay=False),
    help="Env file to create (mode 0600, never overwritten) "
    "[default: ./mail-actions-<MAILBOX>.env].",
)
@click.pass_context
def consent(
    ctx: click.Context, mailbox: str, client_secret: str, prefix: str | None, out: str | None
) -> None:
    """One-time OAuth consent for a Gmail MAILBOX; writes the credentials to an
    env file.

    Run on a workstation with a browser, never in the cluster. The token is
    scoped to gmail.modify and gmail.settings.basic and must come from its own
    consent, separate from any read-only ingest token. The credentials are
    written only to the file, never printed.
    """
    out = out or f"mail-actions-{mailbox}.env"
    # Checked before the browser flow too, so a consent is not wasted.
    if os.path.lexists(out):
        raise click.ClickException(f"{out} already exists; refusing to overwrite it")
    try:
        creds = ctx.obj.consent_flow(client_secret, SCOPES)
    except ImportError as exc:
        raise click.ClickException(
            "install the consent extra first: pip install 'mail-actions[consent]'"
        ) from exc
    prefix = prefix or f"MAIL_ACTIONS_GMAIL_{mailbox}".upper()
    values = {
        f"{prefix}_CLIENT_ID": creds.client_id,
        f"{prefix}_CLIENT_SECRET": creds.client_secret,
        f"{prefix}_REFRESH_TOKEN": creds.refresh_token,
    }
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise click.ClickException(f"{out} already exists; refusing to overwrite it") from exc
    with os.fdopen(fd, "w") as fh:
        fh.writelines(f"{name}={value}\n" for name, value in values.items())
    click.echo(f"wrote {out} (mode 0600) with {', '.join(values)}")
    click.echo("store them as the mailbox credentials (a Secret, never in git), then delete it")


@main.command()
@click.pass_context
def check(ctx: click.Context) -> None:
    """Validate the config, the protected list and each mailbox's credentials.

    Mints an access token per mailbox to prove the credentials and their scopes;
    reads no mail. A missing or empty protected list is a warning (archiving
    stays off until it arrives); an invalid one is an error.
    """
    wiring: Wiring = ctx.obj
    config = _config(ctx)
    errors = 0
    click.echo(f"config: ok ({len(config.mailboxes)} mailbox(es), {len(config.groups)} groups)")

    try:
        protected = ProtectedFile(config.protected_path).current()
    except ConfigError as exc:
        click.echo(f"protected list: ERROR {exc}")
        errors += 1
    else:
        if protected.empty:
            click.echo("protected list: WARNING missing or empty; file will not archive")
        else:
            counts = ", ".join(
                f"{c}={len(getattr(protected, c))}" for c in ("family", "tax", "legal")
            )
            click.echo(f"protected list: ok ({counts})")

    for mailbox in config.mailboxes:
        try:
            scopes = wiring.check_credentials(mailbox, wiring.env)
        except (ConfigError, ProviderError) as exc:
            click.echo(f"mailbox {mailbox.name}: ERROR {exc}")
            errors += 1
            continue
        missing = sorted(set(SCOPES) - scopes)
        if missing:
            click.echo(f"mailbox {mailbox.name}: ERROR token lacks {', '.join(missing)}")
            errors += 1
        else:
            extra = sorted(scopes - set(SCOPES))
            note = f" (WARNING broader than needed: {', '.join(extra)})" if extra else ""
            click.echo(f"mailbox {mailbox.name}: ok{note}")
    if errors:
        sys.exit(1)
