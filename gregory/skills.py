"""Skills a client may name in a request, kept in one git repository.

Layout of that repository:

    <area>/access.toml        clients = ["merlin"]     who may name this area's skills
                              backend = "claude"       optional: which backend answers for this area
    <area>/<skill>/SKILL.md   the skill, in the common SKILL.md format, with any files next to it

Files at the top level (a README, a check script) and hidden entries are ignored. `sync` fetches a
branch with a read-only deploy key, checks the tree, publishes a copy without links below
`[skills] dir` as versions/<commit> and then points `current` at it in one step. A tree that fails
the check is not published: the old one stays. The published copy is plain files, so the agents
can load the same skills from `<dir>/current/<area>`.
"""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import safecopy
from .config import NAME_RE, SkillsConfig

MAX_FILES = 2000
MAX_BYTES = 100 * 1024 * 1024
MAX_SKILL_BYTES = 10 * 1024 * 1024
KEEP = 3
SHA_RE = re.compile(r"[0-9a-f]{40}")


class SkillsError(Exception):
    """The text is safe to show to the client."""


@dataclass(frozen=True)
class Skill:
    area: str
    name: str
    clients: frozenset[str]
    backend: str

    @property
    def path(self) -> str:
        return f"{self.area}/{self.name}"


def _access(area: Path) -> tuple[frozenset[str], str]:
    try:
        data = tomllib.loads((area / "access.toml").read_text())
    except FileNotFoundError:
        raise SkillsError(f"{area.name}/: a folder at the top level needs an access.toml") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise SkillsError(f"{area.name}/access.toml cannot be read") from None
    clients, backend = data.get("clients"), data.get("backend", "")
    if not isinstance(clients, list) or not all(isinstance(c, str) and NAME_RE.fullmatch(c) for c in clients):
        raise SkillsError(f"{area.name}/access.toml: clients must be a list of client names")
    if not isinstance(backend, str) or (backend and not NAME_RE.fullmatch(backend)):
        raise SkillsError(f"{area.name}/access.toml: backend must be the name of a backend")
    return frozenset(clients), backend


def read(root: Path) -> list[Skill]:
    """Every skill in the tree at `root`. Raises SkillsError for the first thing that is not acceptable."""
    found: list[Skill] = []
    files = total = 0
    try:
        tops = sorted(root.iterdir())
    except OSError:
        raise SkillsError("no skills are installed") from None
    for area in tops:
        if area.name.startswith("."):
            continue
        if area.is_symlink():
            raise SkillsError(f"{area.name}: links are not allowed")
        if not area.is_dir():
            continue
        if not NAME_RE.fullmatch(area.name):
            raise SkillsError(f"{area.name}/: a folder name is lowercase letters, digits and dashes")
        clients, backend = _access(area)
        for skill in sorted(area.iterdir()):
            if skill.name.startswith(".") or skill.name == "access.toml":
                continue
            if skill.is_symlink():
                raise SkillsError(f"{area.name}/{skill.name}: links are not allowed")
            if not skill.is_dir():
                raise SkillsError(f"{area.name}/{skill.name}: only access.toml and skill folders belong here")
            if not NAME_RE.fullmatch(skill.name):
                raise SkillsError(f"{area.name}/{skill.name}/: a skill name is lowercase letters, digits and dashes")
            size = 0
            for base, dirs, names in os.walk(skill, followlinks=False):
                for name in dirs + names:
                    entry = Path(base) / name
                    shown = entry.relative_to(root).as_posix()
                    if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                        raise SkillsError(f"{shown}: only plain files and folders are allowed")
                    if name in names:
                        files += 1
                        size += entry.stat().st_size
            if not (skill / "SKILL.md").is_file():
                raise SkillsError(f"{area.name}/{skill.name}/: SKILL.md is missing")
            if size > MAX_SKILL_BYTES:
                raise SkillsError(f"{area.name}/{skill.name}/ is larger than {MAX_SKILL_BYTES // 2**20} MB")
            total += size
            found.append(Skill(area.name, skill.name, clients, backend))
    if files > MAX_FILES or total > MAX_BYTES:
        raise SkillsError("the skills repository is too large")
    return found


def allowed(skills: list[Skill], client: str) -> list[Skill]:
    return [s for s in skills if client in s.clients]


def find(skills: list[Skill], client: str, name: str) -> Skill:
    """The skill a client named, as `area/skill` or just `skill`. One it may not use looks like one that does not exist."""
    mine = allowed(skills, client)
    hits = [s for s in mine if name in (s.path, s.name)] if isinstance(name, str) else []
    if len(hits) == 1:
        return hits[0]
    can = ", ".join(s.path for s in mine) or "none"
    if hits:
        raise SkillsError(f"several skills are called {name}: name it with its area. You may use: {can}")
    raise SkillsError(f"unknown skill. You may use: {can}")


# ---- fetching and publishing ------------------------------------------------------------------------

def current(cfg: SkillsConfig) -> Path:
    return cfg.dir / "current"


def _git(args: list[str], key: Path, cwd: Path | None, run) -> subprocess.CompletedProcess:
    ssh = (f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
           f"-o UserKnownHostsFile={key.parent / 'known_hosts'}")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(key.parent), "GIT_SSH_COMMAND": ssh, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "LANG": "C.UTF-8"}
    return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", *args],
               cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def sync(cfg: SkillsConfig, key: Path, state: Path, run: Callable = subprocess.run,
         clock: Callable[[], float] = time.time) -> str:
    """Fetch, check, publish, switch. Returns one line for the client; raises SkillsError when nothing was changed."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state / "skills.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SkillsError("the skills are being updated right now") from None
        try:
            line = _sync(cfg, key, state, run)
        except SkillsError as e:
            _note(state, clock, f"failed: {e}")
            raise
        _note(state, clock, line)
        return line


def _note(state: Path, clock, text: str) -> None:
    (state / "skills-status").write_text(time.strftime("%Y-%m-%d %H:%M ", time.localtime(clock())) + text + "\n")


def status(state: Path) -> str:
    try:
        return (state / "skills-status").read_text().strip()[:400]
    except OSError:
        return ""


def _sync(cfg: SkillsConfig, key: Path, state: Path, run) -> str:
    fetch = state / "skills-fetch"
    shutil.rmtree(fetch, ignore_errors=True)
    try:
        p = _git(["clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags", "--branch", cfg.branch,
                  "--", cfg.repo, str(fetch)], key, None, run)
        if p.returncode != 0:
            raise SkillsError("the skills repository could not be fetched (address, branch or key)")
        sha = _git(["rev-parse", "HEAD"], key, fetch, run).stdout.strip()
        if not SHA_RE.fullmatch(sha):
            raise SkillsError("the fetched repository has no commit")
        versions = cfg.dir / "versions"
        try:
            if current(cfg).resolve().name == sha and (versions / sha).is_dir():
                return f"skills are up to date ({sha[:7]})"
        except OSError:
            pass
        found = read(fetch)
        # every area is published, also one without a skill yet: an agent may already point at its folder
        areas = sorted(d.name for d in fetch.iterdir() if d.is_dir() and not d.name.startswith("."))
        target, tmp = versions / sha, versions / f".{sha}.{os.getpid()}"
        try:
            versions.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(mode=0o755)
            budget = safecopy.Budget(MAX_FILES, MAX_BYTES)
            for area in areas:
                safecopy.copy(fetch, area, tmp / area, budget)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp, target)
            link = cfg.dir / f".current.{os.getpid()}"
            link.unlink(missing_ok=True)
            os.symlink(f"versions/{sha}", link)
            os.replace(link, current(cfg))
        except safecopy.CopyRefused as e:
            raise SkillsError(str(e)) from None
        except OSError:
            raise SkillsError("the skills could not be written to the published folder") from None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        old = sorted((v for v in versions.iterdir() if v.is_dir() and v.name != sha and SHA_RE.fullmatch(v.name)),
                     key=lambda v: v.stat().st_mtime, reverse=True)
        for v in old[KEEP - 1:]:
            shutil.rmtree(v, ignore_errors=True)
        return f"skills updated to {sha[:7]}: {len(found)} skills in {len(areas)} areas"
    finally:
        shutil.rmtree(fetch, ignore_errors=True)
