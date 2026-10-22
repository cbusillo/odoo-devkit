import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.git_fixture import commit_fixture_repository, init_fixture_repository


class GitFixtureTests(unittest.TestCase):
    def test_fixture_commit_starts_no_background_git_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_path = Path(temporary_directory) / "repo"
            trace_path = Path(temporary_directory) / "git-trace.log"
            init_fixture_repository(repo_path)
            (repo_path / "README.md").write_text("fixture\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"GIT_TRACE": str(trace_path)}):
                commit_fixture_repository(repo_path)

            started_commands = [line for line in trace_path.read_text().splitlines() if "run_command:" in line]
            self.assertFalse([line for line in started_commands if " maintenance " in line or " gc " in line])


if __name__ == "__main__":
    unittest.main()
