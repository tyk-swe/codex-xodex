# Deployment

Use a dedicated, non-production Linux VPS and unprivileged service account.
Required: Python 3.11+, Git with `--attr-source` support, rootless Podman, cgroup v2,
subordinate UID/GID mappings, user systemd and space for retained tasks/evidence.
The application probes Git support and refuses root execution.

On Debian/Ubuntu-family hosts, provision packages such as `python3-venv git podman
uidmap slirp4netns fuse-overlayfs`. Verify `/etc/subuid` and `/etc/subgid`, then log
in as the service user so its user manager and `XDG_RUNTIME_DIR` exist. An
administrator enables persistence after logout once:

```bash
sudo loginctl enable-linger xodex
```

Replace `xodex` with the actual service account. Run everything below as that
user, not through sudo.

## 1. Install and configure

Extract the source archive, enter its root and run:

```bash
./scripts/install-user.sh
```

The installer creates `~/.xodex/venv`, installs three user services and adds
`~/.local/bin/xodex`. It copies examples only when absent and **does not start
services, reset state or publish anything**. Python installation needs package
index access. Initial application/storage versions are `0.1.0`/`1`; use an empty
state directory rather than importing earlier development snapshots.

Edit `~/.xodex/config.toml`. Its example allows `tyk-swe/pcr`, uses
`tyk/xodex-<UUID>` branches and requires Rust formatting, Clippy and test checks.
Replace/add exact lowercase `owner/repo` tables; attachment cannot expand the
allowlist. For a disposable acceptance repository:

```toml
[repositories."your-owner/xodex-fixture"]
checks = ["python3 test_calc.py"]
network = "none"
```

Replace the table name with a real repository you are authorized to use. Never
add credentials, browser cookies or arbitrary clone URLs to task arguments.

## 2. Provision GitHub access

Use a fine-grained token limited to the allowed repositories, with contents and
pull-request read/write permissions. Organization approval/SSO and workflow-file
permissions may also apply. Review GitHub's [token guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
and [pull-request API](https://docs.github.com/en/rest/pulls/pulls); do not broaden
permissions automatically when a request fails.

Provision the token without putting its value in arguments or chat:

```bash
install -d -m 700 ~/.xodex/secrets
# Bash; do not enable shell tracing.
read -r -s -p 'GitHub token: ' TOKEN; printf '\n'
(umask 077; printf '%s\n' "$TOKEN" > ~/.xodex/secrets/github-token)
unset TOKEN
chmod 600 ~/.xodex/secrets/github-token
```

The file must be regular, owner-only, owned by the service user and not a symlink.
The supervisor loads it into memory; workers never receive it. Restart the engine
after rotation. The GitHub integration targets GitHub.com HTTPS and REST version
`2022-11-28`, not Enterprise origins, SSH remotes or fork-based publication.

## 3. Build the worker

```bash
./scripts/build-worker.sh
# For a repository-pinned Rust toolchain:
RUST_TOOLCHAIN='<repository-pinned-version>' ./scripts/build-worker.sh
```

The default tag is `localhost/chatgpt-xodex-worker:0.1.0`. Use a reviewed
`BASE_IMAGE` digest for repeatable builds and retain the resulting image digest.
The engine resolves the tag at startup; rebuild/restart when tooling changes.

The image supplies Bash, Git, ripgrep, Python, Node/npm, Rust tooling, compilers
and libpcap headers. Extra languages, pnpm, browser runtimes, libraries or services
need an owner-maintained image. Rust toolchains must be installed at build time:
`RUSTUP_HOME` is read-only at runtime; `CARGO_HOME` is task-local and writable.

Workers are **offline by default**. Seed dependencies/caches or explicitly enable
outbound access in that repository's policy:

```toml
network = "slirp4netns:allow_host_loopback=false"
```

This is broad outbound access, not a domain allowlist; apply appropriate egress
controls. It never authorizes mounting host credentials, agents or runtime sockets.
See [security](SECURITY.md).

## 4. Start and probe local services

```bash
~/.local/bin/xodex doctor --probe
systemctl --user enable --now xodex-engine.service xodex-mcp.service
~/.xodex/venv/bin/python scripts/smoke-mcp.py
journalctl --user -u xodex-engine.service -u xodex-mcp.service -f
```

The doctor runs a disposable container to check non-root identity, dropped
capabilities, no-new-privileges, read-only rootfs, cgroup limits, absent secret
variables and a writable task mount. The smoke client negotiates MCP, reads the
catalog/health and lists tasks. It creates no task, branch, commit or PR.

Run the real Podman tests on this host as described in
[development](DEVELOPMENT.md). Local fake-backend tests are not a substitute.

## 5. Connect the private tunnel and ChatGPT app

Install a verified [official tunnel-client](https://github.com/openai/tunnel-client)
release bundle in `~/.local/opt/openai-tunnel/`, preserving its runtime files.
Authorize a tunnel for the OpenAI context that will use the app. Put its ID in
`~/.xodex/tunnel-client.yaml` and its **runtime key** in `~/.xodex/tunnel.env`.
This is not an organization admin key or a ChatGPT session token.

The example routes channel `main` to `http://localhost/mcp` through the private
Unix socket supplied as `MCP_UNIX_SOCKET_PATH`. Neither Python service listens on
public TCP. Check the installed client's [configuration contract](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md)
before using the example; its upstream `config_version: 1` is not an Xodex version.

```bash
chmod 600 ~/.xodex/tunnel.env ~/.xodex/tunnel-client.yaml
systemctl --user enable --now xodex-tunnel.service
journalctl --user -u xodex-tunnel.service -f
```

Follow the official [developer-mode guide](https://developers.openai.com/api/docs/guides/developer-mode)
and [account/client guidance](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
to create **chatgpt-xodex**, choose the authorized tunnel and inspect its 17 tools.
Verify account eligibility, client availability and write-tool confirmations;
a paid plan or server capability alone is not evidence of access. UI labels and
availability can change. Select the app in the conversation; refresh its catalog
when tool definitions change. Xodex does not bypass confirmations or provide an
integration with the separately branded agent mode.

## 6. Accept the deployment before real work

Use an owner-approved disposable GitHub repository with a deliberately incorrect
`calc.py` addition function and a failing `test_calc.py`. Allow that repository
and require `python3 test_calc.py`. Ask ChatGPT:

> Use chatgpt-xodex on owner/xodex-fixture. Reproduce the addition bug, fix it,
> validate the regression and open a PR. Handle execution yourself.

Verify the failing test first, then the passing check, intended PR diff and
reported commit. Repeat the exact finalization request and confirm no duplicate
PR. Stop a sleep command and confirm execution ends but files remain. Restart the
gateway/tunnel during a command and check retained output. Restart the supervisor
and confirm interruption—not success or silent replay. Check services after
logout/reboot.

These are required live checks, not claims that this source archive was tested
against your host, credentials, GitHub account or ChatGPT connection.
