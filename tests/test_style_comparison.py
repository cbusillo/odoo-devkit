import json
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
        self.git(self.source, "checkout", "-b", "independent", self.base)
        self.independent = self.commit("second.py", "value = 2\n")
        self.git(self.source, "checkout", "main")

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
    def resolve(
        repo: Path, base: str = "", *, event: str = "", ref: str = "", payload: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
        environment.update(GITHUB_EVENT_NAME=event, GITHUB_REF=ref, GITHUB_EVENT_PATH=str(payload) if payload else "")
        return subprocess.run(["bash", str(SCRIPT), str(repo), base], capture_output=True, text=True, env=environment)

    def event_payload(self, *, before: str = "", pull_request_base: str = "") -> Path:
        payload = self.root / "event.json"
        payload.write_text(
            json.dumps(
                {"repository": {"default_branch": "main"}, "before": before, "pull_request": {"base": {"sha": pull_request_base}}}
            )
        )
        return payload

    def changed_paths(self, repo: Path, base: str) -> set[str]:
        return set(self.git(repo, "diff", "--name-only", f"{base}...HEAD").splitlines())

    def test_exact_source_head_covers_both_commits_and_combined_entries(self) -> None:
        for construction in ("linear", "combined"):
            with self.subTest(construction=construction):
                if construction == "combined":
                    self.git(self.source, "checkout", "--detach", self.base)
                    for entry in (self.first, self.independent):
                        self.git(self.source, "merge", "--no-ff", "-m", "candidate entry", entry)
                head = self.git(self.source, "rev-parse", "HEAD")
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

    def test_candidate_pushes_compare_all_entries_with_default_branch(self) -> None:
        self.git(self.source, "branch", "candidate")
        self.git(self.source, "update-ref", "refs/heads/main", self.base)
        clone = self.root / "candidate"
        self.git(self.root, "clone", "--depth=1", "--branch=candidate", self.source.as_uri(), str(clone))
        for before in ("0" * len(self.base), self.first):
            with self.subTest(before=before):
                payload = self.event_payload(before=before)
                result = self.resolve(clone, event="push", ref="refs/heads/candidate", payload=payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.changed_paths(clone, result.stdout.strip()), {"first.py", "second.py"})

    def test_pull_request_base_can_advance_past_the_source_fork(self) -> None:
        self.git(self.source, "checkout", "-b", "advanced", self.base)
        advanced = self.commit("unrelated.py", "value = 3\n")
        self.git(self.source, "checkout", "main")
        result = self.resolve(self.source, event="pull_request", payload=self.event_payload(pull_request_base=advanced))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changed_paths(self.source, result.stdout.strip()), {"first.py", "second.py"})

    def test_default_branch_push_uses_its_previous_commit(self) -> None:
        result = self.resolve(self.source, event="push", ref="refs/heads/main", payload=self.event_payload(before=self.first))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changed_paths(self.source, result.stdout.strip()), {"second.py"})

    def test_schedule_discovers_remote_default_without_event_repository(self) -> None:
        clone = self.root / "scheduled"
        self.git(self.root, "clone", "--depth=1", self.source.as_uri(), str(clone))
        payload = self.root / "schedule.json"
        payload.write_text("{}")
        result = self.resolve(clone, event="schedule", payload=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.changed_paths(clone, result.stdout.strip()))

    def test_unavailable_comparison_history_refuses_instead_of_using_last_commit(self) -> None:
        clone = self.root / "clone"
        self.git(self.root, "clone", self.source.as_uri(), str(clone))
        for repo, base in ((self.source, ""), (clone, "missing-reference")):
            with self.subTest(repo=repo, base=base):
                result = self.resolve(repo, base)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.stdout.strip())
