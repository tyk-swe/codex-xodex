"""Bounded supervisor-owned subprocesses. This is not an agent shell backend."""
from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Sequence

from .errors import XodexError


async def run_control(argv: Sequence[str], env: dict[str, str], *, data: bytes = b"",
                      timeout: int = 120, limit: int = 16 * 1024 * 1024) -> bytes:
    async def collect(reader: asyncio.StreamReader, budget: int) -> bytes:
        chunks = bytearray()
        while chunk := await reader.read(65536):
            chunks.extend(chunk)
            if len(chunks) > budget:
                raise XodexError("control_output_limit", "Control command exceeded its output budget")
        return bytes(chunks)

    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await spawn
        # The leader may exit while descendants still hold its output pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise

    async def feed() -> None:
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    tasks = [asyncio.create_task(feed()), asyncio.create_task(collect(process.stdout, limit)),
             asyncio.create_task(collect(process.stderr, 65536)), asyncio.create_task(process.wait())]
    try:
        async with asyncio.timeout(timeout):
            _, output, _, code = await asyncio.gather(*tasks)
        if code:
            # No arbitrary remote error text, credentials, or repository-controlled escape sequences in errors.
            raise XodexError("git_failed", "A managed Git operation failed; check repository access, branch policy, or network",
                             exit_code=code)
        return output
    except TimeoutError as error:
        raise XodexError("git_timeout", "A managed Git operation timed out; remote effects may require reconciliation") from error
    finally:
        # The leader may exit while descendants still hold its output pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A killed process can still have a paused StreamReader. Waiting without
        # draining its pipes can deadlock after an output-budget violation.
        await process.communicate()
