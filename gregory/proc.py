"""Run a process with bounded output and a timeout. Output goes to a temp file, never into
unbounded memory buffers."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

TIMED_OUT = 124


def run_capped(cmd: list[str], *, cwd: Path | None, env: dict, timeout: float, limit: int = 64_000,
               stdin_text: str | None = None) -> tuple[int, str, bool]:
    """Return (exit_code, output, truncated) with the FIRST `limit` bytes of output. Exit code 124 means timeout."""
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text.encode())
                p.stdin.close()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + timeout
        code = None
        while code is None:
            try:
                code = p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    _kill(p)
                    code = TIMED_OUT
        size = out.seek(0, os.SEEK_END)
        out.seek(0)
        return code, out.read(limit).decode(errors="replace"), size > limit


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, 9)
    except ProcessLookupError:
        pass
    p.wait()
