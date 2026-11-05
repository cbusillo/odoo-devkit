import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/addon-tests/comparison-base.sh"


class StyleComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-b", "main")
        self.base = self.commit("initial.py", "value = 0\n")
        self.first = self.commit("first.py", "# An offending comment in the earlier change.\nvalue = 1\n")
        self.head = self.commit("second.py", "value = 2\n")

    @staticmethod
    def git(repo: Path, *arguments: str) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        return subprocess.run(
            ["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True, env=environment
        ).stdout.strip()

    def commit(self, filename: str, content: str) -> str:
        (self.source / filename).write_text(content)
        self.git(self.source, "add", filename)
        self.git(self.source, "commit", "-m", filename)
        return self.git(self.source, "rev-parse", "HEAD")

    @staticmethod
    def resolve(repo: Path, base: str, fallback: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPT), str(repo), base, fallback], capture_output=True, text=True)

    def changed_paths(self, repo: Path, base: str) -> set[str]:
        return set(self.git(repo, "diff", "--name-only", base, "HEAD").splitlines())

    def test_exact_source_head_covers_both_commits_and_combined_entries(self) -> None:
        for construction in ("linear", "combined"):
            with self.subTest(construction=construction):
                if construction == "combined":
                    self.git(self.source, "checkout", "--detach", self.base)
                    for entry in (self.first, self.head):
                        self.git(self.source, "merge", "--no-ff", "-m", "candidate entry", entry)
                head = self.git(self.source, "rev-parse", "HEAD")
                self.assertNotIn("first.py", self.changed_paths(self.source, "HEAD^1"))
                result = self.resolve(self.source, self.base)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.changed_paths(self.source, result.stdout.strip()), {"first.py", "second.py"})
                self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), head)

    def test_shallow_checkout_fetches_complete_comparison_history(self) -> None:
        clone = self.root / "clone"
        self.git(self.root, "clone", "--depth=1", self.source.as_uri(), str(clone))
        result = self.resolve(clone, self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changed_paths(clone, result.stdout.strip()), {"first.py", "second.py"})
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), self.head)

    def test_new_branch_push_compares_with_default_branch(self) -> None:
        self.git(self.source, "branch", "candidate")
        self.git(self.source, "update-ref", "refs/heads/main", self.base)
        clone = self.root / "candidate"
        self.git(self.root, "clone", "--depth=1", "--branch=candidate", self.source.as_uri(), str(clone))
        result = self.resolve(clone, "0" * len(self.base), "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changed_paths(clone, result.stdout.strip()), {"first.py", "second.py"})

    def test_missing_comparison_base_refuses_instead_of_using_last_commit(self) -> None:
        for base in ("", "missing-reference"):
            with self.subTest(base=base):
                result = self.resolve(self.source, base)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.stdout.strip())
