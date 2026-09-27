# chatgpt-xodex

**Give ChatGPT a repository and a task. Get back a pull request.**

Xodex is a single-owner MCP executor for a Linux VPS. ChatGPT makes the coding
decisions; Xodex handles repository setup, isolated commands, retained files,
validation and publication. It does not run another coding agent or inference loop.

```text
Attach repository → implement → validate → open pull request
```

Initial release: **0.1.0** · SQLite schema: **1**. There are no migrations or
backward-compatibility promises for earlier development snapshots.

## Use it

On a provisioned Linux VPS, run `./scripts/install-user.sh` for guided setup.
For bootstrap without prompts, add `--install-only`, then run `xodex setup` in a
terminal. Setup configures repository policies, validates the sandbox and offers
service startup with a default of no. See [deployment](docs/DEPLOYMENT.md) for host
prerequisites, credentials, the external tunnel client and live acceptance.

After deployment, select the app and describe the work:

> Use chatgpt-xodex on your-owner/your-repo. Reproduce this bug, fix it, add a regression
> test, validate the change, and open a PR. Handle setup and execution yourself.
>
> [Issue and acceptance criteria.]

Ask for progress, stop, or continuation in plain language. ChatGPT manages task
and command IDs. `finish_task` runs the checks and publishes the PR; it does not
hand the Git work back to you.

## What it guarantees—and what it does not

Commands run in rootless Podman with per-task mounts and owner-defined limits.
Repositories must be explicitly allowed. Publication credentials and authoritative
Git objects stay outside workers. Required checks run before publication, and a
changed candidate tree blocks the PR. Unknown publication outcomes are reconciled
rather than blindly repeated.

Task files and evidence are retained until explicit owner administration. There
is no automatic eviction and no host-shell fallback. Workers are offline by
default; arbitrary repositories need suitable toolchains and dependency caches
or an explicitly enabled network policy.

Admitted commands and finalization survive a disconnected client, **not a
supervisor restart**. Xodex cannot continue model reasoning after ChatGPT stops
calling tools. Account eligibility, tool confirmations and live tunnel support
must be checked on your deployment. Never expose the private sockets through an
unauthenticated public proxy.

## Documentation

| Guide | Contents |
|---|---|
| [Deployment](docs/DEPLOYMENT.md) | Host, credentials, workers, tunnel and acceptance. |
| [Architecture](docs/ARCHITECTURE.md) | Ownership, storage, lifecycle and publication. |
| [Protocol](docs/PROTOCOL.md) | MCP tools, retries, commands and file edits. |
| [Security](docs/SECURITY.md) | Trust boundaries and limitations. |
| [Operations](docs/OPERATIONS.md) | Recovery, backups and explicit deletion. |
| [Development](docs/DEVELOPMENT.md) | Tests, packaging and live verification gates. |

## Develop

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q -W error
python -m pip wheel --no-deps . -w dist
python scripts/release.py
```

The release command builds `dist/chatgpt-xodex-v0.1.0.zip` with deterministic
metadata, executable scripts and a generated `SHA256SUMS`. Verification logs and
build products belong outside source documentation; see the development guide.

MIT licensed. Unofficial; not affiliated with OpenAI or Cognition.
