import os
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from unittest import mock

FIXTURE_IDENTITY_CONFIG = (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"))
NO_BACKGROUND_MAINTENANCE_CONFIG = (("maintenance.auto", "false"), ("gc.auto", "0"))


def init_fixture_repository(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo_path)], check=True, capture_output=True)
    for key, value in (*FIXTURE_IDENTITY_CONFIG, *NO_BACKGROUND_MAINTENANCE_CONFIG):
        subprocess.run(["git", "-C", str(repo_path), "config", key, value], check=True, capture_output=True)


def commit_fixture_repository(repo_path: Path, message: str = "fixture") -> None:
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "--quiet", "-m", message], check=True, capture_output=True)


def fixture_git_environment() -> AbstractContextManager[dict[str, str]]:
    environment = {"GIT_CONFIG_COUNT": str(len(NO_BACKGROUND_MAINTENANCE_CONFIG))}
    for index, (key, value) in enumerate(NO_BACKGROUND_MAINTENANCE_CONFIG):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return mock.patch.dict(os.environ, environment)
