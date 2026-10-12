import 'agents.just'

# mail-actions dev tasks. Enter the dev shell first: `nix develop`.

pkg := "python/mail_actions"

# Create the uv venv and install the package with its test + dev deps, exactly
# as pinned in uv.lock. After changing dependencies, run `uv lock` and commit it.
setup:
    cd {{pkg}} && uv sync --frozen --python python3.12 --all-extras

# Unit tests.
test:
    cd {{pkg}} && uv run pytest

# Lint and format check (the pinned ruff from the venv, matching CI).
lint:
    cd {{pkg}} && uv run ruff check . && uv run ruff format --check .

# Tests with coverage, gated by the ratchet in .github/coverage-baseline.txt.
cov:
    cd {{pkg}} && uv run coverage run --source=mail_actions -m pytest
    cd {{pkg}} && uv run coverage report --precision=2
    python3 scripts/coverage_gate.py "$(cd {{pkg}} && uv run coverage report --format=total --precision=2)" .github/coverage-baseline.txt

# Lint this branch's commit messages against Conventional Commits, mirroring the
# commitlint CI job. Checks commits not yet on origin/main.
commitlint BASE="origin/main":
    commitlint --from {{BASE}} --to HEAD

# All pre-PR checks, mirroring CI. Run this (and fix any failures) before pushing.
check: lint cov commitlint

# One-time OAuth consent for a mailbox; writes the credentials to a 0600 env
# file (never printed, never overwritten).
# Usage: just consent personal path/to/client_secret.json [path/to/out.env]
consent MAILBOX CLIENT_SECRET OUT=("mail-actions-" + MAILBOX + ".env"):
    uv run --directory {{pkg}} mail-actions consent {{MAILBOX}} --client-secret "$(realpath {{CLIENT_SECRET}})" --out "$(realpath -m {{OUT}})"
