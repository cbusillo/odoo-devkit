import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from odoo_release_inventory import addon_digest, ignored_source_entry
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
        return cursor.fetchone()[0]


def install_tracker(database: connection) -> None:
    with database, database.cursor() as cursor:
        cursor.execute(INSTALL_TRACKER)


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
        if last_loaded is None or last_loaded != addon_digest(paths[name]):
            changed.add(name)
    return changed


def _ignore_source_entries(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if ignored_source_entry(name)}


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
        target_root = snapshot_roots.get(path.parent, directory / str(len(snapshot_roots)))
        snapshot = target_root / name
        shutil.copytree(path, snapshot, symlinks=True, ignore=_ignore_source_entries)
        digest = addon_digest(snapshot)
        if digest is not None:
            snapshot_roots[path.parent] = target_root
            snapshots[name] = snapshot
            digests[name] = digest
        else:
            shutil.rmtree(snapshot)
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
