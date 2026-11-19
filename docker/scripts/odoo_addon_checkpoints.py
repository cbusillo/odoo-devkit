import hashlib
import os
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from odoo_release_inventory import ignored_source_entry
from psycopg2.extensions import connection

TABLE = "public.devkit_addon_checkpoint"
ATTEMPT_SETTING = "odoo_devkit.module_load_attempt"

INSTALL_TRACKER = """
CREATE TABLE IF NOT EXISTS public.devkit_addon_checkpoint (
    name text PRIMARY KEY, digest text, attempt text
);
CREATE OR REPLACE FUNCTION public.devkit_invalidate_addon_checkpoint() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF to_regclass('public.devkit_addon_checkpoint') IS NOT NULL THEN
        UPDATE public.devkit_addon_checkpoint SET digest = NULL,
            attempt = CASE WHEN TG_ARGV[0] = 'load' THEN current_setting('odoo_devkit.module_load_attempt', true) END
        WHERE name = NEW.name;
    END IF;
    RETURN NEW;
END
$$;
CREATE OR REPLACE TRIGGER devkit_addon_loaded AFTER UPDATE OF demo ON public.ir_module_module
    FOR EACH ROW EXECUTE FUNCTION public.devkit_invalidate_addon_checkpoint('load');
CREATE OR REPLACE TRIGGER devkit_addon_state_changed AFTER UPDATE OF state ON public.ir_module_module
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state AND NEW.state <> 'installed')
    EXECUTE FUNCTION public.devkit_invalidate_addon_checkpoint('state');
ALTER TABLE public.ir_module_module ENABLE TRIGGER devkit_addon_loaded;
ALTER TABLE public.ir_module_module ENABLE TRIGGER devkit_addon_state_changed;
"""


@dataclass(frozen=True)
class AddonLoad:
    attempt: str
    roots: list[Path]
    sources: dict[str, Path]
    digests: dict[str, str]

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        pgoptions = environment.get("PGOPTIONS", "")
        return {**environment, "PGOPTIONS": f"{pgoptions} -c {ATTEMPT_SETTING}={self.attempt}".strip()}


def checkpoint_exists(database: connection) -> bool:
    with database.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,))
        return next(cursor)[0]


def ensure_tracker(database: connection) -> None:
    with database, database.cursor() as cursor:
        if checkpoint_exists(database):
            cursor.execute("""
                SELECT count(*) = 2 FROM pg_catalog.pg_trigger
                WHERE tgrelid = 'public.ir_module_module'::regclass
                  AND tgname IN ('devkit_addon_loaded', 'devkit_addon_state_changed')
                  AND tgenabled IN ('O', 'A')
                """)
            if next(cursor)[0]:
                return
        cursor.execute(INSTALL_TRACKER)
        cursor.execute("UPDATE public.devkit_addon_checkpoint SET digest = NULL, attempt = NULL WHERE name <> ''")


def checkpoint_digests(database: connection) -> dict[str, str | None]:
    with database.cursor() as cursor:
        cursor.execute("SELECT name, digest FROM public.devkit_addon_checkpoint")
        return dict(cursor.fetchall())


def complete_adoption(database: connection) -> None:
    with database, database.cursor() as cursor:
        cursor.execute("INSERT INTO public.devkit_addon_checkpoint (name) VALUES ('') ON CONFLICT DO NOTHING")


def changed_addons(paths: dict[str, Path], recorded: dict[str, str | None], installed: set[str]) -> set[str]:
    changed = set()
    for name in paths.keys() & installed:
        last_loaded = recorded.get(name)
        try:
            if last_loaded is not None and last_loaded == addon_digest(paths[name]):
                continue
        except (OSError, ValueError):
            pass
        changed.add(name)
    return changed


def _addon_entries(directory: Path, ancestors: frozenset[Path] = frozenset()) -> Iterator[Path]:
    resolved = directory.resolve()
    if resolved in ancestors:
        raise ValueError(f"Circular addon directory symlink: {directory}")
    for path in sorted(directory.iterdir()):
        if ignored_source_entry(path.name):
            continue
        if path.is_dir():
            yield path
            yield from _addon_entries(path, ancestors | {resolved})
        elif path.is_file():
            yield path


def addon_digest(root: Path, *, snapshot: Path | None = None) -> str:
    digest = hashlib.sha256()
    if snapshot is not None:
        snapshot.mkdir(parents=True)
    for source in _addon_entries(root):
        relative = source.relative_to(root)
        if source.is_dir():
            if snapshot is not None:
                (snapshot / relative).mkdir()
            continue
        if snapshot is not None:
            source = Path(shutil.copy2(source, snapshot / relative))
        digest.update(os.fsencode(relative.as_posix()) + b"\0")
        with source.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def prepare_addon_load(database: connection, roots: list[Path], paths: dict[str, Path], directory: Path) -> AddonLoad:
    with database, database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO public.devkit_addon_checkpoint (name) SELECT unnest(%s::text[]) ON CONFLICT DO NOTHING",
            (list(paths),),
        )

    snapshots = {}
    digests = {}
    snapshot_roots = {}
    for name, path in paths.items():
        target_root = snapshot_roots.setdefault(path.parent, directory / str(len(snapshot_roots)))
        snapshot = target_root / name
        digests[name] = addon_digest(path, snapshot=snapshot)
        snapshots[name] = snapshot
    load_roots = [path for root in roots for path in (snapshot_roots.get(root), root) if path is not None]
    return AddonLoad(uuid4().hex, load_roots, snapshots, digests)


def confirm_addon_load(database: connection, load: AddonLoad, loaded: list[str]) -> None:
    with database, database.cursor() as cursor:
        cursor.executemany(
            """
            UPDATE public.devkit_addon_checkpoint SET digest = %s, attempt = NULL
            WHERE name = %s AND attempt = %s
              AND EXISTS (SELECT 1 FROM public.ir_module_module WHERE name = %s AND state = 'installed')
            """,
            [(load.digests[name], name, load.attempt, name) for name in set(loaded) & load.digests.keys()],
        )
