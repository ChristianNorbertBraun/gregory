"""Paths and the config file of the service user.

Everything lives in the XDG directories of that user: config and credentials in
$XDG_CONFIG_HOME/gregory (default ~/.config/gregory), state in $XDG_STATE_HOME/gregory
(default ~/.local/state/gregory). Set the two variables in the service unit to move them.

config.toml:

    [update]
    repo = "your-name/gregory"       # where the releases are
    publisher = "your-github-login"  # only releases published by this login are installed

    [server]
    port = 8768                  # optional

    [ask]
    backend = "claude"           # which table under [backends] answers when a skill's area names none
    daily_limit = 30             # optional: requests per client and day
    timeout_seconds = 240        # optional: how long one request may take
    max_prompt_chars = 200000    # optional
    max_parallel = 2             # optional: requests answered at the same time

    [backends.claude]            # any number; the client never names one
    type = "claude-code"         # see backends.py for the types and their settings
    model = "opus"

    [skills]                     # optional: skills the clients may name in a request (see skills.py)
    repo = "git@github.com:your-name/agent-skills.git"
    branch = "main"              # optional
    dir = "/srv/agent-skills"    # where the checked copy is published (readable by the agents, too)

    [handoff]                    # optional: files a client hands over for one request (see handoff.py)
    dir = "/srv/handoff"         # a client's files are in <dir>/<client name>/
    max_files = 20               # optional
    max_mb = 25                  # optional: all files of one request together
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PORT = 8768
CLIENT = "merlin"  # name of the first chat client; a client's token file is token-<name>
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REPO_RE = re.compile(r"(git@github\.com:|https://github\.com/)[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\.git")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class UpdateConfig:
    repo: str
    publisher: str


@dataclass(frozen=True)
class AskConfig:
    backend: str = ""
    daily_limit: int = 30
    timeout_seconds: int = 240
    max_prompt_chars: int = 200_000
    max_parallel: int = 2


@dataclass(frozen=True)
class SkillsConfig:
    repo: str
    dir: Path
    branch: str = "main"


@dataclass(frozen=True)
class HandoffConfig:
    dir: Path
    max_files: int = 20
    max_mb: int = 25


@dataclass(frozen=True)
class Paths:
    conf: Path
    state: Path

    @classmethod
    def default(cls, env=os.environ, home: Path | None = None) -> "Paths":
        home = home or Path.home()

        def base(var: str, fallback: str) -> Path:
            value = env.get(var, "")
            return Path(value) if value.startswith("/") else home / fallback  # XDG: relative values are ignored

        return cls(base("XDG_CONFIG_HOME", ".config") / "gregory", base("XDG_STATE_HOME", ".local/state") / "gregory")

    @property
    def token(self) -> Path:
        return self.conf / f"token-{CLIENT}"

    @property
    def config(self) -> Path:
        return self.conf / "config.toml"

    @property
    def skills_key(self) -> Path:
        return self.conf / "skills-key"

    def tokens(self) -> dict[str, str]:
        """Every chat client with a token file: {client name: token}."""
        found = {p.name[len("token-"):]: load_token(p) for p in sorted(self.conf.glob("token-*")) if p.is_file()}
        if not found:
            raise ConfigError(f"no token file (token-<client>) in {self.conf.name}")
        for name in found:
            if not NAME_RE.fullmatch(name):
                raise ConfigError(f"token-{name}: a client name is lowercase letters, digits and dashes")
        return found


def _load(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path.name}: {type(e).__name__}") from None


def _table(data: dict, key: str) -> dict | None:
    section = data.get(key)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"[{key}] must be a table")
    return section


def _int(section: dict, table: str, key: str, default: int, low: int, high: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"[{table}] {key} must be a whole number from {low} to {high}")
    return value


def _dir(section: dict, table: str) -> Path:
    value = section.get("dir")
    if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
        raise ConfigError(f"[{table}] dir must be an absolute path")
    return Path(value)


def load_update(path: Path) -> UpdateConfig | None:
    """The [update] section, or None when updates are not configured."""
    section = _table(_load(path), "update")
    if section is None:
        return None
    repo, publisher = section.get("repo"), section.get("publisher")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", repo):
        raise ConfigError("[update] repo must look like owner/name")
    if not isinstance(publisher, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", publisher):
        raise ConfigError("[update] publisher must be a GitHub login")
    return UpdateConfig(repo, publisher)


def load_port(path: Path) -> int:
    return _int(_table(_load(path), "server") or {}, "server", "port", PORT, 1024, 65535)


def load_ask(path: Path) -> AskConfig:
    section = _table(_load(path), "ask") or {}
    backend = section.get("backend", "")
    if not isinstance(backend, str) or (backend and not NAME_RE.fullmatch(backend)):
        raise ConfigError("[ask] backend must be the name of a table under [backends]")
    return AskConfig(backend,
                     _int(section, "ask", "daily_limit", 30, 1, 10_000),
                     _int(section, "ask", "timeout_seconds", 240, 10, 1800),
                     _int(section, "ask", "max_prompt_chars", 200_000, 100, 900_000),
                     _int(section, "ask", "max_parallel", 2, 1, 8))


def load_backend_tables(path: Path) -> dict[str, dict]:
    """The tables under [backends], unchecked: backends.load() knows what each type needs."""
    tables = _table(_load(path), "backends") or {}
    for name, table in tables.items():
        if not NAME_RE.fullmatch(name) or not isinstance(table, dict):
            raise ConfigError(f"[backends.{name}] must be a table with a lowercase name")
    return tables


def load_skills(path: Path) -> SkillsConfig | None:
    section = _table(_load(path), "skills")
    if section is None:
        return None
    repo, branch = section.get("repo"), section.get("branch", "main")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise ConfigError("[skills] repo must be a GitHub address ending in .git (git@github.com:owner/name.git)")
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", branch):
        raise ConfigError("[skills] branch must be a branch name")
    return SkillsConfig(repo, _dir(section, "skills"), branch)


def load_handoff(path: Path) -> HandoffConfig | None:
    section = _table(_load(path), "handoff")
    if section is None:
        return None
    return HandoffConfig(_dir(section, "handoff"), _int(section, "handoff", "max_files", 20, 1, 500),
                         _int(section, "handoff", "max_mb", 25, 1, 500))


def load_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
    except OSError:
        raise ConfigError(f"cannot read {path.name}") from None
    if len(token) < 32:
        raise ConfigError(f"{path.name} is too short to be a token")
    return token
