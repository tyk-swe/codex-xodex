from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import XodexError
from .files import WorkspaceFS, relative, sha256
from .jsonutil import canonical


@dataclass
class Change:
    path: str
    before: bytes | None
    after: bytes | None
    mode: int = 0o644


def _updated(original: bytes, body: list[str]) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise XodexError("patch_invalid", "Patches only edit UTF-8 text") from error
    if "\x00" in text:
        raise XodexError("patch_invalid", "Cannot patch a binary file")
    newline = "\r\n" if "\r\n" in text else "\n"
    source = text.replace("\r\n", "\n").split("\n")
    ended = bool(source and source[-1] == "")
    if ended:
        source.pop()
    output: list[str] = []
    cursor = 0
    index = 0
    saw_hunk = False
    while index < len(body):
        header = body[index]
        if header != "@@" and not header.startswith("@@ "):
            raise XodexError("patch_invalid", "Each update hunk must start with @@")
        index += 1
        anchor = header[3:] if header.startswith("@@ ") else ""
        if anchor:
            positions = [i for i in range(cursor, len(source)) if source[i] == anchor]
            if len(positions) != 1:
                raise XodexError("patch_ambiguous", "Hunk anchor must match one exact line", anchor=anchor)
            anchor_end = positions[0] + 1
            output.extend(source[cursor:anchor_end])
            cursor = anchor_end
        old: list[str] = []
        new: list[str] = []
        eof = False
        while index < len(body) and not body[index].startswith("@@"):
            line = body[index]
            index += 1
            if line == "*** End of File":
                eof = True
                if index < len(body):
                    raise XodexError("patch_invalid", "End of File must be the final hunk line")
                break
            if not line or line[0] not in " +-":
                raise XodexError("patch_invalid", "Hunk lines must start with a space, +, or -")
            if line[0] in " -":
                old.append(line[1:])
            if line[0] in " +":
                new.append(line[1:])
        if not old:
            if not eof:
                raise XodexError("patch_ambiguous", "Context-free insertions must be anchored at End of File")
            position = len(source)
        else:
            positions = [i for i in range(cursor, len(source) - len(old) + 1)
                         if source[i:i + len(old)] == old and (not eof or i + len(old) == len(source))]
            if len(positions) != 1:
                raise XodexError("patch_ambiguous", "Expected one exact context match; fuzzy patches are rejected",
                                 matches=len(positions))
            position = positions[0]
        output.extend(source[cursor:position])
        output.extend(new)
        cursor = position + len(old)
        saw_hunk = True
    if not saw_hunk:
        raise XodexError("patch_invalid", "An update must have at least one hunk")
    output.extend(source[cursor:])
    # Preserve the source's newline convention and final-newline state.
    return (newline.join(output) + (newline if ended and output else "")).encode("utf-8")


def prepare(fs: WorkspaceFS, patch: str, expected: dict[str, str]) -> list[Change]:
    if len(patch.encode()) > 1024 * 1024:
        raise XodexError("patch_invalid", "Patch exceeds 1 MiB")
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise XodexError("patch_invalid", "Expected Codex Begin Patch / End Patch markers")
    changes: list[Change] = []
    touched: set[str] = set()
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        index += 1
        if header.startswith("*** Add File: "):
            path = header[14:]
            relative(path)
            if fs.exists(path):
                raise XodexError("patch_conflict", "Add target already exists", path=path)
            added: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                line = lines[index]
                if not line.startswith("+"):
                    raise XodexError("patch_invalid", "Add-file lines must start with +")
                added.append(line[1:])
                index += 1
            changes.append(Change(path, None, ("\n".join(added) + ("\n" if added else "")).encode()))
        elif header.startswith("*** Delete File: "):
            path = header[17:]
            data, mode = fs.read(path)
            changes.append(Change(path, data, None, mode))
        elif header.startswith("*** Update File: "):
            path = header[17:]
            data, mode = fs.read(path)
            target = path
            if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                target = lines[index][13:]
                relative(target)
                index += 1
                if target == path or fs.exists(target):
                    raise XodexError("patch_conflict", "Move destination must not exist", path=target)
            body = []
            while index < len(lines) - 1:
                line = lines[index]
                if line.startswith("*** ") and line != "*** End of File":
                    break
                body.append(line)
                index += 1
            after = _updated(data, body)
            if target != path:
                changes.extend([Change(target, None, after, mode), Change(path, data, None, mode)])
            else:
                changes.append(Change(path, data, after, mode))
        else:
            raise XodexError("patch_invalid", "Unknown patch header", header=header)
        if len(changes) > 100:
            raise XodexError("patch_invalid", "Patch touches more than 100 paths")
    if not changes:
        raise XodexError("patch_invalid", "Empty patch")
    for change in changes:
        if change.path in touched:
            raise XodexError("patch_invalid", "A patch may touch a path only once", path=change.path)
        touched.add(change.path)
        if change.before is not None and change.path in expected and sha256(change.before) != expected[change.path]:
            raise XodexError("patch_conflict", "File changed since it was read", path=change.path)
    if set(expected) - touched:
        raise XodexError("patch_invalid", "expected_sha256 contains an untouched path")
    return changes


def commit(fs: WorkspaceFS, changes: list[Change], journal: Path) -> dict[str, Any]:
    journal.mkdir(mode=0o700)
    record = [{"path": c.path, "before": base64.b64encode(c.before).decode() if c.before is not None else None,
               "after_sha256": sha256(c.after) if c.after is not None else None, "mode": c.mode}
              for c in changes]
    with (journal / "before.json").open("x", encoding="utf-8") as file:
        file.write(canonical(record))
        file.flush()
        os.fsync(file.fileno())
    fd = os.open(journal, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
        parent_fd = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(fd)
    applied: list[Change] = []
    try:
        for change in changes:
            # Detect an out-of-band edit before replacing it; worker jobs are excluded by the engine lease.
            current = fs.read(change.path)[0] if fs.exists(change.path) else None
            if current != change.before:
                raise XodexError("patch_conflict", "File changed during patch preparation", path=change.path)
            applied.append(change)
            if change.after is None:
                fs.delete(change.path)
            else:
                fs.write(change.path, change.after, change.mode)
    except BaseException as error:
        failures = []
        for change in reversed(applied):
            try:
                current = fs.read(change.path)[0] if fs.exists(change.path) else None
                if current == change.before:
                    continue
                if current != change.after:
                    raise XodexError("reconciliation_required", "Out-of-band edit prevents safe rollback", path=change.path)
                if change.before is None:
                    fs.delete(change.path)
                else:
                    fs.write(change.path, change.before, change.mode)
            except BaseException as rollback_error:
                failures.append(str(rollback_error))
        if failures:
            raise XodexError("reconciliation_required", "Patch failed and rollback was incomplete",
                             journal=str(journal), failures=failures) from error
        raise
    return {"changed": [{"path": c.path, "sha256": sha256(c.after) if c.after is not None else None,
                          "action": "delete" if c.after is None else "add" if c.before is None else "update"}
                         for c in changes], "journal": str(journal), "atomicity": "per-file; protected preimages retained"}
