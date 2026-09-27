# MCP contract

## Transport and discovery

`/mcp` accepts JSON-RPC 2.0 POSTs over a private mode-0600 Unix socket. Supported
MCP versions are `2025-06-18` and `2025-03-26`. Responses are JSON; GET/SSE and
DELETE return 405. There are no connection-owned sessions, sampling requests,
subscriptions or protocol-level MCP Tasks.

Discovery exposes 17 tools, the Markdown resource `xodex://guide`, a
`coding-workflow` prompt and server instructions. `src/xodex/tools.py` is the
catalog authority; retrieve the exact schemas with `tools/list` rather than
maintaining a second JSON snapshot. See [development](DEVELOPMENT.md) for export.

| Task workflow | Purpose |
|---|---|
| `attach_repository` | Verify allowlist, repository identity and publication access. |
| `start_task` | Admit setup and create the task branch automatically. |
| `list_tasks`, `task_status` | Recover work, progress, evidence and output. |
| `stop_task`, `continue_task` | Stop without deletion; resume recoverable work. |
| `finish_task` | Validate, commit, push and create or reconcile the PR. |

Implementation tools are `exec_command`, `command_input`, `read_file`,
`list_files`, `search_files`, `write_file`, `apply_patch`, `view_image` and
`task_note`. `server_info` reports health and allowed repositories.

## Requests and results

Bodies are bounded to 2 MiB. JSON must use UTF-8, unique keys, finite numbers and
valid Unicode. Integer fields reject booleans and floating-point spellings such
as `1.0`. Unknown tools/arguments, malformed IDs, unsupported protocol headers
and NUL-containing shell/check commands are rejected before admission.
Notifications do not invoke tools.

Origins must be absent or one of the accepted ChatGPT web origins. **Origin
filtering is not authentication:** private socket and tunnel authorization are
required. The engine IPC rejects Origin-bearing requests.

Results contain `content`, `structuredContent` and `isError`. Check `ok`, then
task phase or command state and exit code; HTTP 200 does not mean a test passed.
Image results contain actual MCP image data.

## Example

Tool-call arguments below are agent bookkeeping, not daily user orchestration:

```jsonl
{"name":"attach_repository","arguments":{"request_id":"attach-1","repository":"tyk-swe/pcr"}}
{"name":"start_task","arguments":{"request_id":"task-1","repository":"tyk-swe/pcr","task":"Fix the parser bug and add a regression test."}}
```

Poll `task_status` with the returned `task_id`. Once working, inspect instructions,
edit and test. Then:

```json
{"name":"finish_task","arguments":{"task_id":"<UUID>","request_id":"finish-1","title":"fix: preserve parser invariant","summary":"Describe the actual change and evidence.","checks":["cargo test --locked -p packetcraftr-core"]}}
```

Owner-required checks are prepended; exact duplicate strings are removed. Checks
must be meaningful: the executor verifies execution, not semantic adequacy.

## Retries

Every mutation requires `request_id`, scoped to tool and task. Exact retries
return the original receipt; changed arguments under the same key fail. A timeout
does not establish that nothing ran. Do not use a new key to replay an unknown
effect. A deliberately corrected edit or validation is a new operation.

The ledger is written before execution. Pending operations after a crash become
uncertain. Patches/writes with unknown outcomes quarantine the task; offline
reconciliation acknowledges uncertainty without replay or restoration. Branch
conflicts are not force-updated, closed PRs are not recreated, and ambiguous PR
POSTs are not repeated automatically. See [operations](OPERATIONS.md).

## Commands

`command_id` belongs to one task. `task_status` defaults to the latest output
tail; explicit cursors are raw byte offsets. Output is decoded with UTF-8
replacement. Follow `next_cursor` and `has_more`; cursors past EOF are rejected.

Tool waits are at most ten seconds. Command runtime is separately bounded:
default 30 minutes. Tool-requested runtime is capped at one day and may be further
restricted by the owner. Timeout, output and free-space violations terminate
commands, not retained directories.
Background descendants keep the writer occupied until cleanup completes.

`command_input` targets the active command. PTY EOF is explicit `\u0004` input,
not pipe half-close. States distinguish success, failure, termination,
interruption and uncertain cleanup. No Podman availability means no execution.

## Files

Paths are repository-relative POSIX paths with UTF-8 names. Traversal and symlink
parents are refused. `write_file` requires the prior SHA-256 or an empty hash for
create-only. `apply_patch` accepts Codex Begin Patch add/update/delete/move grammar
with exact unique context—not `git apply`, fuzzy matching or a Codex runtime.

Preimages are fsynced outside worker mounts. Atomicity is per file, not per patch.
Rollback refuses to overwrite an out-of-band edit; incomplete rollback requires
reconciliation. Shell edits are not journaled and need filesystem backups.
