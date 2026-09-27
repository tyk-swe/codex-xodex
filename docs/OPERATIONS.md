# Operations

## Health and ordinary use

Ask ChatGPT for progress, stop or continuation. It manages task IDs and should
report the PR and actual validation evidence, or a specific blocker.

```bash
systemctl --user status xodex-engine xodex-mcp xodex-tunnel
journalctl --user -u xodex-engine.service --since '1 hour ago'
xodex smoke
```

Gateway/tunnel restarts do not terminate admitted jobs. Supervisor restart or
host reboot interrupts execution; recovery marks it accordingly and does not
replay shell commands. Continue reuses files or the recorded publication commit.
A user-stopped task requires an explicit request to resume.

| Blocker | Owner/agent action |
|---|---|
| `repository_not_allowed`, `repository_revoked` | Review the exact allowlist and restart; never send a token in chat. |
| `repository_readonly`, `github_http` | Check permissions, SSO, rate limits and branch policy without automatically widening access. |
| `validation_failed`, `validation_changed_tree` | Inspect evidence, continue, fix and finish with a new intentional request. |
| `publication_conflict` | Investigate repository, branch and PR identity; never overwrite to force success. |
| `pr_outcome_unknown` | Inspect GitHub and continue reconciliation; do not blindly repeat POST. |
| `reconciliation_required` | Inspect files/processes, then acknowledge through offline administration. |
| `storage_fault`, `disk_pressure` | Restore storage health and inspect retained records before restart. |
| `schema_version` | Use an empty state directory for this initial schema; never relabel another database. |

## Offline administration

These commands are owner-only and not exposed through MCP. Stop the supervisor;
its exclusive state lock otherwise refuses access. Inspect the task first.

```bash
systemctl --user stop xodex-engine.service
~/.local/bin/xodex admin reconcile --task '<UUID>' --confirm '<UUID>'
systemctl --user start xodex-engine.service
```

`reconcile` verifies container cleanup and acknowledges uncertainty. It does not
replay commands, restore files or validate code. Inspect the protected patch
journal and working copy first. Uncertain historical request IDs stay non-replayable.

For an ambiguous PR request, independently check GitHub before authorizing a
future retry while the engine is stopped:

```bash
~/.local/bin/xodex admin retry-pr --task '<UUID>' --confirm '<UUID>'
```

The command checks again and refuses a visible matching PR. A missing lookup is
not proof of absence: this explicit override accepts duplicate risk. It does not
create the PR itself. Restart the engine and continue the task.

## Retention and deletion

Stop, finish, restart and disk pressure never authorize automatic deletion.
To remove one task's working directory, stop the engine and run:

```bash
~/.local/bin/xodex admin delete --task '<UUID>' --confirm '<UUID>'
```

Only that task directory is removed after container cleanup. Siblings, metadata,
command logs, patch journals and protected Git objects remain. Remote branches
and PRs are untouched. Restart the engine afterward. Evidence has no automatic
GC; archive or remove it only through a deliberate offline storage procedure.

## Backups

Stop services and back up task and state trees together. Copying only the live
SQLite database omits WAL data and is not a consistent backup. Protect configuration
and secrets; test restoration in an isolated, network-disabled instance before
reconnecting publication credentials. Never run two supervisors on one state root.

Patch journals cover file-tool edits, not arbitrary shell changes. Publication
snapshots cover publishable bytes, not every ignored artifact. Use independent
filesystem backups for broader recovery. Reinstalling does not overwrite owner
configuration or remove retained state; it is not a database reset command.
