import json
import stat
from types import SimpleNamespace
from unittest.mock import create_autospec

import httpx
import pytest
from click.testing import CliRunner
from opentelemetry.sdk.metrics import MeterProvider

from mail_actions import cli
from mail_actions.cli import Wiring, main
from mail_actions.config import ConfigError, Mailbox, parse_config
from mail_actions.gmail import API, SCOPES, TOKEN_URL, GmailAdapter
from mail_actions.provider import Provider, ProviderError

CONFIG = """\
mailboxes:
  - name: personal
    kind: gmail
groups: [Banking]
"""


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG + f"protected_path: {tmp_path / 'protected.yaml'}\n")
    return path


@pytest.fixture
def protected_path(config_path):
    return config_path.parent / "protected.yaml"


@pytest.fixture
def provider():
    m = create_autospec(Provider, instance=True)
    m.kind = "gmail"
    m.get_rules.return_value = []
    return m


@pytest.fixture
def open_provider(provider):
    return create_autospec(cli.open_provider, return_value=provider)


@pytest.fixture
def check_credentials():
    return create_autospec(cli.check_credentials, return_value=frozenset(SCOPES))


@pytest.fixture
def run_server():
    return create_autospec(cli.run_server)


@pytest.fixture
def consent_flow():
    return create_autospec(cli.consent_flow)


@pytest.fixture
def configure_telemetry():
    return create_autospec(cli.configure, return_value=None)


@pytest.fixture
def wiring(open_provider, check_credentials, run_server, consent_flow, configure_telemetry):
    return Wiring(
        env={},
        open_provider=open_provider,
        check_credentials=check_credentials,
        run_server=run_server,
        consent_flow=consent_flow,
        configure_telemetry=configure_telemetry,
    )


def invoke(wiring, *args):
    return CliRunner().invoke(main, list(args), obj=wiring, catch_exceptions=False)


def test_serve_opens_mailboxes_converges_and_runs(
    wiring, config_path, protected_path, open_provider, provider, run_server
):
    protected_path.write_text("family: [mom@example.org]\n")

    result = invoke(wiring, "--config", str(config_path), "serve", "--port", "9100")

    assert result.exit_code == 0, result.output
    assert open_provider.call_args.args[0].name == "personal"
    provider.create_rule.assert_called_once()  # the protected rule
    server = run_server.call_args.args[0]
    assert (server.name, server.settings.port) == ("mail-actions", 9100)


def test_serve_exports_metrics_and_flushes_them_on_exit(
    wiring, config_path, configure_telemetry, run_server
):
    meter_provider = create_autospec(MeterProvider, instance=True)
    configure_telemetry.return_value = meter_provider
    run_server.side_effect = KeyboardInterrupt

    result = CliRunner().invoke(main, ["--config", str(config_path), "serve"], obj=wiring)

    assert result.exit_code == 1
    configure_telemetry.assert_called_once_with(wiring.env)
    meter_provider.shutdown.assert_called_once_with()


def test_serve_fails_cleanly_when_a_mailbox_cannot_open(
    wiring, config_path, open_provider, run_server
):
    open_provider.side_effect = ProviderError("token:invalid_grant")

    result = CliRunner().invoke(main, ["--config", str(config_path), "serve"], obj=wiring)

    assert result.exit_code == 1
    assert "token:invalid_grant" in result.output
    run_server.assert_not_called()


def test_serve_reports_a_bad_config(wiring, tmp_path):
    result = CliRunner().invoke(
        main, ["--config", str(tmp_path / "none.yaml"), "serve"], obj=wiring
    )

    assert result.exit_code == 1
    assert "cannot read config" in result.output


def test_check_warns_on_a_missing_protected_list(wiring, config_path):
    result = invoke(wiring, "--config", str(config_path), "check")

    assert result.exit_code == 0
    assert "protected list: WARNING missing or empty" in result.output
    assert "mailbox personal: ok" in result.output


def test_check_counts_the_protected_list(wiring, config_path, protected_path):
    protected_path.write_text("family: [mom@example.org]\ntax: [tax.example.net]\n")

    result = invoke(wiring, "--config", str(config_path), "check")

    assert "protected list: ok (family=1, tax=1, legal=0)" in result.output


def test_check_fails_on_an_invalid_protected_list(wiring, config_path, protected_path):
    protected_path.write_text("friends: []\n")

    result = invoke(wiring, "--config", str(config_path), "check")

    assert result.exit_code == 1
    assert "protected list: ERROR" in result.output


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (ConfigError("missing credentials: X_REFRESH_TOKEN"), "ERROR missing credentials"),
        (ProviderError("token:invalid_grant"), "ERROR token:invalid_grant"),
        (frozenset({SCOPES[0]}), "ERROR token lacks"),
    ],
)
def test_check_fails_on_bad_credentials(wiring, config_path, check_credentials, outcome, message):
    if isinstance(outcome, Exception):
        check_credentials.side_effect = outcome
    else:
        check_credentials.return_value = outcome

    result = invoke(wiring, "--config", str(config_path), "check")

    assert result.exit_code == 1
    assert message in result.output


def test_check_warns_on_an_overbroad_token(wiring, config_path, check_credentials):
    check_credentials.return_value = frozenset(SCOPES) | {"https://mail.google.com/"}

    result = invoke(wiring, "--config", str(config_path), "check")

    assert result.exit_code == 0
    assert "WARNING broader than needed: https://mail.google.com/" in result.output


@pytest.fixture
def client_secret(tmp_path):
    secret = tmp_path / "client.json"
    secret.write_text("{}")
    return secret


@pytest.fixture
def granted(consent_flow):
    consent_flow.return_value = SimpleNamespace(
        client_id="cid", client_secret="cs", refresh_token="rt-not-for-stdout"
    )
    return consent_flow


def test_consent_writes_a_0600_env_file_and_never_prints_the_token(
    wiring, tmp_path, client_secret, granted
):
    out = tmp_path / "personal.env"

    result = invoke(
        wiring, "consent", "personal", "--client-secret", str(client_secret), "--out", str(out)
    )

    granted.assert_called_once_with(str(client_secret), SCOPES)
    assert "rt-not-for-stdout" not in result.output
    assert "cs" not in result.output.split()
    assert str(out) in result.output
    assert "MAIL_ACTIONS_GMAIL_PERSONAL_REFRESH_TOKEN" in result.output
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.read_text() == (
        "MAIL_ACTIONS_GMAIL_PERSONAL_CLIENT_ID=cid\n"
        "MAIL_ACTIONS_GMAIL_PERSONAL_CLIENT_SECRET=cs\n"
        "MAIL_ACTIONS_GMAIL_PERSONAL_REFRESH_TOKEN=rt-not-for-stdout\n"
    )


def test_consent_defaults_to_a_file_named_for_the_mailbox(
    wiring, tmp_path, client_secret, granted, monkeypatch
):
    monkeypatch.chdir(tmp_path)  # the working directory, not a collaborator

    result = invoke(wiring, "consent", "personal", "--client-secret", str(client_secret))

    assert result.exit_code == 0, result.output
    assert (tmp_path / "mail-actions-personal.env").exists()


def test_consent_refuses_to_overwrite_before_the_browser_flow(
    wiring, tmp_path, client_secret, granted
):
    out = tmp_path / "personal.env"
    out.write_text("keep me\n")

    result = CliRunner().invoke(
        main,
        ["consent", "personal", "--client-secret", str(client_secret), "--out", str(out)],
        obj=wiring,
    )

    assert result.exit_code == 1
    assert "refusing to overwrite" in result.output
    granted.assert_not_called()
    assert out.read_text() == "keep me\n"


def test_consent_refuses_a_file_that_appears_during_the_flow(
    wiring, tmp_path, client_secret, granted
):
    out = tmp_path / "personal.env"

    def appear(*_):
        out.write_text("raced\n")
        return SimpleNamespace(client_id="cid", client_secret="cs", refresh_token="rt")

    granted.side_effect = appear

    result = CliRunner().invoke(
        main,
        ["consent", "personal", "--client-secret", str(client_secret), "--out", str(out)],
        obj=wiring,
    )

    assert result.exit_code == 1
    assert out.read_text() == "raced\n"


def test_consent_explains_a_missing_extra(wiring, tmp_path, consent_flow):
    secret = tmp_path / "client.json"
    secret.write_text("{}")
    consent_flow.side_effect = ImportError

    result = CliRunner().invoke(
        main, ["consent", "personal", "--client-secret", str(secret)], obj=wiring
    )

    assert result.exit_code == 1
    assert "mail-actions[consent]" in result.output


@pytest.mark.parametrize("command", [cli.open_provider, cli.check_credentials])
def test_unsupported_kinds_are_config_errors(command):
    mailbox = Mailbox("club", "imap", "X")

    with pytest.raises(ConfigError, match="no imap adapter yet"):
        command(*([mailbox, None, {}] if command is cli.open_provider else [mailbox, {}]))


ENV = {
    "MAIL_ACTIONS_GMAIL_PERSONAL_CLIENT_ID": "cid",
    "MAIL_ACTIONS_GMAIL_PERSONAL_CLIENT_SECRET": "cs",
    "MAIL_ACTIONS_GMAIL_PERSONAL_REFRESH_TOKEN": "rt",
}
TOKEN = {"access_token": "at", "expires_in": 3599, "scope": " ".join(SCOPES)}


def gmail_transport(seen, labels):
    def handle(request):
        seen.append((request.method, str(request.url)))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=TOKEN)
        if request.method == "GET":
            return httpx.Response(200, json={"labels": labels})
        name = json.loads(request.content)["name"]
        return httpx.Response(200, json={"id": f"id-{name}", "name": name})

    return httpx.MockTransport(handle)


def test_open_provider_prepares_a_gmail_adapter():
    config = parse_config(
        {"mailboxes": [{"name": "personal", "kind": "gmail"}], "groups": ["Banking"]}, {}
    )
    existing = [{"id": f"L{i}", "name": n} for i, n in enumerate(sorted(config.tags))]
    seen = []

    adapter = cli.open_provider(
        config.mailboxes[0], config, ENV, transport=gmail_transport(seen, existing)
    )

    assert isinstance(adapter, GmailAdapter)
    assert seen == [("POST", TOKEN_URL), ("GET", f"{API}/labels")]


def test_check_credentials_mints_a_token_only():
    seen = []

    scopes = cli.check_credentials(
        Mailbox("personal", "gmail", "MAIL_ACTIONS_GMAIL_PERSONAL"),
        ENV,
        transport=gmail_transport(seen, []),
    )

    assert scopes == set(SCOPES)
    assert seen == [("POST", TOKEN_URL)]
