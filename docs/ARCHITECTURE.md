# Architecture

## Ownership

The **task** owns repository identity, base commit, branch, working files, command
history, validation evidence and PR identity. A workspace is its implementation
detail, not a separate public lifecycle.

```text
ChatGPT: coding decisions
  ↓ authorized OpenAI tunnel
Official tunnel-client
  ↓ private mode-0600 Unix socket; HTTP /mcp
xodex serve: MCP parsing, discovery and presentation
  ↓ separate private Unix socket; /v1/tools/<tool>
xodex supervise: task state and side effects
  ├─ Store: SQLite, operation ledger and audit history
  ├─ Jobs: containers, PTYs, output and termination
  ├─ Tasks: setup, validation, publication and recovery
  ├─ GitControl: protected objects/index and HTTPS push
  └─ GitHub: repository identity and pull requests
```

Two Python services separate execution from client connectivity. The third
service runs the upstream tunnel client. There is no broker, mirrored session,
workflow DSL, model router or second agent.

## Module boundaries

`cli.py` parses arguments and dispatches commands. `services.py` owns listener
locking, socket permissions and process lifetimes; `diagnostics.py` owns the
behavioral sandbox probe. `gateway.py` implements MCP, `supervisor.py` adapts the
engine to private HTTP, and `client.py` owns that IPC client. `server.py` retains
compatibility imports. `http.py` handles bounded HTTP bodies and responses;
`jsonutil.py` is the shared strict parser/canonical encoder and operation hasher.
Its encoding remains identical for existing persistence and retry identities.

`setup.py` orchestrates the interactive owner workflow. `deployment.py` handles
private generated files, prerequisites, worker builds and unit rendering;
`smoke.py` implements the read-only MCP check. Setup never instantiates an engine
or changes tasks. Service startup invokes the existing recovery logic. Assets in
`src/xodex/assets` ship in wheels and source releases, so deployment works without
access to the source checkout.

## Storage

Default layout; omitted runtime paths follow the selected configuration file’s
directory, and explicit absolute paths take precedence:

```text
~/.xodex/
  config.toml
  secrets/github-token                 # never mounted in workers
  tunnel.env, tunnel-client.yaml        # tunnel credential and configuration
  run/{engine,mcp}.sock
  tasks/<task-id>/attempt-N/{repo,home}/ # worker's only persistent mounts
  state/
    state.sqlite3                      # schema 1; WAL; FULL synchronous
    supervisor.lock                    # one owner, including offline admin
    jobs/<command-id>.log
    patches/<operation-key>/            # protected preimages
    tasks/<task-id>/attempt-N/
      source.git, index, blobs/         # protected publication data
  venv/
```

Only an empty, unversioned database is initialized. Existing state must identify
this schema and its instance; unsupported or unidentified state is rejected,
not adopted, migrated or relabeled. Startup does not create missing tables in
an existing database.

Task attempts and evidence are retained. A failed setup may create a new attempt
on explicit continuation; previous attempts remain. Ephemeral containers are
removed after commands finish. See [operations](OPERATIONS.md) for deletion.

## Lifecycle

`start_task` records `preparing`, verifies the attached repository ID, fetches its
base branch into protected storage and creates an independent shallow checkout
and branch. `working` accepts edits with one writer per task; separate tasks may
run concurrently within owner limits.

`finish_task` seals editing and enters `validating`, then `publishing` once the
validated commit is recorded. Success is `completed`; a validated empty diff
completes without a PR. Failures become `blocked` with evidence. Stop cancels the
lifecycle and command boundary and ends in `stopped`, without undoing remote
effects. Completed tasks are not reopened for additional coding.

## Publication invariant

**Publish exactly the candidate tree captured before validation and still
present after every successful check.**

1. Initialize a protected index from the captured base. Enumerate tracked and
   non-ignored untracked paths independently of the worker's index and remotes.
2. Read through confined directory descriptors, reject special files and symlink
   parents, and preserve symlink text, binary data, modes and deletions. Spool
   bounded raw bytes outside worker mounts and build blobs without Git filters.
3. Run required and task-specific checks sequentially. Retain actual terminal
   states, exit codes and output handles. Re-snapshot after each successful check;
   any changed tree blocks publication.
4. Create an aggregate commit with the captured base as parent, anchor it at
   `refs/xodex/result`, and persist its SHA before pushing.
5. Reverify repository identity and reconcile the task PR. Create the task branch
   only when absent, using an empty expected-value push lease; never overwrite a
   different commit. Confirm its SHA before attempting PR creation.
6. Record the PR-attempt marker before POST. Accept only the matching repository,
   head/base, task marker and commit. A lost or ambiguous response requires lookup,
   not an automatic second POST.

Host Git accesses worker `.git` only during initial checkout, before any worker
runs. Later publication uses protected metadata. Checks bind selected commands
to published bytes; they do not establish test adequacy or prevent malicious
code from changing its tests or temporarily mutating files during a check.

## Interruption

Gateway or tunnel loss does not cancel admitted execution. Exact mutation retries
return the same admission receipt, with current progress where applicable.

Supervisor restart reconciles containers and marks active work interrupted; it
never replays arbitrary commands. Continuation reuses a recorded publication
commit. Uncertain edits or cleanup require owner reconciliation. Unrecoverable
command-state persistence faults block new mutations until inspection/restart.

Task notes preserve explicit handoffs, not hidden model state. The service can
finish admitted setup, checks and publication without a client, but cannot invent
the next coding decision.
