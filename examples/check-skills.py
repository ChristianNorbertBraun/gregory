#!/usr/bin/env python3
"""Check a skills repository before it is merged: `python3 check-skills.py [directory]`.

Put this file into the skills repository and run it in its CI (and as the check of a coding agent
that proposes changes). It applies the rules Gregory applies before it publishes a new version, so
a change that passes here will be installed. Standard library only.
"""
import os
import re
import sys
import tomllib
from pathlib import Path

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MAX_SKILL_MB = 10


def problems(root: Path) -> list[str]:
    found = []
    for area in sorted(root.iterdir()):
        if area.name.startswith(".") or (not area.is_dir() and not area.is_symlink()):
            continue
        if area.is_symlink():
            found.append(f"{area.name}: links are not allowed")
            continue
        if not NAME.fullmatch(area.name):
            found.append(f"{area.name}/: a folder name is lowercase letters, digits and dashes")
        try:
            access = tomllib.loads((area / "access.toml").read_text())
            clients, backend = access.get("clients"), access.get("backend", "")
            if not isinstance(clients, list) or not all(isinstance(c, str) and NAME.fullmatch(c) for c in clients):
                found.append(f"{area.name}/access.toml: clients must be a list of client names")
            if not isinstance(backend, str) or (backend and not NAME.fullmatch(backend)):
                found.append(f"{area.name}/access.toml: backend must be the name of a backend")
        except FileNotFoundError:
            found.append(f"{area.name}/: a folder at the top level needs an access.toml")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            found.append(f"{area.name}/access.toml cannot be read")
        for skill in sorted(area.iterdir()):
            shown = f"{area.name}/{skill.name}"
            if skill.name.startswith(".") or skill.name == "access.toml":
                continue
            if skill.is_symlink() or not skill.is_dir():
                found.append(f"{shown}: only access.toml and skill folders belong here")
                continue
            if not NAME.fullmatch(skill.name):
                found.append(f"{shown}/: a skill name is lowercase letters, digits and dashes")
            if not (skill / "SKILL.md").is_file():
                found.append(f"{shown}/: SKILL.md is missing")
            size = 0
            for base, dirs, names in os.walk(skill, followlinks=False):
                for name in dirs + names:
                    entry = Path(base) / name
                    if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                        found.append(f"{entry.relative_to(root).as_posix()}: only plain files and folders are allowed")
                    elif name in names:
                        size += entry.stat().st_size
            if size > MAX_SKILL_MB * 2**20:
                found.append(f"{shown}/ is larger than {MAX_SKILL_MB} MB")
    return found


if __name__ == "__main__":
    bad = problems(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    for line in bad:
        print(line)
    print("skills ok" if not bad else f"{len(bad)} problem(s)")
    sys.exit(1 if bad else 0)
