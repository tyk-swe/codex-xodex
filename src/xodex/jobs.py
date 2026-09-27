from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import pty
import shutil
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .backend import Launch, PodmanBackend
from .config import Config
from .errors import XodexError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Running:
    id: int
    task_id: str
    name: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    launched: asyncio.Event = field(default_factory=asyncio.Event)
    process: asyncio.subprocess.Process | None = None
    master: int | None = None
    output_fd: int | None = None
    reason: str | None = None
    task: asyncio.Task | None = None
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    terminate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Jobs:
    def __init__(self, config: Config, store: Store, backend: PodmanBackend,
                 changed: Callable[[dict[str, Any]], None]):
        self.config, self.store, self.backend, self.changed = config, store, backend, changed
        self.running: dict[int, Running] = {}
        self.closing = False
        self.fault: str | None = None

    def name(self, job: int) -> str:
        return f"xodex-{self.store.instance[:12]}-{job}"

    def active(self, task_id: str) -> list[int]:
        return [job.id for job in self.running.values() if job.task_id == task_id]

    def start(self, task_id: str, root: Path, home: Path, command: str, cwd: str,
              tty: bool, timeout: int, network: str, kind: str = "command") -> int:
        if self.fault:
            raise XodexError("storage_fault", "Command state could not be persisted; restart after owner inspection")
        if self.closing:
            raise XodexError("shutting_down", "Supervisor is stopping; task_id remains retained")
        if len(self.running) >= self.config.max_jobs:
            raise XodexError("busy", "Global command limit reached")
        cursor = self.store.db.execute(
            "INSERT INTO jobs(task_id,kind,command,cwd,tty,state,created,timeout_seconds) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, kind, command, cwd, int(tty), "starting", time.time(), timeout))
        job = cursor.lastrowid
        try:
            self.store.log_path(job).touch(mode=0o600, exist_ok=False)
        except OSError as error:
            self.store.db.execute("UPDATE jobs SET state='failed',finished=?,reason='log_create_failed' WHERE id=?",
                                  (time.time(), job))
            raise XodexError("command_not_started", "Could not create the retained command log; no command was launched",
                             command_id=job, errno=error.errno) from error
        running = Running(job, task_id, self.name(job))
        self.running[job] = running
        running.task = asyncio.create_task(self._run(running, root, home, command, cwd, tty, timeout, network))
        return job

    async def _read_pty(self, fd: int) -> bytes:
        loop = asyncio.get_running_loop()
        while True:
            try:
                return os.read(fd, 65536)
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_reader(fd, lambda: not ready.done() and ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_reader(fd)
            except OSError as error:
                if error.errno == errno.EIO:
                    return b""
                raise

    async def _collect(self, live: Running) -> None:
        count = 0
        with self.store.log_path(live.id).open("ab", buffering=0) as log:
            while True:
                chunk = await self._read_pty(live.output_fd)
                if not chunk:
                    break
                capacity = self.config.output_limit_bytes - count
                if capacity > 0:
                    written = log.write(chunk[:capacity])
                    os.fsync(log.fileno())
                    count += written
                    self.store.db.execute("UPDATE jobs SET output_bytes=? WHERE id=?", (count, live.id))
                if len(chunk) > capacity:
                    live.reason = "output_limit"
                    await self._terminate(live)
                    break
                if shutil.disk_usage(self.store.root).free < self.config.min_free_bytes:
                    live.reason = "disk_pressure"
                    await self._terminate(live)
                    break

    async def _watch_space(self, live: Running, root: Path, waiter: asyncio.Task) -> None:
        while not waiter.done():
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 1)
            except TimeoutError:
                if any(shutil.disk_usage(path).free < self.config.min_free_bytes
                       for path in (self.store.root, root)):
                    live.reason = "disk_pressure"
                    await self._terminate(live)
                    return

    async def _terminate(self, live: Running) -> None:
        # Serialize stop, timeout and normal-finalization cleanup of the same boundary.
        async with live.terminate_lock:
            process = live.process
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            await self.backend.cleanup(live.name)

    async def wait_capacity(self) -> None:
        while len(self.running) >= self.config.max_jobs:
            lives = list(self.running.values())
            waits = [asyncio.create_task(live.done.wait()) for live in lives]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
                await asyncio.gather(*waits, return_exceptions=True)

    async def _spawn(self, live: Running, launch: Launch, tty: bool) -> None:
        child_fd = None
        try:
            if tty:
                live.master, child_fd = pty.openpty()
                live.output_fd = live.master
                os.set_blocking(live.master, False)
                fcntl.ioctl(live.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                bootstrap = str(Path(__file__).with_name("ptyexec.py"))
                argv = [sys.executable, bootstrap, *launch.argv]
                stdin = child_fd
            else:
                live.output_fd, child_fd = os.pipe2(os.O_CLOEXEC)
                os.set_blocking(live.output_fd, False)
                argv = launch.argv
                stdin = asyncio.subprocess.PIPE
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, stdin=stdin, stdout=child_fd, stderr=child_fd,
                env=launch.env, cwd=launch.cwd, start_new_session=True,
            ))
            try:
                live.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Keep ownership until _finalize can reap a process admitted during cancellation.
                live.process = await spawn
                raise
        finally:
            if child_fd is not None:
                os.close(child_fd)

    async def _run(self, live: Running, root: Path, home: Path, command: str, cwd: str,
                   tty: bool, timeout: int, network: str) -> None:
        exit_code = None
        state = "failed"
        children = []
        try:
            if live.reason:
                state = "interrupted" if live.reason == "supervisor_shutdown" else "terminated"
                return
            launch = self.backend.launch(live.name, root, home, command, cwd, tty, network)
            await self._spawn(live, launch, tty)
            live.launched.set()
            if live.reason:
                await self._terminate(live)
            self.store.db.execute("UPDATE jobs SET state='running' WHERE id=?", (live.id,))
            collector = asyncio.create_task(self._collect(live))
            waiter = asyncio.create_task(live.process.wait())
            watchdog = asyncio.create_task(self._watch_space(live, root, waiter))
            children = [waiter, collector, watchdog]
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.gather(*children)
            except TimeoutError:
                live.reason = "timeout"
                await self._terminate(live)
            exit_code = live.process.returncode
            if live.reason:
                state = "interrupted" if live.reason == "supervisor_shutdown" else "terminated"
            else:
                state = "succeeded" if exit_code == 0 else "failed"
        except asyncio.CancelledError:
            live.reason = "supervisor_shutdown"
            state = "interrupted"
        except Exception as error:
            live.reason = f"runner_error:{type(error).__name__}"
            log.exception("Command execution failed: %s", live.id)
        finally:
            live.launched.set()
            for child in children:
                if not child.done():
                    child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            await self._finalize(live, state, exit_code)

    async def _finalize(self, live: Running, state: str, exit_code: int | None) -> None:
        try:
            try:
                await self._terminate(live)
            except Exception as error:
                state, live.reason = "uncertain", "boundary_cleanup_failed"
                self.store.update(live.task_id, reconciliation=str(error)[:2000])
            self.store.db.execute(
                "UPDATE jobs SET state=?,finished=?,exit_code=?,reason=? WHERE id=?",
                (state, time.time(), exit_code, live.reason, live.id),
            )
            self.changed(self.store.job(live.task_id, live.id))
            self.store.audit("job_finished", live.task_id, {
                "command_id": live.id, "state": state, "exit_code": exit_code, "reason": live.reason,
            })
        except Exception:
            self.fault = "Unable to persist terminal command state"
            log.exception("Failing closed after command persistence failure: %s", live.id)
        finally:
            if live.output_fd is not None:
                os.close(live.output_fd)
                live.output_fd = None
                live.master = None
            self.running.pop(live.id, None)
            live.done.set()

    async def poll(self, task_id: str, job: int, cursor: int = 0, limit: int = 24000,
                   wait_ms: int = 0) -> dict[str, Any]:
        self.store.job(task_id, job)
        live = self.running.get(job)
        if live is not None and wait_ms:
            try:
                await asyncio.wait_for(live.done.wait(), wait_ms / 1000)
            except TimeoutError:
                pass
        record = self.store.job(task_id, job)
        path = self.store.log_path(job)
        size = path.stat().st_size if path.exists() else 0
        if cursor > size:
            raise XodexError("invalid_cursor", "Output cursor exceeds retained bytes", bytes=size)
        data = b""
        if path.exists():
            with path.open("rb") as file:
                file.seek(cursor)
                data = file.read(limit)
        return {"command_id": job, "task_id": task_id, "state": record["state"],
                "exit_code": record["exit_code"], "reason": record["reason"],
                "output": data.decode("utf-8", errors="replace"), "cursor": cursor,
                "next_cursor": cursor + len(data), "total_bytes": size,
                "has_more": cursor + len(data) < size, "tty": bool(record["tty"]),
                "encoding": "utf-8 with replacement; cursors are raw byte offsets"}

    async def stdin(self, task_id: str, job: int, chars: str, close: bool) -> dict[str, Any]:
        self.store.job(task_id, job)
        live = self.running.get(job)
        if live is None or live.process is None or live.process.returncode is not None:
            raise XodexError("not_running", "Command is not accepting input; poll its status")
        data = chars.encode("utf-8")
        if len(data) > 65536:
            raise XodexError("invalid_input", "Input exceeds 64 KiB")
        async with live.input_lock:
            try:
                async with asyncio.timeout(2):
                    if live.master is not None:
                        if close:
                            raise XodexError("invalid_input", "PTYs have no half-close; send \\u0004 explicitly")
                        await self._write_pty(live.master, data)
                    else:
                        if live.process.stdin.is_closing():
                            raise XodexError("input_closed", "stdin is already closed")
                        live.process.stdin.write(data)
                        await live.process.stdin.drain()
                        if close:
                            live.process.stdin.close()
            except (TimeoutError, BrokenPipeError, OSError) as error:
                raise XodexError("input_uncertain", "Input may be partially delivered; inspect before sending again") from error
        return {"command_id": job, "accepted_bytes": len(data), "stdin_closed": close}

    async def _write_pty(self, fd: int, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        offset = 0
        while offset < len(data):
            try:
                offset += os.write(fd, data[offset:])
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_writer(fd, lambda: not ready.done() and ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_writer(fd)

    async def recover(self) -> None:
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state IN ('starting','running','stopping','uncertain')").fetchall()
        for row in rows:
            try:
                await self.backend.cleanup(self.name(row["id"]))
            except XodexError as error:
                self.store.db.execute("UPDATE tasks SET reconciliation=? WHERE id=?", (str(error), row["task_id"]))
                continue
            self.store.db.execute("UPDATE jobs SET state='interrupted',finished=?,reason='supervisor_restart' WHERE id=?",
                                  (time.time(), row["id"]))
            self.changed(self.store.job(row["task_id"], row["id"]))
        self.store.db.execute("UPDATE operations SET state='uncertain' WHERE state='pending'")
        uncertain = self.store.db.execute(
            "SELECT task_id,key FROM operations WHERE state='uncertain' AND tool IN ('apply_patch','write_file')").fetchall()
        for row in uncertain:
            self.store.db.execute("UPDATE tasks SET reconciliation=? WHERE id=?",
                                  ("Uncertain filesystem operation " + row["key"], row["task_id"]))


    async def stop(self, task_id: str, reason: str = "user_stop") -> None:
        lives = [live for live in self.running.values() if live.task_id == task_id]
        for live in lives:
            live.reason = reason
        for live in lives:
            await live.launched.wait()
            await self._terminate(live)
            await live.done.wait()

    async def shutdown(self) -> None:
        self.closing = True
        await asyncio.gather(*(self.stop(task_id, "supervisor_shutdown")
                               for task_id in {live.task_id for live in self.running.values()}))
