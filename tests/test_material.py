import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path

from gregory import safecopy, skills
from gregory.config import SkillsConfig
from gregory.safecopy import Budget, CopyRefused
from gregory.skills import SkillsError

SHA_A, SHA_B = "a" * 40, "b" * 40


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


GOOD = {"podcast/access.toml": 'clients = ["merlin"]\n', "podcast/shownotes/SKILL.md": "# Shownotes\n",
        "podcast/shownotes/references/examples.md": "example\n",
        "dev/access.toml": 'clients = ["anton", "merlin"]\nbackend = "other"\n', "dev/review/SKILL.md": "# Review\n",
        "README.md": "about\n", ".github/x.yml": "ignored\n"}


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class SafeCopyTests(Temp):
    def setUp(self):
        super().setUp()
        self.root = tree(self.tmp / "in", {"a.txt": "hello", "dir/b.txt": "bee", "dir/sub/c.txt": "sea", "dir/.hidden": "x"})
        self.secret = self.tmp / "secret"
        self.secret.write_text("TOKEN")
        self.out = self.tmp / "out"

    def test_copies_a_file_and_a_folder_without_hidden_entries(self):
        safecopy.copy(self.root, "a.txt", self.out / "a.txt", Budget(10, 1000))
        safecopy.copy(self.root, "dir", self.out / "dir", Budget(10, 1000))
        self.assertEqual((self.out / "a.txt").read_text(), "hello")
        self.assertEqual(sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file()),
                         ["a.txt", "dir/b.txt", "dir/sub/c.txt"])

    def test_names_that_leave_the_folder_are_refused(self):
        for name in ("../secret", "/etc/passwd", "dir/../../secret", "", ".hidden", "dir/.hidden", "a\x00b"):
            with self.subTest(name=name), self.assertRaises(CopyRefused):
                safecopy.copy(self.root, name, self.out / "x", Budget(10, 1000))
        self.assertFalse(self.out.exists())

    def test_a_link_is_never_followed(self):
        os.symlink(self.secret, self.root / "link.txt")
        os.symlink(self.tmp, self.root / "linkdir")
        os.symlink(self.secret, self.root / "dir" / "inner")
        for name in ("link.txt", "linkdir/secret", "linkdir", "dir"):
            with self.subTest(name=name), self.assertRaises(CopyRefused):
                safecopy.copy(self.root, name, self.out / name, Budget(10, 1000))
        copied = [p.read_text() for p in self.out.rglob("*") if p.is_file()] if self.out.exists() else []
        self.assertNotIn("TOKEN", copied)

    def test_a_pipe_is_refused_and_does_not_block(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(CopyRefused):
            safecopy.copy(self.root, "pipe", self.out / "pipe", Budget(10, 1000))

    def test_budget_for_count_and_size(self):
        with self.assertRaises(CopyRefused) as e:
            safecopy.copy(self.root, "dir", self.out / "d1", Budget(1, 1000))
        self.assertEqual(str(e.exception), "too many files")
        with self.assertRaises(CopyRefused) as e:
            safecopy.copy(self.root, "dir", self.out / "d2", Budget(10, 4))
        self.assertEqual(str(e.exception), "the files are too large")

    def test_missing_file(self):
        with self.assertRaises(CopyRefused) as e:
            safecopy.copy(self.root, "nope.txt", self.out / "n", Budget(10, 1000))
        self.assertEqual(str(e.exception), "nope.txt: not found")


class SkillTreeTests(Temp):
    def test_reads_areas_and_skills(self):
        found = skills.read(tree(self.tmp, GOOD))
        self.assertEqual([(s.path, sorted(s.clients), s.backend) for s in found],
                         [("dev/review", ["anton", "merlin"], "other"), ("podcast/shownotes", ["merlin"], "")])

    def test_a_client_only_finds_its_own_skills(self):
        found = skills.read(tree(self.tmp, GOOD))
        self.assertEqual(skills.find(found, "merlin", "shownotes").path, "podcast/shownotes")
        self.assertEqual(skills.find(found, "anton", "dev/review").path, "dev/review")
        with self.assertRaises(SkillsError) as e:
            skills.find(found, "anton", "podcast/shownotes")
        self.assertEqual(str(e.exception), "unknown skill. You may use: dev/review")
        with self.assertRaises(SkillsError) as e:
            skills.find(found, "stranger", "review")
        self.assertEqual(str(e.exception), "unknown skill. You may use: none")

    def test_a_bare_name_that_fits_twice_must_be_named_with_its_area(self):
        found = skills.read(tree(self.tmp, {**GOOD, "dev/shownotes/SKILL.md": "x"}))
        with self.assertRaises(SkillsError) as e:
            skills.find(found, "merlin", "shownotes")
        self.assertIn("several skills are called shownotes", str(e.exception))
        self.assertEqual(skills.find(found, "merlin", "dev/shownotes").area, "dev")

    def test_unacceptable_trees(self):
        cases = {
            "no access file": ({"podcast/shownotes/SKILL.md": "x"}, "needs an access.toml"),
            "bad access file": ({"podcast/access.toml": "clients = 3", "podcast/s/SKILL.md": "x"}, "clients must be a list"),
            "broken toml": ({"podcast/access.toml": "clients = [", "podcast/s/SKILL.md": "x"}, "cannot be read"),
            "no SKILL.md": ({"podcast/access.toml": 'clients = ["merlin"]', "podcast/s/notes.md": "x"}, "SKILL.md is missing"),
            "stray file": ({"podcast/access.toml": 'clients = ["merlin"]', "podcast/notes.md": "x"}, "only access.toml and skill folders"),
            "bad name": ({"Podcast/access.toml": 'clients = ["merlin"]'}, "lowercase letters"),
        }
        for label, (files, message) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d, self.assertRaises(SkillsError) as e:
                skills.read(tree(Path(d), files))
            self.assertIn(message, str(e.exception))

    def test_links_in_the_tree_are_refused(self):
        root = tree(self.tmp / "t", GOOD)
        os.symlink("/etc/passwd", root / "podcast" / "shownotes" / "passwd")
        with self.assertRaises(SkillsError) as e:
            skills.read(root)
        self.assertIn("podcast/shownotes/passwd: only plain files and folders", str(e.exception))
        (root / "podcast" / "shownotes" / "passwd").unlink()
        os.symlink(root / "dev", root / "alias")
        with self.assertRaises(SkillsError):
            skills.read(root)


class FakeGit:
    """Stands in for git: `clone` writes a tree to the target, `rev-parse` prints the commit."""

    def __init__(self, files=GOOD, sha=SHA_A, fail=False):
        self.files, self.sha, self.fail, self.calls = files, sha, fail, []

    def __call__(self, cmd, cwd=None, env=None, **kw):
        self.calls.append((cmd, env))
        if "clone" in cmd:
            if self.fail:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: secret detail")
            tree(Path(cmd[-1]), self.files)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, self.sha + "\n", "")


class SyncTests(Temp):
    def setUp(self):
        super().setUp()
        self.cfg = SkillsConfig("git@github.com:owner/agent-skills.git", self.tmp / "published")
        self.cfg.dir.mkdir()
        self.state, self.key = self.tmp / "state", self.tmp / "conf" / "skills-key"

    def sync(self, git):
        return skills.sync(self.cfg, self.key, self.state, run=git, clock=lambda: 0)

    def test_publishes_a_checked_copy_as_a_real_folder(self):
        git = FakeGit()
        self.assertEqual(self.sync(git), "skills updated to aaaaaaa: 2 skills in 2 areas")
        current = skills.current(self.cfg)
        self.assertTrue(current.is_dir() and not current.is_symlink())  # agents resolve the path once: it must stay valid
        self.assertEqual((current / "podcast/shownotes/references/examples.md").read_text(), "example\n")
        self.assertFalse((current / "README.md").exists())  # only the areas are published
        self.assertEqual(sorted(p.name for p in self.cfg.dir.iterdir()), ["current"])
        self.assertFalse((self.state / "skills-fetch").exists())
        self.assertIn("skills updated to aaaaaaa", skills.status(self.state))
        clone, env = git.calls[0]
        self.assertEqual(clone[-3:], ["--", self.cfg.repo, str(self.state / "skills-fetch")])
        self.assertIn(f"-i {self.key}", env["GIT_SSH_COMMAND"])
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])

    def test_an_area_without_a_skill_is_published_too(self):
        self.assertEqual(self.sync(FakeGit(files={"assistant/access.toml": 'clients = ["merlin"]\n', "README.md": "x"})),
                         "skills updated to aaaaaaa: 0 skills in 1 areas")
        self.assertEqual((skills.current(self.cfg) / "assistant/access.toml").read_text(), 'clients = ["merlin"]\n')

    def test_same_commit_changes_nothing(self):
        self.sync(FakeGit())
        self.assertEqual(self.sync(FakeGit()), "skills are up to date (aaaaaaa)")

    def test_a_bad_tree_is_not_published_and_the_old_one_stays(self):
        self.sync(FakeGit())
        with self.assertRaises(SkillsError) as e:
            self.sync(FakeGit(files={"podcast/shownotes/SKILL.md": "x"}, sha=SHA_B))
        self.assertIn("needs an access.toml", str(e.exception))
        self.assertEqual((skills.current(self.cfg) / "dev/review/SKILL.md").read_text(), "# Review\n")
        self.assertEqual(sorted(p.name for p in self.cfg.dir.iterdir()), ["current"])
        self.assertIn("failed:", skills.status(self.state))
        self.assertEqual(self.sync(FakeGit()), "skills are up to date (aaaaaaa)")

    def test_a_failed_fetch_shows_no_details(self):
        with self.assertRaises(SkillsError) as e:
            self.sync(FakeGit(fail=True))
        self.assertNotIn("secret", str(e.exception))

    def test_a_new_commit_replaces_the_folder_under_the_same_path(self):
        self.sync(FakeGit())
        resolved = (skills.current(self.cfg) / "dev").resolve()  # what a long-running agent remembers
        self.sync(FakeGit(files={"dev/access.toml": 'clients = ["anton"]\n', "dev/review/SKILL.md": "v2", "dev/new/SKILL.md": "n"}, sha=SHA_B))
        self.assertEqual((resolved / "review/SKILL.md").read_text(), "v2")
        self.assertTrue((resolved / "new/SKILL.md").is_file())
        self.assertFalse((skills.current(self.cfg) / "podcast").exists())  # a removed area is gone
        self.assertEqual(sorted(p.name for p in self.cfg.dir.iterdir()), ["current"])

    def test_the_link_layout_of_the_first_versions_is_replaced(self):
        (self.cfg.dir / "versions" / SHA_A / "dev").mkdir(parents=True)
        os.symlink(f"versions/{SHA_A}", self.cfg.dir / "current")
        self.sync(FakeGit(sha=SHA_B))
        current = skills.current(self.cfg)
        self.assertTrue(current.is_dir() and not current.is_symlink())
        self.assertEqual(sorted(p.name for p in self.cfg.dir.iterdir()), ["current"])

class CheckScriptTests(Temp):
    """examples/check-skills.py is a copy of the rules for the skills repository itself: both must agree."""

    def check(self, root):
        script = Path(__file__).resolve().parent.parent / "examples" / "check-skills.py"
        return runpy.run_path(str(script))["problems"](root)

    def test_accepts_what_gregory_accepts(self):
        self.assertEqual(self.check(tree(self.tmp, GOOD)), [])

    def test_rejects_what_gregory_rejects(self):
        for files in ({"podcast/shownotes/SKILL.md": "x"}, {"podcast/access.toml": "clients = 3", "podcast/s/SKILL.md": "x"},
                      {"podcast/access.toml": 'clients = ["merlin"]', "podcast/s/notes.md": "x"},
                      {"podcast/access.toml": 'clients = ["merlin"]', "podcast/notes.md": "x"},
                      {"Podcast/access.toml": 'clients = ["merlin"]'}):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as d:
                root = tree(Path(d), files)
                self.assertTrue(self.check(root))
                with self.assertRaises(SkillsError):
                    skills.read(root)

    def test_a_link_is_a_problem(self):
        root = tree(self.tmp, GOOD)
        os.symlink("/etc/passwd", root / "podcast" / "shownotes" / "passwd")
        self.assertEqual(self.check(root),
                         ["podcast/shownotes/passwd: only plain files and folders are allowed"])


if __name__ == "__main__":
    unittest.main()
