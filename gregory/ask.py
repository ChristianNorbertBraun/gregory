"""One request from a client: check it, put its material into a fresh directory, let a backend answer.

The directory holds `skill/` (a copy of the skill the client named) and `files/` (copies of the
files it handed over) and is removed when the request is done. Nothing else reaches the model.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import safecopy, skills as skill_store
from .backends import Ask, BackendError
from .config import AskConfig, HandoffConfig, SkillsConfig

ANSWER_LIMIT = 40_000  # Hermes moves larger tool results into a file


class Refused(Exception):
    """The text is safe to show to the client."""


class Usage:
    """Requests per client and day, kept in a small file so a restart does not reset the count."""

    def __init__(self, path: Path, limit: int, clock: Callable[[], float] = time.time):
        self.path, self.limit, self._clock, self._lock = path, limit, clock, threading.Lock()

    def take(self, client: str) -> int:
        """Count one request. Returns how many are left today; raises Refused when the day's limit is used up."""
        day = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        with self._lock:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = {}
            counts = data.get("counts", {}) if isinstance(data, dict) and data.get("day") == day else {}
            used = counts.get(client, 0) if isinstance(counts.get(client, 0), int) else 0
            if used >= self.limit:
                raise Refused(f"the limit of {self.limit} requests per day is used up; it starts again at midnight")
            counts[client] = used + 1
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": day, "counts": counts}))
            tmp.replace(self.path)
            return self.limit - used - 1


class Asker:
    def __init__(self, cfg: AskConfig, backends: dict, usage: Usage, work_root: Path,
                 skills: SkillsConfig | None = None, handoff: HandoffConfig | None = None, log=print):
        self.cfg, self.backends, self.usage, self.work_root = cfg, backends, usage, work_root
        self.skills, self.handoff, self.log = skills, handoff, log
        self._slots = threading.BoundedSemaphore(cfg.max_parallel)

    def skill_list(self, who: str) -> list[skill_store.Skill]:
        if self.skills is None:
            return []
        try:
            return skill_store.allowed(skill_store.read(skill_store.current(self.skills)), who)
        except skill_store.SkillsError:
            return []

    def ask(self, who: str, prompt, skill=None, files=None) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise Refused("prompt is missing")
        if len(prompt) > self.cfg.max_prompt_chars:
            raise Refused(f"the prompt is longer than {self.cfg.max_prompt_chars} characters; hand large material over as a file")
        if skill is not None and not isinstance(skill, str):
            raise Refused("skill must be the name of a skill")
        files = [] if files is None else files
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise Refused("files must be a list of names")
        if not self._slots.acquire(blocking=False):
            raise Refused("busy with other requests, try again in a few minutes")
        try:
            return self._ask(who, prompt, skill or None, files)
        finally:
            self._slots.release()

    def _ask(self, who: str, prompt: str, skill: str | None, files: list[str]) -> str:
        backend_name, chosen = self.cfg.backend, None
        if skill:
            if self.skills is None:
                raise Refused("no skills are set up")
            root = skill_store.current(self.skills).resolve()  # `current` is our own link to the published version
            try:
                chosen = skill_store.find(skill_store.read(root), who, skill)
            except skill_store.SkillsError as e:
                raise Refused(str(e)) from None
            backend_name = chosen.backend or backend_name
        backend = self.backends.get(backend_name)
        if backend is None:
            raise Refused("no backend is set up for this request")
        if files and self.handoff is None:
            raise Refused("handing over files is not set up")
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        work = Path(tempfile.mkdtemp(prefix="ask-", dir=self.work_root))
        try:
            work.chmod(0o755)
            material = []
            try:
                if chosen:
                    safecopy.copy(root, chosen.path, work / "skill", safecopy.Budget(2000, skill_store.MAX_SKILL_BYTES))
                    material.append("- skill/ : how the asking agent works on this kind of task. Start with skill/SKILL.md.")
                if files:
                    if len(set(files)) != len(files):
                        raise Refused("a file is named twice")
                    budget = safecopy.Budget(self.handoff.max_files, self.handoff.max_mb * 1024 * 1024)
                    for name in files:
                        parts = safecopy.parts_of(name)
                        safecopy.copy(self.handoff.dir / who, name, work.joinpath("files", *parts), budget)
                        material.append(f"- files/{'/'.join(parts)}")
            except safecopy.CopyRefused as e:
                raise Refused(str(e)) from None
            left = self.usage.take(who)
            text = f"[Request from the agent \"{who}\"]\n{prompt.strip()}\n"
            if material:
                text += "\n[Material for this request, in the working directory]\n" + "\n".join(material) + "\n"
            started = time.monotonic()
            try:
                answer = backend.answer(Ask(text, work, self.cfg.timeout_seconds))
            except BackendError as e:
                raise Refused(str(e)) from None
            self.log(f"ask: {who} skill={chosen.path if chosen else '-'} files={len(files)} backend={backend_name} "
                     f"{len(prompt)} -> {len(answer)} chars in {int(time.monotonic() - started)}s, {left} left today")
            if len(answer) > ANSWER_LIMIT:
                answer = answer[:ANSWER_LIMIT] + "\n[the answer was cut here: ask for a shorter one or for parts]"
            return answer
        finally:
            shutil.rmtree(work, ignore_errors=True)
