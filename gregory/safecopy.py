"""Copy files that somebody else controls, without ever following a link.

Gregory copies two kinds of foreign material into the directory a model may read: a skill from the
skills repository and files a client put into its hand-over folder. A symbolic link in either could
point at Gregory's own credentials, so every path component is opened with O_NOFOLLOW relative to
its parent directory, and only regular files and directories are taken.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 16
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class CopyRefused(Exception):
    """The text is safe to show to the client."""


@dataclass
class Budget:
    files: int
    bytes: int

    def take(self, size: int) -> None:
        self.files -= 1
        self.bytes -= size
        if self.files < 0:
            raise CopyRefused("too many files")
        if self.bytes < 0:
            raise CopyRefused("the files are too large")


def parts_of(name: str) -> tuple[str, ...]:
    """A relative path as given by a client -> its components. No '..', no absolute path, nothing hidden."""
    if not isinstance(name, str) or not name or len(name) > 300 or "\x00" in name or name.startswith("/"):
        raise CopyRefused("a name must be a relative path")
    parts = tuple(p for p in name.split("/") if p not in ("", "."))
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise CopyRefused(f"{name}: '..' and hidden names are not allowed")
    return parts


def _open(name: str, dir_fd: int, flags: int, shown: str) -> int:
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise CopyRefused(f"{shown}: not found") from None
    except OSError:  # ELOOP for a link, ENOTDIR, EACCES ...
        raise CopyRefused(f"{shown}: not a plain file or folder that can be read") from None


def _copy_file(fd: int, dest: Path, budget: Budget, shown: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise CopyRefused(f"{shown}: not a plain file")
    budget.take(info.st_size)
    left = info.st_size
    with open(dest, "wb") as out:
        while left > 0:
            block = os.read(fd, min(CHUNK, left))
            if not block:
                break
            out.write(block)
            left -= len(block)
    os.chmod(dest, 0o644)


def _copy_dir(fd: int, dest: Path, budget: Budget, shown: str, depth: int) -> None:
    if depth > 8:
        raise CopyRefused(f"{shown}: folders are nested too deeply")
    dest.mkdir(mode=0o755)
    for name in sorted(os.listdir(fd)):
        if name.startswith("."):
            continue  # .git, .DS_Store and friends are never material
        child = _open(name, fd, FILE_FLAGS, f"{shown}/{name}")
        try:
            if stat.S_ISDIR(os.fstat(child).st_mode):
                _copy_dir(child, dest / name, budget, f"{shown}/{name}", depth + 1)
            else:
                _copy_file(child, dest / name, budget, f"{shown}/{name}")
        finally:
            os.close(child)


def copy(root: Path, name: str, dest: Path, budget: Budget) -> None:
    """Copy the file or folder `name` (relative, below `root`) to `dest`, which must not exist yet."""
    parts = parts_of(name)
    try:
        fd = os.open(root, DIR_FLAGS)
    except OSError:
        raise CopyRefused("the folder is not available") from None
    try:
        for part in parts[:-1]:
            nxt = _open(part, fd, DIR_FLAGS, name)
            os.close(fd)
            fd = nxt
        leaf = _open(parts[-1], fd, FILE_FLAGS, name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if stat.S_ISDIR(os.fstat(leaf).st_mode):
                _copy_dir(leaf, dest, budget, name, 0)
            else:
                _copy_file(leaf, dest, budget, name)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)
