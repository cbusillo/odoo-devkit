import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from odoo_devkit.addon_baseline import database_input, file_sha256, producer_input, verify_baseline


class AddonBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binding = self.root / "binding.json"
        self.stage: dict[str, Any] = {
            "synthetic_only": True,
            "database": "synthetic_baseline",
            "postgres_image": "postgres@sha256:" + "a" * 64,
            "postgres_version": "postgres (PostgreSQL) 17.11 (fixture)",
            "architecture": "x86_64",
            "database_identity": "UTF8|en_US.utf8|en_US.utf8|c",
            "settings": {
                "lc_messages": "en_US.utf8",
                "lc_monetary": "en_US.utf8",
                "lc_numeric": "en_US.utf8",
                "lc_time": "en_US.utf8",
                "default_text_search_config": "pg_catalog.english",
                "timezone": "Etc/UTC",
            },
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
            member.type = cast(bytes, tarfile.DIRTYPE)
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
        with self.assertRaises(ValueError):
            self.verify(archive)
        with archive.open("ab") as output:
            output.write(b"changed")
        with self.assertRaises(ValueError):
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
        with self.assertRaises(ValueError):
            self.verify(archive, budget=1)
        with self.assertRaises(ValueError):
            self.verify(self.archive(omit="pgdata/global/pg_control"))
        with self.assertRaises(ValueError):
            self.verify(self.archive(auto_conf=b"primary_conninfo = 'host=external.invalid'\n"))

    def test_equivalent_regenerated_binding_is_accepted(self) -> None:
        archive = self.archive()
        self.binding.write_text(json.dumps(dict(reversed(list(self.stage.items()))), indent=2))
        self.assertEqual(self.verify(archive)["binding"], self.stage)

    def test_malformed_binding_is_refused(self) -> None:
        archive = self.archive()
        for record in [None, [], {}, dict(self.stage, initialization={"schema": None}), dict(self.stage, postgres_version=17)]:
            with self.subTest(record=record):
                self.binding.write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    self.verify(archive)

    def test_producer_binds_dump_and_complete_filestore(self) -> None:
        inputs = self.root / "inputs"
        (inputs / "filestore").mkdir(parents=True)
        dump = inputs / "database.dump"
        dump.write_bytes(b"qualified synthetic dump")
        (inputs / "filestore/a").write_bytes(b"a")
        (inputs / "filestore/z").write_bytes(b"z")
        lines = [
            f"{file_sha256(path)}  {path.relative_to(inputs).as_posix()}\n" for path in sorted((inputs / "filestore").iterdir())
        ]
        self.assertNotEqual(lines, sorted(lines))
        self.stage["initialization"] = {
            "logical_dump": file_sha256(dump),
            "filestore_inventory": hashlib.sha256("".join(sorted(lines)).encode()).hexdigest(),
        }
        self.binding.write_text(json.dumps(self.stage))
        (inputs / "binding.json").write_bytes(self.binding.read_bytes())
        container = producer_input(inputs, binding=self.binding, user="fixture", password="fixture-only")
        self.assertEqual(container["image"], self.stage["postgres_image"])
        self.assertEqual(container["environment"]["BASELINE_DUMP_SHA256"], self.stage["initialization"]["logical_dump"])
        self.assertTrue(all("\n" not in value and "\r" not in value for value in container["environment"].values()))
        (inputs / "filestore/unlisted").write_bytes(b"unexpected")
        with self.assertRaises(ValueError):
            producer_input(inputs, binding=self.binding, user="fixture", password="fixture-only")
        (inputs / "filestore/unlisted").unlink()
        dump.write_bytes(b"different dump")
        with self.assertRaises(ValueError):
            producer_input(inputs, binding=self.binding, user="fixture", password="fixture-only")


if __name__ == "__main__":
    unittest.main()
