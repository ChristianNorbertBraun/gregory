"""bubblewrap sandbox for the model. (The unit tests of a downloaded release run in agent-tool-selfupdate's.)

What the sandboxed process sees: the system read-only, a few /etc files, the material of one request
read-only at /work, a small tmpfs home and /tmp. It does NOT see the home of the service user, so the
stored credentials are out of reach.
"""
from __future__ import annotations

import shutil
from pathlib import Path

WORK = "/work"
SANDBOX_HOME = "/home/sandbox"
ETC_RO = ("ssl", "ca-certificates", "resolv.conf", "hosts", "nsswitch.conf", "passwd", "group",
          "ld.so.cache", "alternatives", "localtime")
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
MODEL_BIN = "/opt/model/cli"  # not under /usr: that mount is read-only, bwrap cannot add files there
MAX_FILE_KB = 64 * 1024
MAX_PROCS = 512
# runs inside the sandbox before the real command: bound file size and process count
LIMIT_SCRIPT = f'ulimit -f {MAX_FILE_KB}; ulimit -u {MAX_PROCS}; exec "$@"'
TMP_BYTES = 256 * 1024 * 1024
HOME_BYTES = 64 * 1024 * 1024


def available() -> bool:
    return shutil.which("bwrap") is not None


def _base(etc_dir: Path) -> list[str]:
    args = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
        "--unshare-user", "--disable-userns",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--proc", "/proc", "--dev", "/dev",
        "--size", str(TMP_BYTES), "--tmpfs", "/tmp",
        "--size", str(HOME_BYTES), "--tmpfs", SANDBOX_HOME,
    ]
    for name in ETC_RO:
        src = Path(etc_dir) / name
        if src.exists():
            args += ["--ro-bind", str(src), f"/etc/{name}"]
    return args


def wrap_model(cmd: list[str], work: Path, binary: Path, etc_dir: Path = Path("/etc")) -> list[str]:
    """For one request to a model: `work` is READ-ONLY at /work and the model's program is at MODEL_BIN."""
    return _base(etc_dir) + ["--ro-bind", str(Path(work).resolve()), WORK,
                             "--ro-bind", str(Path(binary).resolve()), MODEL_BIN,
                             "--chdir", WORK, "--", "bash", "-c", LIMIT_SCRIPT, "_", *cmd]
