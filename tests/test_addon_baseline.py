import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from odoo_devkit.addon_baseline import database_input, file_sha256, verify_baseline


class AddonBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binding = self.root / "binding.json"
        self.stage = {
            "synthetic_only": True,
            "database": "synthetic_baseline",
            "postgres_image": "postgres@sha256:" + "a" * 64,
            "postgres_version": "postgres (PostgreSQL) 17.11 (fixture)",
            "architecture": "x86_64",
            "initialization": {"schema": "b" * 64, "configuration": "c" * 64},
        }
        self.binding.write_text(json.dumps(self.stage))

    def archive(self, extra: tarfile.TarInfo | None = None, *, omit: str = "", auto_conf: bytes = b"# empty\n") -> Path:
        path = self.root / "baseline.tar"
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as output:
            for name, content in {
                "binding.json": self.binding.read_bytes(),
                "pgdata/PG_VERSION": b"17\n",
                "pgdata/global/pg_control": b"synthetic control bytes",
                "pgdata/postgresql.auto.conf": auto_conf,
                "filestore/ab/attachment": b"synthetic attachment",
            }.items():
                if name == omit:
                    continue
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o600
                output.addfile(member, io.BytesIO(content))
            member = tarfile.TarInfo("filestore")
            member.type = tarfile.DIRTYPE
            output.addfile(member)
            if extra is not None:
                output.addfile(extra)
        return path

    def verify(self, archive: Path, *, budget: int = 1024 * 1024) -> dict:
        return verify_baseline(archive, sha256=file_sha256(archive), binding=self.binding, max_expanded_bytes=budget)

    def test_qualified_stage_returns_existing_queue_container_input(self) -> None:
        archive = self.archive()
        verified = self.verify(archive)
        self.assertEqual(verified["binding"], self.stage)
        container = database_input(archive, sha256=file_sha256(archive), binding=self.binding, max_expanded_bytes=1024 * 1024)
        self.assertEqual(container["image"], self.stage["postgres_image"])
        self.assertEqual(Path(container["copies"][0]["source"]), archive.resolve())

    def test_changed_initialization_and_corrupted_archive_are_refused(self) -> None:
        archive = self.archive()
        original = file_sha256(archive)
        self.stage["initialization"]["schema"] = "d" * 64
        self.binding.write_text(json.dumps(self.stage))
        with self.assertRaisesRegex(ValueError, "stage bindings"):
            self.verify(archive)
        with archive.open("ab") as output:
            output.write(b"changed")
        with self.assertRaisesRegex(ValueError, "qualified original"):
            verify_baseline(archive, sha256=original, binding=self.binding, max_expanded_bytes=1024 * 1024)

    def test_unsafe_archive_members_are_refused(self) -> None:
        for name, kind in [
            ("pgdata/../escape", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("pgdata/link", tarfile.SYMTYPE),
            ("pgdata/hardlink", tarfile.LNKTYPE),
            ("pgdata/device", tarfile.CHRTYPE),
            ("pgdata/pipe", tarfile.FIFOTYPE),
            ("pgdata/pg_tblspc/123", tarfile.REGTYPE),
            ("pgdata/postmaster.pid", tarfile.REGTYPE),
            ("pgdata/standby.signal", tarfile.REGTYPE),
            ("pgdata/recovery.signal", tarfile.REGTYPE),
            ("pgdata/backup_label", tarfile.REGTYPE),
            ("pgdata/PG_VERSION", tarfile.REGTYPE),
            ("pgdata/global", tarfile.REGTYPE),
        ]:
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                member.type = kind
                with self.assertRaises(ValueError):
                    self.verify(self.archive(member))

    def test_expansion_missing_state_and_automatic_settings_are_refused(self) -> None:
        archive = self.archive()
        with self.assertRaisesRegex(ValueError, "expanded-byte"):
            self.verify(archive, budget=1)
        with self.assertRaisesRegex(ValueError, "missing"):
            self.verify(self.archive(omit="pgdata/global/pg_control"))
        with self.assertRaisesRegex(ValueError, "automatic PostgreSQL settings"):
            self.verify(self.archive(auto_conf=b"primary_conninfo = 'host=external.invalid'\n"))


if __name__ == "__main__":
    unittest.main()
