# Deployment

Use a dedicated, non-production Linux VPS and an unprivileged service account.
An administrator provisions Python 3.11+, Git with `--attr-source` support,
rootless Podman, cgroup v2, subordinate UID/GID mappings, user systemd and space
for retained tasks/evidence. Xodex does not install host packages or provision users.

On Debian/Ubuntu-family hosts, packages include `python3-venv git podman uidmap
slirp4netns fuse-overlayfs`. Verify `/etc/subuid` and `/etc/subgid`, then log in
as the service user so its user manager and `XDG_RUNTIME_DIR` exist. An
administrator enables persistence after logout once:

```bash
sudo loginctl enable-linger xodex
```

Replace `xodex` with the actual service account. Run the remaining steps as that
user, without sudo. Keep the private sockets off public TCP/proxies.

## Guided installation

Extract the source archive, enter its root, and run in a terminal:

```bash
./scripts/install-user.sh
```

Bootstrap creates `~/.xodex/venv` and `~/.local/bin/xodex`, installs the package,
and runs `xodex setup`. Package installation requires package-index access.
For unattended bootstrap, or to install before the VPS prerequisites are ready:

```bash
./scripts/install-user.sh --install-only
~/.local/bin/xodex setup
```

`--install-only` creates no configuration or service units and starts no services.
Direct noninteractive setup refuses to write files. A wheel installation also
supports the same setup flow; it does not need the source checkout or scripts.

Setup guides you through:

1. Exact allowed GitHub repositories, required check commands and network policy.
   There is no preselected repository. Workers default to `network = "none"` and
   task branches use `xodex/<UUID>`. Empty owner checks are allowed; finalization
   still requires the task to supply checks under the existing rules.
2. Host prerequisites and GitHub credentials. Select an existing private token
   file or enter a missing token through a masked prompt. Setup reads repository
   metadata to check identity and push access; this cannot prove PR permissions
   or branch rules without the live acceptance flow.
3. The worker image. An available configured image is reused. If the bundled
   default is missing, setup offers to build it. A missing custom image requires
   its owner's build/provisioning procedure.
4. The existing disposable sandbox probe, including non-root identity, dropped
   capabilities, no-new-privileges, read-only rootfs, cgroup limits, absent secret
   variables and a writable task mount.
5. Optional tunnel configuration, using a separately installed client and an
   already authorized tunnel. Missing tunnel prerequisites are reported separately;
   local setup can finish and local services can start.
6. User service files and `systemctl --user daemon-reload`. Units use the installed
   Python interpreter, selected configuration and actual socket paths. Enabling
   and starting services requires an explicit **yes**, with **no** as the default.
   After startup, setup runs the integrated read-only MCP smoke check.

Setup, doctor and smoke create no task, branch, commit or PR. Starting an existing
supervisor still invokes its normal recovery behavior for previously retained work.
Setup never issues a service restart. Use an empty state directory for this initial
storage schema (`1`); do not import unidentified development databases.

## Reruns and custom locations

```bash
xodex --config /home/xodex/project/config.toml setup
xodex --config /home/xodex/project/config.toml doctor --probe
xodex --config /home/xodex/project/config.toml smoke
```

Omitted paths are derived from the selected configuration file's directory:
`state/`, `tasks/`, `run/engine.sock`, `run/mcp.sock` and `secrets/github-token`.
Explicit absolute paths take precedence, with existing `~` and environment-variable
expansion. Existing complete configurations continue to use their specified paths.
Unix socket paths remain limited to 100 bytes, and all workspace confinement rules
still apply. The default installation remains under `~/.xodex`.

Reruns preserve configuration, credentials and retained state. Identical generated
files are reused without touching their contents or timestamps. Conflicting units,
credentials or unsafe file paths are reported and never overwritten. Review and
back up a conflicting generated file, then move it aside yourself before rerunning
setup. Edit existing repository policies explicitly; setup does not replace them.
Unit files live under `${XDG_CONFIG_HOME:-~/.config}/systemd/user`.
Setup enables units by absolute filename, so the user manager can find them even
when its `XDG_CONFIG_HOME` differs from the setup shell's environment. The printed
start command uses those same filenames if you choose to start services later.

Failures and cancellation identify the stage and give a command to resume. Files
completed before the failure remain available to the next run. Generated resume,
worker-build and smoke commands use the installed Python interpreter, so they work
even when `~/.local/bin` is not on `PATH`. If startup partly
succeeded, inspect `systemctl --user status` before resuming. Reinstalling updates
the package but does not automatically restart running services; plan any restart
with the retained-work recovery behavior in mind.

## Credentials and private tunnel

Use a fine-grained GitHub token limited to the allowed repositories, with contents
and pull-request read/write permissions. Organization approval/SSO and workflow-file
permissions may also apply. Review GitHub's [token guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
and [pull-request API](https://docs.github.com/en/rest/pulls/pulls). Do not broaden
permissions automatically after a failure.

Credential files must be regular, owner-only (0600), owned by the service user and
not symlinks. Values are not accepted as command arguments. The supervisor loads
its GitHub credential into memory; workers never receive it. Restart the supervisor
explicitly after credential rotation. The GitHub integration targets GitHub.com
HTTPS and REST version `2022-11-28`, without Enterprise or SSH remotes.

Install a verified [official tunnel-client](https://github.com/openai/tunnel-client)
release bundle, preserving its runtime files, normally in
`~/.local/opt/openai-tunnel/`. Authorize a tunnel for the OpenAI context that will
use the app. Both client installation and account authorization are external steps.
Setup accepts its ID and a masked **tunnel runtime key**, or reuses existing private
`tunnel-client.yaml` and `tunnel.env` files. This key is not an organization admin
key or ChatGPT session token. To reuse files/client at other locations:

```bash
xodex setup --tunnel-client /home/xodex/tools/tunnel-client \
  --tunnel-config /home/xodex/private/tunnel-client.yaml \
  --tunnel-env /home/xodex/private/tunnel.env
```

Use the same options on reruns; the resume command includes them. The tunnel
environment-file path may include spaces, but not glob characters, backslashes,
control characters or trailing whitespace. The profile
retains the existing upstream `config_version: 1` format and routes channel `main`
to `http://localhost/mcp` through `env:MCP_UNIX_SOCKET_PATH`. The key is read through
`env:CONTROL_PLANE_API_KEY` from the service's private environment file. Setup
checks the bundled profile contract and file permissions; the upstream client
validates the full configuration. Existing incompatible/custom profiles are left
untouched and reported as pending. Check the client's
[configuration contract](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md).
A locally configured tunnel is not evidence of a working account connection.

Follow the official [developer-mode guide](https://developers.openai.com/api/docs/guides/developer-mode)
and [account/client guidance](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
to create **chatgpt-xodex**, choose the authorized tunnel and inspect its 17 tools.
Verify account eligibility, client availability and write-tool confirmations.
Select the app in the conversation and refresh its catalog when tool definitions
change. Xodex does not bypass confirmations or provide an integration with the
separately branded agent mode.

## Manual configuration and worker reference

The [configuration example](../deploy/config.example.toml) is a manual reference.
Replace its placeholder repository with one you are authorized to use:

```toml
[repositories."your-owner/xodex-fixture"]
checks = ["python3 test_calc.py"]
network = "none"
branch_prefix = "xodex/"
```

Never add credentials, browser cookies or arbitrary clone URLs to task arguments.
To provision a missing token manually without putting it in shell arguments:

```bash
install -d -m 700 ~/.xodex/secrets
# Bash; do not enable shell tracing. Only use this for a missing credential file.
read -r -s -p 'GitHub token: ' TOKEN; printf '\n'
(umask 077; set -o noclobber; printf '%s\n' "$TOKEN" > ~/.xodex/secrets/github-token)
unset TOKEN
```

Build the default worker with the installed command:

```bash
xodex build-worker
RUST_TOOLCHAIN='<repository-pinned-version>' BASE_IMAGE='<reviewed-base-digest>' xodex build-worker
```

The default tag is `localhost/chatgpt-xodex-worker:0.1.0`. Retain the resulting image
digest. The engine resolves the tag at startup; rebuilding does not restart it.
The image supplies Bash, Git, ripgrep, Python, Node/npm, Rust tooling, compilers
and libpcap headers. Extra languages, pnpm, browser runtimes, libraries or services
need an owner-maintained image. Rust toolchains must be installed at build time;
`RUSTUP_HOME` is read-only at runtime and `CARGO_HOME` is task-local and writable.
The bundled build command refuses custom tags to avoid replacing an owner's image.

Workers are offline by default. Preprovision dependencies/caches or explicitly
set `network = "slirp4netns:allow_host_loopback=false"` for that repository. This
allows broad outbound access, not a domain allowlist; apply appropriate egress
controls. It never authorizes mounting host credentials, agents or runtime sockets.
See [security](SECURITY.md).

Service templates, tunnel templates and the Containerfile have one authoritative
copy in packaged [assets](../src/xodex/assets). Templates use setup substitution
markers and must be rendered before manual installation. The old build and smoke
scripts are thin wrappers around installed `xodex build-worker` and `xodex smoke`;
`BASE_IMAGE`, `RUST_TOOLCHAIN` and the smoke script's `--socket` still work.

## Checks and troubleshooting

```bash
xodex doctor --probe
xodex smoke
xodex smoke --socket /absolute/path/to/mcp.sock
systemctl --user status xodex-engine xodex-mcp xodex-tunnel
journalctl --user -u xodex-engine.service -u xodex-mcp.service -f
```

| Failed stage | Resume action |
|---|---|
| Host prerequisites | Provision missing tools, subordinate IDs, cgroup v2 or the user manager; log in again, then run the reported setup command. |
| GitHub credentials/access | Fix the private file's owner/mode or repository authorization, then rerun setup. |
| Worker image | Run `xodex build-worker`, or provision the configured custom image, then rerun setup. |
| Sandbox probe | Inspect `xodex doctor --probe`; correct the rootless host/image before startup. |
| Tunnel configuration pending | Install/authorize the client and supply private files, then rerun setup with the same tunnel options. Local services can remain available. |
| Service file conflict | Review/back up and move aside only the conflicting generated file, then rerun setup. |
| Reload/startup/MCP smoke | Inspect user service status and journals, correct the cause, then resume setup. Do not reset retained state. |

To start services later after a successful setup, use its printed start command,
then `xodex smoke`. Smoke negotiates MCP, reads catalog/health and lists tasks.
It does not exercise the live tunnel. Run the opt-in real Podman tests as described
in [development](DEVELOPMENT.md); mocked tests do not establish sandbox isolation.

## Live acceptance before real work

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
