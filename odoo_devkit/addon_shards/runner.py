from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import ExitStack, suppress
from pathlib import Path

from .inventory import Inventory, read_inventory, same_inventory, split_classes

EVIDENCE = Path("/tmp/addon-results")


class ParallelRun:
    def __init__(self, arguments: list[str], expected: Inventory, installed: list[str], work: Path) -> None:
        self.arguments = arguments
        self.expected = expected
        self.installed = installed
        self.work = work
        self.seed = self.option("--database")
        self.seed_data = Path(self.option("--data-dir"))
        self.timeout = int(self.option("--limit-time-real"))
        self.deadline = time.monotonic() + self.timeout
        self.groups = split_classes(expected)
        self.connection = ["-h", self.option("--db_host"), "-U", self.option("--db_user")]

    def option(self, name: str) -> str:
        return self.arguments[self.arguments.index(name) + 1]

    def database(self, part: str) -> str:
        return self.seed if part == "seed" else f"{self.seed}_{part}"

    def postgres(self, program: str, *args: str) -> str:
        return subprocess.run(
            [program, *self.connection, *args], check=True, capture_output=True, text=True, timeout=30
        ).stdout.strip()

    def query(self, database: str, sql: str) -> list[str]:
        return self.postgres("psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-d", database, "-c", sql).splitlines()

    def start(self, part: str, streams: ExitStack) -> subprocess.Popen:
        args = list(self.arguments)
        if part != "seed":
            db = self.database(part)
            init = args.index("--init")
            del args[init : init + 2]
            args[args.index("--database") + 1] = db
            args[args.index("--data-dir") + 1] = str(self.work / db)
            args.extend(["--http-port", str(8069 + int(part))])
        temp = self.work / part
        temp.mkdir()
        environment = {
            **os.environ,
            "TMPDIR": str(temp),
            "ADDON_TEST_WORK": str(self.work),
            "ADDON_TEST_PART": part,
            "ODOO_SOURCE_BIN": str(Path(__file__).with_name("suite.py")),
        }
        output = streams.enter_context((EVIDENCE / f"{part}.log").open("x"))
        return subprocess.Popen(
            ["/odoo/odoo-bin", *args], stdout=output, stderr=subprocess.STDOUT, env=environment, start_new_session=True
        )

    def validate(self, part: str) -> None:
        observed = read_inventory((EVIDENCE / f"{part}.log").read_text())
        db = self.database(part)
        if observed.database != db:
            raise ValueError(f"Worker {part} reported database {observed.database}, expected {db}")
        at_install = self.expected.at_install if part == "seed" else []
        post_install = [] if part == "seed" else self.groups[int(part)]
        same_inventory(observed.at_install, at_install, "at-install")
        same_inventory(observed.post_install, post_install, "post-install")
        wrappers = {f"odoo.addons.{test}" for test in at_install + post_install}
        same_inventory(observed.hoot, [test for test in self.expected.hoot if test.split(" ", 1)[0] in wrappers], "HOOT")
        same_inventory(observed.tours, [tour for tour in self.expected.tours if tour in wrappers], "tours")
        installed = self.query(db, "SELECT name FROM ir_module_module WHERE state='installed' ORDER BY name")
        same_inventory(installed, self.installed, "installed modules")

    def run_parts(self, parts: list[str]) -> None:
        processes = {}
        with ExitStack() as streams:
            try:
                for part in parts:
                    processes[part] = self.start(part, streams)
                pending = set(parts)
                while pending:
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError(f"Shared {self.timeout}s addon deadline expired with {sorted(pending)} running")
                    for part in sorted(pending):
                        code = processes[part].poll()
                        if code is not None:
                            if code:
                                raise ValueError(f"Addon worker {part} exited {code}")
                            self.validate(part)
                            pending.remove(part)
                    if pending:
                        time.sleep(0.05)
            finally:
                for process in processes.values():
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                codes = {part: proc.wait(timeout=10) for part, proc in processes.items()}
                (EVIDENCE / f"{parts[0]}-exits.json").write_text(json.dumps(codes))

    def run(self) -> int:
        (self.work / "groups.json").write_text(json.dumps(self.groups))
        self.run_parts(["seed"])
        with ExitStack() as cleanup:
            for part in ("0", "1"):
                db = self.database(part)
                self.postgres("createdb", "-T", self.seed, db)
                cleanup.callback(self.postgres, "dropdb", "--force", db)
                dest = self.work / db
                shutil.copytree(self.seed_data, dest)
                filestore = dest / "filestore" / self.seed
                if filestore.exists():
                    filestore.rename(filestore.with_name(db))
            self.run_parts(["0", "1"])
        databases = self.query("postgres", "SELECT datname FROM pg_database WHERE datname NOT IN ('template0','template1')")
        same_inventory(databases, ["postgres", self.seed], "remaining databases")
        return len(self.expected.at_install) + len(self.expected.post_install)


def main() -> None:
    EVIDENCE.mkdir()
    reference = json.loads((Path(__file__).resolve().parent.parent / "reference.json").read_text())
    values = reference["inventory"]
    inventory = Inventory(**{**values, "class_seconds": Counter(values["class_seconds"])})
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="s") as temporary:
        total = ParallelRun(sys.argv[1:], inventory, reference["installed_addons"], Path(temporary)).run()
    (EVIDENCE / "counts.json").write_text(json.dumps({"tests": total}))
    print(f"Both post-install workers completed: {total} native tests across both phases")


if __name__ == "__main__":
    main()
