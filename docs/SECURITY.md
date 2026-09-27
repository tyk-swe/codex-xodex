# Security

## Trust model

Xodex is a **single-owner private executor**, not a public or multi-tenant service.
Use a dedicated, unprivileged account on a non-production Linux VPS. Anyone
allowed to invoke the tunnel can act on its permitted repositories and tasks;
there is no separate per-user authorization layer. Never expose `/mcp` through
unauthenticated public HTTP.

Repository code, issues, output and tool arguments are untrusted. The host kernel,
Podman, Git/Python, owner configuration, service account, VPS administrator and
GitHub/OpenAI authorization are trusted. Rootless containers share the host
kernel; a dedicated VM limits the impact of a runtime or kernel compromise.

## Execution

Commands use rootless Podman, a read-only rootfs, dropped capabilities,
no-new-privileges, private PID/IPC/UTS namespaces and owner-defined CPU, memory,
PID and concurrency limits. The configured image tag is resolved to an image ID
at startup. Only that task's repository and worker home are mounted read/write.
No service state, sibling task, host home, credentials, agents or runtime sockets
are mounted. There is no production host-shell fallback.

The local test backend exists only under `tests/`; it is not installed in the
wheel or selectable from the CLI. Its passing tests do not establish real
container isolation. Run the Podman tests and `doctor --probe` on the deployment
host; see [development](DEVELOPMENT.md).

Networking is off by default. The optional slirp policy permits broad outbound
traffic, **not a domain allowlist**. Disabling host loopback does not isolate all
private networks. Network-enabled code can exfiltrate mounted data; apply suitable
host or external egress controls. Privileged networking and host packet capture
are not supported.

## Files and publication

File tools use no-follow, directory-relative access and atomic per-file writes.
A task cannot access sibling roots through these tools, but code can destroy
files in its own writable mounts. Retention is neither immutability nor a backup.

Host Git uses protected metadata after initial setup. Checkout and publication
preserve raw Git-object bytes without clean/smudge, EOL or encoding transformations.
Worker `.git` changes cannot redirect the publisher or hide tracked edits.
Submodules, nested repositories and LFS hydration are unsupported.

Destinations are fixed GitHub.com HTTPS origins plus validated allowlisted names.
Repository IDs are rechecked before publication. Managed Git/REST reject redirects;
REST ignores ambient proxies. Credentials stay outside workers, and arbitrary
remote error bodies are not returned to ChatGPT.

The push lease is create-only: an empty expected ref means the branch must not
exist. There is no API for overwriting another commit, pushing the default branch,
merging, deleting remote refs or choosing another publication host. PR creation
can trigger repository automation; owner permissions, branch rules and platform
confirmations remain authorization boundaries.

## Resources, secrets and remaining risks

Input, file, snapshot, output, process and runtime limits are bounded, but these
are **not disk quotas**. Clones, ignored outputs and protected evidence accumulate
and may grow before a free-space check reacts. Reserve space for SQLite/logging
and use filesystem quotas. Persistence faults close mutation admission rather
than reporting unreliable success.

Repository instructions cannot authorize new credentials, destinations or disabled
safeguards. Server checks enforce these boundaries independently of agent guidance;
the guidance itself is not a formal prompt-injection defense.

Do not place secrets in task text, commands, source or logs. Results may reach
ChatGPT and retained logs/preimages may contain sensitive code. Protect backups
and credentials. Rotation does not erase recorded secrets or undo remote effects.

Validation proves only the exercised checks against the captured bytes—not
semantic correctness, test independence, hermetic execution or security. There
is no secret-scanning guarantee or independent security certification.
