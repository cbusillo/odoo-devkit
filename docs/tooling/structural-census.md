# Technical structural census

The collector produces a consolidated structural list before the real migration.
Root keeps original-data custody and runs actual product probes through the
existing [heavy queue](https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/heavy-runs.md).
It adds no migration engine, scheduler or per-PR gate. OPW, CM inventory and the
CM website use separate actual inventories and reports. CM's fresh20 foundation
does not acquire a19 migration.

## Root extraction before delegation

Root first extracts from a source already within its separately authorized
custody. A source worker receives no archive, connection, credentials or business
records. No baseline/file-store restore is needed for the collector.

`docker/scripts/odoo_structural_extract.py` owns the column allowlist in `TABLES`.
It reads technical modules/dependencies, model/field declarations and inheritance,
XMLIDs/noupdate, every stock/custom/inactive view and language architecture,
parent/priority/source order/group and website bindings, all action classes and
view relations, menus, filters/defaults, automation, templates and QWeb counts.
The inherited action table accounts for unknown action subclasses too. Existing
ACL ownership work is reused; this is not a rights-equivalence oracle.

Use a dedicated new cursor, before any query, in root's existing extraction job:

```python
from odoo_structural_extract import extract_private_technical, project_private_inventory

private_tables = extract_private_technical(cursor, source_receipt_identity)
private_inventory = project_private_inventory(private_tables)
```

The extractor establishes one repeatable, read-only snapshot. It returns code,
compute, URL and history **presence**, never raw code, URLs or `arch_prev` text.
It reads no business tables. The shared projector maps IDs, XMLIDs, view topology,
action subclasses and relation rows into one schema; product owners do not need
another parser. Original IDs remain private technical order/identity evidence.

Both returns are **unscreened private material**. Architecture, domains, context,
translations, XPath and even technical identifiers can contain business values
or secrets. Root screens/minimizes them before delegation. Exclude business names,
labels, addresses, record values, chatter, attachments, credentials and private
endpoints. `screened` records that custody step, not automatic sanitization.
Root may obtain a separately bounded code/reference projection if needed; the
default extractor does not read code literals to audit them.

Retain source totals when rows are withheld. Mark changed/withheld expressions
and languages explicitly; never replace values with synthetic literals and
claim the original expression was tested. The projector retains unavailable
schema/table counts as unknown. Selection labels are minimized to technical
values and marked untested; only their actual choice values qualify schema
checks. Manual inheritance requiring additional qualification stays unavailable.
History presence is preserved; original history text and its effects remain
outside this projection. Root retains full technical packets/native logs.
Public output is counts, technical causes and owning issue links only.

## Canonical private inputs

The projector returns `binding`, `records`, `totals`, `action_totals` and
`schema_gaps`. `records` uses the buckets in `odoo_structural_census.BUCKETS`.
Each row has a stable `key`; rows can include `owners`, `xmlids`, typed
`references` (`kind`, `name`, optional `model`), `withheld` and `minimized`.
Missing source totals are unknown, never zero. The module bucket retains states,
so available/uninstalled source rows are not mistaken for installed requirements.

Views retain `parent`, `mode`, `type`, `model`, `active`, `priority`,
`source_order`, per-XMLID noupdate, `arch_updated`, history/customization presence,
`groups`, optional `view_key` and `website_scope`, plus `architectures` by language
and `changed_languages`. Website variants without actual target identity/context
remain untested; they are not combined into a generic family.

Fields retain technical `name`, `model`, `state`, `ttype`, `relation`, `related`,
`depends`, compute presence, safe selection values and flags. Actions retain the
actual class as `type`, target as `model`, `path`, `view_mode`/relation modes,
view references and safe context/domain. Unknown or executable classes remain
counted. Stored Python/server actions/compute code are never executed.
QWeb rows refer to their canonical view row rather than copying its architecture.

For static checking, the target surface has an exact `binding`, `surface`,
`modules`, `models` (fields, relation metadata and known public methods), `xmlids`,
`view_types`, `action_paths` and source-bound migration dispositions.
A static target remains static; `registry_surface` exports the native surface
only after a healthy empty target has loaded.

```bash
python /volumes/scripts/odoo_structural_census.py \
  --inventory /private/input.json --target /private/target.json \
  --output /private/census.json
```

Every retained view is statically inventoried, including obsolete stock such as
the Calendar regression. Inheritance fragments whose model cannot be inferred
are marked native-required; relational field domains use the comodel. Dynamic
expressions/code, numeric action IDs without target binding, withheld text and
auxiliary sources are visible gaps. Static blockers and native-unreached counts
are separate. `checked` excludes records with untested/blocked material.

## Native caller and stock dispositions

Root uses an Odoo Python caller inside its existing network-isolated disposable
job, with `db_template=template0` and no cron/server threads or test discovery.
Reuse Build's original `artifact-manifest.json` and baked image declaration:

```python
from pathlib import Path
import time
import odoo.addons
from odoo_structural_native import collect_native

candidate = {
    "artifact": artifact,
    "platform": platform_from_existing_receipt,
    "intended_modules": full_intended_community20_roots,
    "migration_sources": candidate_migration_and_rename_paths,
}
report = collect_native(
    inventory,
    candidate,
    [Path(root) for root in odoo.addons.__path__],
    declaration=declaration,
    deadline=time.monotonic() + remaining_queue_seconds,
    output=Path("/private/census.json"),
)
```

`artifact` is the original Build object, not a reconstructed graph. `declaration`
is read from `/opt/launchplane/evidence/release-compatibility.json` in that image.
The existing release verifier binds them. The artifact digest supplies the
candidate identity. Native registry/loading/converter/view/action/cache/transaction
sources and each migration/rename path are checked against existing declared file
hashes; no second caller-supplied native hash catalog is maintained. Old19/18
sources are static references only. Missing binding is not guessed.

Keep source `odoo_install_modules`, the full intended target roots, the actual
installed set and diagnostic reductions distinct. Build's `complete=true` means
available source, not installed/auto-installed or retained-record coverage.
Native bootstrap reports the actual installed set separately. It uses the full
intended closure first, then fresh empty baselines after known failures. Failed
modules and reverse dependencies remain blocked. Auto-install diagnosis uses its
actual manifest requirements; reducing a trigger is conditional and never clears
the original set. Unknown finalization attribution stops bootstrap with visible
unreached coverage. Work is finite and bounded by the existing queue deadline.

A matched stock XMLID is not permission to overwrite a20 root/action with its
old definition. Root qualifies its actual upgrade fate from pinned stock/migration
source and actual old flags. A row's existing `candidate_coverage` can record
`native20-stock-replacement`: exact artifact digest in `candidate`, release
`digest(row_without_candidate_coverage)` in `record_sha256`, a declared
`source_sha256`, the qualified `guard_receipt`, and matching XMLID in `reference`.
The collector uses the actual target record unchanged, so custom extensions are
checked against20. This is root's attestation of an already-proven guard, not a
new guard evaluator. Other covered findings match their exact `code`/`reference`
and may link their owning `issue`. Changed inputs invalidate the annotation.
An absent target XMLID is never blanket-excluded; retained obsolete stock is
probed as retained. Unqualified matched stock stays explicitly unreached.
No custom diff is rewritten onto a new stock view.

Safe manual declarations enter an empty committed baseline in a batch followed
by registry reload. Failure rolls back and reloads the last committed baseline;
only then are individual declarations diagnosed. Stored compute, callable
selection/domain and unsupported declarations remain blocked. This is schema
preparation, never business data or stored-code execution.

View/action probes make no schema changes. Each family/language or non-executable
window action uses a fresh native cursor/transaction on that owned baseline,
then full rollback and environment invalidation. Odoo20 transaction rollback
restores its ORM-cache layers; these sources are pinned above. No per-family DB
clone or registry reload is needed. The regression primes XMLID caches and proves
both row and cache isolation. Schema changes are confined to baseline preparation.
A candidate override that changes schema during a view/action write needs separate
qualification rather than this data-only probe contract.

A view family includes its actual root, extensions and sibling primary views.
An unchanged native constraint failure leaves that original family unreached;
independent families continue. Static scanning can find several bad references
inside one family; native validation still stops at its first error. This does
not promise every latent native error behind a failed dependency. No diagnostic
fragment removal, deactivation, forced readiness or validation bypass occurs.
Warnings/errors are retained privately with source/logger/severity. Actual
relative-order inversions are conditional; equal priorities alone are not a
blocker. Real-upgrade loaded-view filtering/order remains final-proof territory.
Inactive/translated/website/QWeb/action/data-dependent limits remain explicit.
Per-user UI reads, if needed later, must follow native pruned architecture.

The single private report is checkpointed before/through bootstrap and after
components. The work deadline reserves10% of remaining time, capped at30seconds,
for cleanup; the queue's process deadline remains the outer bound. Cleanup only
accepts this invocation's generated databases. A cleanup failure records
`incomplete`, retains findings/owned names in the report and raises; the existing
queue still owns its disposable containers and cleanup receipt. No sweep service
or second report store is introduced. A census is a structural diagnosis, not a
product migration/permission/workflow or preservation pass.

## Source delivery checks

Run `uv run python -m unittest discover -s tests -p test_structural_census.py` and
full touched safety modules, declared lint/format, native IDE inspection and the
weighed other-provider review. Real loader/stock-root/manual-field/cache/action
regressions live in the existing `devkit_live_checks` disposable suite. Root owns
coordinated native/CI/consumer/train/adoption checks under Q1097; a source worker
submits no CT125 or new per-PR heavy job. Actual product technical coverage and
final real-data migration remain with their product owners.
