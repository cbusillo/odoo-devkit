from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from odoo_devkit.migration_validation import validate_product_migrations


class ProductMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.com")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-m", "Fixture change")
        return self.git("rev-parse", "HEAD")

    def addon(self, relative: str = "group/product", version: str = "1.0") -> Path:
        root = self.repo / "addons" / relative
        root.mkdir(parents=True, exist_ok=True)
        (root / "__init__.py").touch()
        (root / "__manifest__.py").write_text(repr({"name": root.name, "version": version}))
        return root

    @staticmethod
    def migration(root: Path, version: str, directory: str = "migrations") -> None:
        folder = root / directory / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "post-change.py").write_text("def migrate(cr, version):\n    return None\n")

    def test_rejects_future_folder_in_effective_nested_or_direct_addon(self) -> None:
        for relative in ("group/product", "direct"):
            self.migration(self.addon(relative), "20.0.9.0")
            candidate = self.commit()
            with self.assertRaisesRegex(ValueError, f"{Path(relative).name}.*20[.]0[.]9[.]0"):
                validate_product_migrations(self.repo, candidate, "20.0", base=candidate)
            self.git("rm", "-r", "addons")
            self.commit()

    def test_uses_each_introducing_manifest_and_native_version_rules(self) -> None:
        root = self.addon()
        self.migration(root, "0.0.0")
        self.migration(root, "19.0.9.0")
        base = self.commit()
        self.migration(self.addon(version="1.1"), "1.1")
        self.commit()
        self.migration(self.addon(version="1.1.2"), "1.1.2", "upgrades")
        candidate = self.commit()
        validate_product_migrations(self.repo, candidate, "20.0", base=base)

        self.migration(root, "1.2")
        self.commit()
        self.addon(version="1.2")
        candidate = self.commit()
        with self.assertRaisesRegex(ValueError, r"product.*1[.]2"):
            validate_product_migrations(self.repo, candidate, "20.0", base=base)
        validate_product_migrations(self.repo, candidate, "20.0", base=candidate)

        self.migration(root, "20.0.1")
        candidate = self.commit()
        with self.assertRaisesRegex(ValueError, r"product.*20[.]0[.]1"):
            validate_product_migrations(self.repo, candidate, "20.0", base=candidate)

    def test_excludes_shadowed_group_shared_tree_and_unexposed_fixtures(self) -> None:
        self.addon("product")
        for relative in ("group/product", "shared/shared_product", "group/deeper/fixture"):
            self.migration(self.addon(relative), "20.0.9.0")
        candidate = self.commit()
        validate_product_migrations(self.repo, candidate, "20.0", base=candidate)

    def test_fetches_shallow_comparison_and_accepts_two_releases(self) -> None:
        self.addon()
        base = self.commit()
        self.migration(self.addon(version="1.1"), "1.1")
        self.commit()
        self.migration(self.addon(version="1.2"), "1.2")
        candidate = self.commit()
        checkout = self.repo / "shallow"
        self.git("clone", "--depth=1", self.repo.as_uri(), str(checkout))
        self.assertTrue((checkout / ".git" / "shallow").is_file())
        validate_product_migrations(checkout, candidate, "20.0", base=base)
        self.assertFalse((checkout / ".git" / "shallow").is_file())

    def test_missing_comparison_is_reported(self) -> None:
        self.addon()
        self.commit()
        self.migration(self.addon(version="1.1"), "1.1")
        candidate = self.commit()
        with self.assertRaisesRegex(ValueError, "Cannot compare migration history"):
            validate_product_migrations(self.repo, candidate, "20.0", base="missing")

    def test_later_feature_push_still_validates_the_introducing_commit(self) -> None:
        self.addon()
        base = self.commit()
        self.git("remote", "add", "origin", self.repo.as_uri())
        self.git("switch", "-c", "feature")
        self.migration(self.addon(version="1.2"), "1.1")
        first_push = self.commit()
        self.git("commit", "--allow-empty", "-m", "Unrelated second push")
        candidate = self.git("rev-parse", "HEAD")
        event = self.repo / "event.json"
        payload = {"before": first_push, "after": candidate, "repository": {"default_branch": "main"}}
        event.write_text(json.dumps(payload))
        with patch.dict(os.environ, GITHUB_EVENT_NAME="push", GITHUB_EVENT_PATH=str(event), GITHUB_REF="refs/heads/feature"):
            with self.assertRaisesRegex(ValueError, r"product.*1[.]1"):
                validate_product_migrations(self.repo, candidate, "20.0")

        self.git("switch", "main")
        self.git("merge", "--ff-only", "feature")
        with patch.dict(os.environ, GITHUB_EVENT_NAME="push", GITHUB_EVENT_PATH=str(event), GITHUB_REF="refs/heads/main"):
            validate_product_migrations(self.repo, candidate, "20.0")

        payload["pull_request"] = {"head": {"sha": candidate}, "base": {"sha": base}}
        event.write_text(json.dumps(payload))
        with patch.dict(os.environ, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(event), GITHUB_REF="refs/pull/1/merge"):
            with self.assertRaisesRegex(ValueError, r"product.*1[.]1"):
                validate_product_migrations(self.repo, candidate, "20.0")

    def test_merge_only_introduction_is_checked_and_inherited_folder_is_not_rejudged(self) -> None:
        self.addon()
        base = self.commit()
        self.git("switch", "-c", "feature")
        self.migration(self.addon(version="1.1"), "1.1")
        self.commit()
        self.git("switch", "main")
        self.git("commit", "--allow-empty", "-m", "Separate main history")
        self.git("merge", "--no-commit", "--no-ff", "feature")
        self.migration(self.addon(), "1.2")
        self.addon(version="1.2")
        candidate = self.commit()
        validate_product_migrations(self.repo, candidate, "20.0", base=base)
        self.git("switch", "-c", "second")
        self.git("commit", "--allow-empty", "-m", "Separate second history")
        self.git("switch", "main")
        self.git("commit", "--allow-empty", "-m", "Separate main history")
        self.git("merge", "--no-commit", "--no-ff", "second")
        self.migration(self.addon(version="1.2"), "1.1", "upgrades")
        invalid = self.commit()
        with self.assertRaisesRegex(ValueError, r"product.*1[.]1"):
            validate_product_migrations(self.repo, invalid, "20.0", base=candidate)

    def test_introduced_folder_requires_an_eligible_version_advance(self) -> None:
        root = self.addon(version="1.1")
        base = self.commit()
        self.migration(root, "1.1")
        candidate = self.commit()
        with self.assertRaisesRegex(ValueError, "product.*advance"):
            validate_product_migrations(self.repo, candidate, "20.0", base=base)
