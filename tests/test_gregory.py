import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from gregory import backends, config, proc, sandbox
from gregory.ask import ANSWER_LIMIT, Asker, Refused, Usage
from gregory.backends import Ask, BackendError, ClaudeCode
from gregory.bridge import Bridge, make_handler
from gregory.config import AskConfig, ConfigError, HandoffConfig, SkillsConfig

TOKEN = "sk-test-" + "t" * 40


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class FakeBackend:
    def __init__(self, reply="the answer", error=None):
        self.reply, self.error, self.asks, self.seen = reply, error, [], {}

    def answer(self, ask: Ask) -> str:
        self.asks.append(ask)
        self.seen = {p.relative_to(ask.work).as_posix(): p.read_text() for p in ask.work.rglob("*") if p.is_file()}
        self.mode = oct(ask.work.stat().st_mode & 0o777)
        if self.error:
            raise BackendError(self.error)
        return self.reply


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.logged = []
        published = tree(self.tmp / "published" / "versions" / "one", {
            "podcast/access.toml": 'clients = ["merlin"]\n', "podcast/shownotes/SKILL.md": "RULES\n",
            "podcast/shownotes/references/ex.md": "EXAMPLE\n",
            "dev/access.toml": 'clients = ["anton"]\nbackend = "second"\n', "dev/review/SKILL.md": "REVIEW\n"})
        os.symlink("versions/one", published.parent.parent / "current")
        self.skills = SkillsConfig("git@github.com:o/s.git", self.tmp / "published")
        self.handoff = HandoffConfig(tree(self.tmp / "handoff", {"merlin/notes.md": "NOTES", "merlin/pack/a.txt": "A",
                                                                 "anton/own.txt": "ANTON"}), max_files=3, max_mb=1)
        self.first, self.second = FakeBackend(), FakeBackend("from the second")

    def asker(self, limit=30, **kw):
        cfg = AskConfig(backend="first", daily_limit=limit, max_prompt_chars=1000, **kw)
        return Asker(cfg, {"first": self.first, "second": self.second}, Usage(self.tmp / "state/usage.json", limit, lambda: 0),
                     self.tmp / "state/work", skills=self.skills, handoff=self.handoff, log=self.logged.append)


class AskTests(Base):
    def test_plain_prompt(self):
        self.assertEqual(self.asker().ask("merlin", "What is 2+2?"), "the answer")
        ask = self.first.asks[0]
        self.assertEqual(ask.prompt, '[Request from the agent "merlin"]\nWhat is 2+2?\n')
        self.assertEqual((self.first.seen, ask.timeout), ({}, 240))
        self.assertFalse(ask.work.exists())  # removed after the request
        self.assertIn("ask: merlin skill=- files=0 backend=first", self.logged[0])
        self.assertNotIn("2+2", " ".join(self.logged))  # the log never holds what was asked

    def test_skill_and_files_are_copied_for_this_request_only(self):
        self.asker().ask("merlin", "Write it", skill="shownotes", files=["notes.md", "pack"])
        self.assertEqual(self.first.seen, {"skill/SKILL.md": "RULES\n", "skill/references/ex.md": "EXAMPLE\n",
                                           "files/notes.md": "NOTES", "files/pack/a.txt": "A"})
        prompt = self.first.asks[0].prompt
        self.assertIn("[Material for this request, in the working directory]\n- skill/ :", prompt)
        self.assertIn("- files/notes.md\n- files/pack\n", prompt)
        self.assertEqual(self.first.mode, "0o755")
        self.assertEqual(list((self.tmp / "state/work").iterdir()), [])

    def test_the_area_of_a_skill_can_pick_another_backend(self):
        self.assertEqual(self.asker().ask("anton", "Look", skill="dev/review"), "from the second")
        self.assertEqual(self.first.asks, [])

    def test_a_client_cannot_name_a_skill_of_another_area(self):
        with self.assertRaises(Refused) as e:
            self.asker().ask("anton", "x", skill="podcast/shownotes")
        self.assertEqual(str(e.exception), "unknown skill. You may use: dev/review")
        self.assertEqual(self.first.asks + self.second.asks, [])

    def test_a_client_only_reaches_its_own_handoff_folder(self):
        for name in ("own.txt", "../anton/own.txt", "/etc/passwd"):
            with self.subTest(name=name), self.assertRaises(Refused):
                self.asker().ask("merlin", "x", files=[name])
        os.symlink(self.handoff.dir / "anton" / "own.txt", self.handoff.dir / "merlin" / "link.txt")
        with self.assertRaises(Refused):
            self.asker().ask("merlin", "x", files=["link.txt"])
        self.assertEqual(self.first.asks, [])

    def test_refused_requests(self):
        asker = self.asker()
        for args, message in ((("merlin", ""), "prompt is missing"), (("merlin", None), "prompt is missing"),
                              (("merlin", "x" * 1001), "longer than 1000 characters"),
                              (("merlin", "x", 3), "skill must be the name"),
                              (("merlin", "x", None, "notes.md"), "files must be a list"),
                              (("merlin", "x", None, ["notes.md", "notes.md"]), "named twice"),
                              (("merlin", "x", None, ["notes.md", "pack", "missing.md"]), "missing.md: not found")):
            with self.subTest(message), self.assertRaises(Refused) as e:
                asker.ask(*args)
            self.assertIn(message, str(e.exception))
        self.assertEqual(self.first.asks, [])

    def test_too_many_files(self):
        tree(self.handoff.dir / "merlin", {f"many/{i}.txt": "x" for i in range(5)})
        with self.assertRaises(Refused) as e:
            self.asker().ask("merlin", "x", files=["many"])
        self.assertEqual(str(e.exception), "too many files")

    def test_daily_limit_is_per_client_and_survives_a_restart(self):
        self.asker(limit=2).ask("merlin", "one")
        self.asker(limit=2).ask("merlin", "two")
        with self.assertRaises(Refused) as e:
            self.asker(limit=2).ask("merlin", "three")
        self.assertIn("limit of 2 requests per day", str(e.exception))
        self.assertEqual(self.asker(limit=2).ask("anton", "other client"), "the answer")
        self.assertEqual(len(self.first.asks), 3)

    def test_a_refused_request_does_not_count(self):
        asker = self.asker(limit=1)
        with self.assertRaises(Refused):
            asker.ask("merlin", "x", skill="nope")
        self.assertEqual(asker.ask("merlin", "x"), "the answer")

    def test_a_new_day_starts_a_new_count(self):
        day = [0]
        usage = Usage(self.tmp / "u.json", 1, lambda: day[0])
        self.assertEqual(usage.take("merlin"), 0)
        with self.assertRaises(Refused):
            usage.take("merlin")
        day[0] = 3 * 86400
        self.assertEqual(usage.take("merlin"), 0)

    def test_backend_failure_is_passed_on_and_the_directory_is_removed(self):
        self.first.error = "no answer within 240 seconds"
        with self.assertRaises(Refused) as e:
            self.asker().ask("merlin", "x", skill="shownotes")
        self.assertEqual(str(e.exception), "no answer within 240 seconds")
        self.assertEqual(list((self.tmp / "state/work").iterdir()), [])

    def test_only_so_many_requests_at_once(self):
        asker = self.asker(max_parallel=1)
        inner = []

        class Reentrant(FakeBackend):
            def answer(me, ask):
                try:
                    asker.ask("anton", "second while the first runs")
                except Refused as e:
                    inner.append(str(e))
                return "done"

        asker.backends["first"] = Reentrant()
        self.assertEqual(asker.ask("merlin", "first"), "done")
        self.assertEqual(inner, ["busy with other requests, try again in a few minutes"])

    def test_a_very_long_answer_is_cut(self):
        self.first.reply = "x" * (ANSWER_LIMIT + 10)
        answer = self.asker().ask("merlin", "x")
        self.assertTrue(answer.startswith("x" * ANSWER_LIMIT + "\n[the answer was cut here"))

    def test_no_backend(self):
        cfg = AskConfig()
        asker = Asker(cfg, {}, Usage(self.tmp / "u.json", 5), self.tmp / "work")
        with self.assertRaises(Refused) as e:
            asker.ask("merlin", "x")
        self.assertEqual(str(e.exception), "no backend is set up for this request")
        with self.assertRaises(Refused) as e:
            asker.ask("merlin", "x", skill="s")
        self.assertEqual(str(e.exception), "no skills are set up")


class FakeRun:
    def __init__(self, out=None, code=0, truncated=False):
        self.out = json.dumps({"type": "result", "is_error": False, "result": "Four."}) if out is None else out
        self.code, self.truncated, self.calls = code, truncated, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return self.code, self.out, self.truncated


class ClaudeCodeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf = Path(self._tmp.name)
        tree(self.conf, {"claude/token": TOKEN + "\n", "bin/claude": "#!/bin/sh\n"})
        self.addCleanup(setattr, sandbox, "available", sandbox.available)
        sandbox.available = lambda: True

    def backend(self, run=None, **settings):
        self.run = run or FakeRun()
        return ClaudeCode("claude", {"type": "claude-code", "binary": str(self.conf / "bin/claude"), **settings},
                          self.conf, run=self.run)

    def test_runs_the_program_in_the_sandbox_with_read_only_tools(self):
        answer = self.backend().answer(Ask("--help me", Path("/srv/work"), 77))
        self.assertEqual(answer, "Four.")
        cmd, kw = self.run.calls[0]
        self.assertEqual(cmd[0], "bwrap")
        self.assertNotIn("--help me", cmd)  # the prompt travels on standard input, never as an argument
        self.assertEqual((kw["stdin_text"], kw["timeout"]), ("--help me", 77))
        self.assertEqual(kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.assertNotIn(TOKEN, " ".join(cmd))
        binds = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--bind"]
        self.assertEqual(binds, [])  # nothing from the host is writable
        ro = [(cmd[i + 1], cmd[i + 2]) for i, a in enumerate(cmd) if a == "--ro-bind"]
        self.assertIn(("/srv/work", "/work"), ro)
        self.assertIn((str((self.conf / "bin/claude").resolve()), sandbox.MODEL_BIN), ro)
        self.assertTrue(all(src in ("/usr", "/srv/work") or src.startswith("/etc/") or src.endswith("bin/claude")
                            for src, _ in ro), ro)
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        denied = cmd[cmd.index("--disallowedTools") + 1:]
        self.assertEqual(allowed, ["Read", "Glob", "Grep", "WebSearch", "WebFetch"])
        for tool in ("Bash", "Edit", "Write"):
            self.assertIn(tool, denied)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep,WebSearch,WebFetch")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_web_can_be_switched_off(self):
        cmd = self.backend(web=False, model="sonnet", max_turns=5).argv()
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read", "Glob", "Grep"])
        self.assertIn("WebFetch", cmd[cmd.index("--disallowedTools") + 1:])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual((cmd[cmd.index("--model") + 1], cmd[cmd.index("--max-turns") + 1]), ("sonnet", "5"))

    def test_the_token_never_leaves_in_an_answer(self):
        run = FakeRun(json.dumps({"is_error": False, "result": f"your token is {TOKEN}!"}))
        self.assertEqual(self.backend(run).answer(Ask("x", Path("/w"), 10)), "your token is [removed]!")

    def test_failures_have_safe_messages(self):
        cases = {"timeout": (FakeRun(code=proc.TIMED_OUT), "no answer within 10 seconds"),
                 "too large": (FakeRun(truncated=True), "the answer was too large"),
                 "garbage": (FakeRun("Traceback /home/gregory/.config secret", code=1), "the model program failed (exit 1)"),
                 "error result": (FakeRun(json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"})),
                                  "the model gave no answer (error_max_turns)"),
                 "empty": (FakeRun(json.dumps({"is_error": False, "result": " "})), "the model gave no answer (error)")}
        for label, (run, message) in cases.items():
            with self.subTest(label), self.assertRaises(BackendError) as e:
                self.backend(run).answer(Ask("x", Path("/w"), 10))
            self.assertEqual(str(e.exception), message)

    def test_a_warning_line_before_the_json_is_tolerated(self):
        run = FakeRun("some notice\n" + json.dumps({"is_error": False, "result": "ok"}))
        self.assertEqual(self.backend(run).answer(Ask("x", Path("/w"), 10)), "ok")

    def test_without_token_or_sandbox_nothing_runs(self):
        (self.conf / "claude/token").unlink()
        with self.assertRaises(BackendError) as e:
            self.backend().answer(Ask("x", Path("/w"), 10))
        self.assertEqual(str(e.exception), "the model is not signed in")
        sandbox.available = lambda: False
        with self.assertRaises(BackendError):
            self.backend().answer(Ask("x", Path("/w"), 10))
        self.assertEqual(self.run.calls, [])

    def test_check_and_settings(self):
        self.backend().check()
        with self.assertRaises(ConfigError):
            ClaudeCode("claude", {"binary": "/nonexistent/claude"}, self.conf).check()
        for bad in ({"binary": "claude"}, {"model": "a b"}, {"web": "yes"}, {"max_turns": 0}, {"colour": "red"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                ClaudeCode("claude", bad, self.conf)
        self.assertEqual(ClaudeCode("c", {}, self.conf, home=Path("/home/x")).binary, Path("/home/x/.local/bin/claude"))

    def test_load_knows_the_types(self):
        loaded = backends.load({"claude": {"type": "claude-code"}}, self.conf)
        self.assertIsInstance(loaded["claude"], ClaudeCode)
        with self.assertRaises(ConfigError):
            backends.load({"x": {"type": "telepathy"}}, self.conf)


class BridgeTests(Base):
    def call(self, bridge, name, args=None, who="merlin"):
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": args or {}}}, who)
        return out["result"]["content"][0]["text"], out["result"]["isError"]

    def bridge(self, **kw):
        return Bridge(self.asker(), log=self.logged.append, skills=self.skills, **kw)

    def test_lists_five_tools_and_initializes(self):
        bridge = self.bridge()
        tools = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["gregory_ask", "gregory_list_skills", "gregory_update_skills",
                                                      "gregory_update_check", "gregory_update_apply"])
        init = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "gregory")
        self.assertIsNone(bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ask_through_the_tool(self):
        text, error = self.call(self.bridge(), "gregory_ask", {"prompt": "Write it", "skill": "shownotes", "files": ["notes.md"]})
        self.assertEqual((text, error), ("the answer", False))
        self.assertIn("files/notes.md", self.first.seen)

    def test_the_client_is_the_token_holder_not_an_argument(self):
        text, error = self.call(self.bridge(), "gregory_ask", {"prompt": "x", "skill": "podcast/shownotes"}, who="anton")
        self.assertTrue(error)
        self.assertEqual(text, "rejected: unknown skill. You may use: dev/review")
        text, error = self.call(self.bridge(), "gregory_ask", {"prompt": "x", "client": "merlin"}, who="anton")
        self.assertEqual(text, "rejected: unknown argument: client")

    def test_list_skills_shows_only_the_clients_own(self):
        text, _ = self.call(self.bridge(skills_status=lambda: "2026-01-01 10:00 skills updated"), "gregory_list_skills")
        self.assertEqual(text, "Skills you may name in gregory_ask:\n- podcast/shownotes\nLast refresh: 2026-01-01 10:00 skills updated")
        text, _ = self.call(self.bridge(), "gregory_list_skills", who="stranger")
        self.assertEqual(text, "Skills you may name in gregory_ask:\n(none)")

    def test_update_skills(self):
        text, error = self.call(self.bridge(sync_skills=lambda: "skills updated to abc1234: 2 skills in 2 areas"), "gregory_update_skills")
        self.assertEqual((text, error), ("skills updated to abc1234: 2 skills in 2 areas", False))
        text, error = self.call(self.bridge(), "gregory_update_skills")
        self.assertEqual((text, error), ("rejected: no skills repository is set up", True))

    def test_unexpected_failure_does_not_leak_details(self):
        def boom():
            raise RuntimeError("secret path /home/gregory/.config/gregory/claude/token")

        text, error = self.call(self.bridge(sync_skills=boom), "gregory_update_skills")
        self.assertEqual((text, error), ("failed: RuntimeError", True))
        self.assertNotIn("secret", " ".join(self.logged))

    def test_unknown_tool_and_method(self):
        bridge = self.bridge()
        out = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "gregory_delete"}})
        self.assertEqual(out["error"]["code"], -32602)
        self.assertEqual(bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)


class FakeConnection:
    """Stands in for the client socket, so the request handler runs without any network."""

    def __init__(self, raw: bytes):
        self._in, self.sent = io.BytesIO(raw), io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self._in if "r" in mode else self.sent

    def sendall(self, data):
        self.sent.write(data)


class HttpTest(Base):
    """The HTTP guards, exercised on the handler directly: a sandbox may block loopback connections."""

    def setUp(self):
        super().setUp()
        self.handler = make_handler(Bridge(self.asker(), log=lambda *_: None, skills=self.skills),
                                    {"merlin": "T" * 40, "anton": "A" * 40})

    def request(self, method="POST", path="/mcp", token="T" * 40, body=None, headers=None):
        payload = json.dumps(body if body is not None else {"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        hdrs = {"Host": "127.0.0.1:8768", "Content-Type": "application/json", "Content-Length": str(len(payload))}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        raw = f"{method} {path} HTTP/1.1\r\n".encode() + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()).encode() + b"\r\n" + payload
        conn = FakeConnection(raw)
        self.handler(conn, ("127.0.0.1", 50000), None)
        head, _, rest = conn.sent.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), rest

    def test_ping_with_token(self):
        status, body = self.request()
        self.assertEqual((status, json.loads(body)["result"]), (200, {}))

    def test_wrong_or_missing_token(self):
        self.assertEqual(self.request(token="x" * 40)[0], 401)
        self.assertEqual(self.request(token=None)[0], 401)

    def test_foreign_host_and_browser_origin_are_blocked(self):
        self.assertEqual(self.request(headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "https://evil.example"})[0], 403)

    def test_other_paths_and_methods(self):
        self.assertEqual(self.request(path="/other")[0], 404)
        self.assertEqual(self.request(method="GET")[0], 405)

    def test_the_token_decides_who_asks(self):
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "gregory_list_skills", "arguments": {}}}
        self.assertIn("- podcast/shownotes", json.loads(self.request(body=call)[1])["result"]["content"][0]["text"])
        self.assertIn("- dev/review", json.loads(self.request(body=call, token="A" * 40)[1])["result"]["content"][0]["text"])

    def test_a_long_prompt_fits_and_a_huge_body_does_not(self):
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "gregory_ask", "arguments": {"prompt": "x" * 900}}}
        self.assertEqual(self.request(body=call)[0], 200)
        self.assertEqual(self.request(body={"pad": "x" * 1_100_000})[0], 413)

    def test_notification_only_gets_202(self):
        self.assertEqual(self.request(body={"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)


class ConfigTests(unittest.TestCase):
    def load(self, text, loader):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text)
            return loader(path)

    def test_defaults_without_a_file(self):
        missing = Path("/nonexistent/config.toml")
        self.assertEqual(config.load_ask(missing), AskConfig())
        self.assertEqual(config.load_port(missing), 8768)
        self.assertIsNone(config.load_skills(missing))
        self.assertIsNone(config.load_handoff(missing))
        self.assertEqual(config.load_backend_tables(missing), {})

    def test_a_full_file(self):
        text = ('[server]\nport = 9000\n[ask]\nbackend = "claude"\ndaily_limit = 5\ntimeout_seconds = 60\n'
                '[backends.claude]\ntype = "claude-code"\n[skills]\nrepo = "git@github.com:me/agent-skills.git"\n'
                'dir = "/srv/agent-skills"\n[handoff]\ndir = "/srv/handoff"\nmax_mb = 3\n')
        self.assertEqual(self.load(text, config.load_port), 9000)
        self.assertEqual(self.load(text, config.load_ask), AskConfig("claude", 5, 60))
        self.assertEqual(self.load(text, config.load_skills), SkillsConfig("git@github.com:me/agent-skills.git", Path("/srv/agent-skills")))
        self.assertEqual(self.load(text, config.load_handoff), HandoffConfig(Path("/srv/handoff"), 20, 3))
        self.assertEqual(self.load(text, config.load_backend_tables), {"claude": {"type": "claude-code"}})

    def test_bad_values(self):
        cases = (('[ask]\ndaily_limit = 0', config.load_ask), ('[ask]\nbackend = "A B"', config.load_ask),
                 ('[ask]\ntimeout_seconds = true', config.load_ask), ('[server]\nport = 80', config.load_port),
                 ('[skills]\nrepo = "--upload-pack=x"\ndir = "/srv/s"', config.load_skills),
                 ('[skills]\nrepo = "git@github.com:me/s.git"\ndir = "relative"', config.load_skills),
                 ('[skills]\nrepo = "git@github.com:me/s.git"\ndir = "/srv/s"\nbranch = "-x"', config.load_skills),
                 ('[handoff]\ndir = "/srv/../etc"', config.load_handoff), ('[backends]\nBad = 3', config.load_backend_tables),
                 ('ask = 3', config.load_ask), ('[ask', config.load_ask))
        for text, loader in cases:
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load(text, loader)

    def test_client_names_come_from_the_token_files(self):
        with tempfile.TemporaryDirectory() as d:
            paths = config.Paths(Path(d), Path(d))
            with self.assertRaises(ConfigError):
                paths.tokens()
            (Path(d) / "token-merlin").write_text("m" * 40)
            (Path(d) / "token-jared").write_text("j" * 40)
            self.assertEqual(paths.tokens(), {"jared": "j" * 40, "merlin": "m" * 40})
            (Path(d) / "token-Bad Name").write_text("b" * 40)
            with self.assertRaises(ConfigError):
                paths.tokens()


if __name__ == "__main__":
    unittest.main()
