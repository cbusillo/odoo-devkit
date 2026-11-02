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
technical XMLIDs/noupdate, every stock/custom/inactive view and language architecture,
parent/priority/source order/group and website bindings, all action classes and
view relations, menus, filters/defaults, automation, scheduled-action references,
templates and QWeb counts. Scheduled actions expose only technical ID, server-action
link, active flag and priority; no names, operators, schedules or code are selected.
The inherited action table accounts for unknown action subclasses too. XMLID
names are selected only for technical models, including the actual action
subclasses and `ir.actions.act_window.view` link identities. Link aliases remain
in the technical XMLID bucket, separate from executable action/button bindings.
Other XMLID rows are counted without selecting their names or IDs.
Existing ACL ownership work is reused; this is not a rights-equivalence oracle.

Earlier packets omitted action-view link XMLIDs even when their physical rows
were captured. Their original scope and out-of-scope counts remain unchanged;
reprojection cannot recover omitted identities. Link ownership from those packets
is UNKNOWN, never absence, until the next already-needed qualified intake
captures it.

Use a dedicated new cursor, before any query, in root's existing extraction job:

```python
from odoo_structural_extract import extract_private_technical, project_private_inventory

private_tables = extract_private_technical(cursor, source_receipt_identity)
private_inventory = project_private_inventory(private_tables)
```

The extractor establishes one repeatable, read-only snapshot. It returns code,
compute, URL and history **presence**, never raw code, URLs or `arch_prev` text.
It reads no business tables. The shared projector maps IDs, XMLIDs, view topology,
action subclasses and relation rows into one schema; `out_of_scope.nontechnical_xmlids`
keeps the excluded count separate from technical XMLID totals. Unknown scope
counts remain unknown. Product owners do not need another parser. Original IDs
remain private technical order/identity evidence.

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

The projector returns `binding`, `records`, `totals`, `action_totals`,
`automation_totals`, `out_of_scope` and `schema_gaps`. `records` uses the buckets in `odoo_structural_census.BUCKETS`.
Each row has a stable `key`; rows can include `owners`, `xmlids`, typed
`references` (`kind`, `name`, optional `model`), `withheld` and `minimized`.
Missing source totals are unknown, never zero, including buckets that contain
known emitted rows. `automation_totals` retains `base_automation` and `ir_cron`
counts separately; the combined automation total stays unknown if either count
is unknown. Scheduled rows retain their `source_action` link and any bound
technical action XMLID. The module bucket retains states,
so available/uninstalled source rows are not mistaken for installed requirements.

Views retain `parent`, `mode`, `type`, `model`, `active`, `priority`,
`source_order`, per-XMLID noupdate, `arch_updated`, history/customization presence,
`groups`, optional `view_key` and `website_scope`, plus `architectures` by language
and `changed_languages`. `action_bindings` maps the original numeric button names
to technical action XMLIDs. Original architecture and numeric IDs stay intact in
the private input; only the disposable probe resolves bound buttons to target IDs.
An unresolved target action stays untested. The default empty baseline activates
only `en_US`; other source languages remain `target-language-unavailable` and
prevent an overall native pass for that family. If the target already has the
requested language active, missing translations use unchanged `en_US`
architecture, matching native20 fallback. No language activation flow is added.
Explicitly withheld/changed languages or a withheld/changed fallback remain
untested. Website variants without actual
target identity/context remain untested; they are not combined into a generic family.

Fields retain technical `name`, `model`, `state`, `ttype`, `relation`, `related`,
`depends`, compute presence, safe selection values, `domain`, `on_delete`, `size`
and flags. Field-declaration `domain`/`context` references use the relation model.
Nonliteral manual domains and scalar literal domains stay unavailable for baseline
preparation; expression shapes outside known domain/context containers stay
unvalidated, without executing them or declaring their runtime behavior invalid.
Actions retain the
actual class as `type`, target as `model`, `path`, `view_mode`/relation modes,
view references and safe context/domain. Unknown or executable classes remain
counted. Stored Python/server actions/compute code are never executed.
Stored-code attribute scans identify only target/source-proven fields; methods
and unknown ORM attributes are not reported as missing fields. Unknown
attributes remain unresolved and code remains unexecuted.
Window-action creation checks native model/path/view-mode constraints, but does
not validate context/domain semantics. Retained expressions remain explicitly
untested even when they are literals; their safe creation still runs so native
constraint failures can be collected. Qualified stock replacement uses the actual
target action and does not audit discarded old expressions.
QWeb rows refer to their canonical view row rather than copying its architecture.

For static checking, the target surface has an exact `binding`, `surface`,
`modules`, `models` (fields, relation metadata and known public methods), `xmlids`,
`view_types`, `action_paths` and source-bound migration dispositions.
A static target remains static; `registry_surface` exports the native surface
only after a healthy empty target has loaded.

Native `action_paths` maps each path to concrete target identities using the
same `{model, key}` shape as `xmlids`. Both registry exports, before and after
manual baseline preparation, use the action's concrete `type` and target ID;
base-class XMLID lookup and a chosen alias are unnecessary. Static targets may
also name owners by XMLID. Source XMLIDs must resolve to one target of the
source action's class before comparing owners. Aliases for that same target
do not compete. Missing keys, ambiguous mappings, moved classes and legacy
unqualified `native:<id>` owners remain `action-path-identity-unresolved`,
not proven competitors. Source IDs never match target IDs by number.
Independent source duplicates and distinct qualified target owners still
produce `action-path-conflict`. This comparison does not replace native path
syntax, reserved-name or uniqueness checks, or the stock-disposition guard.

```bash
python /volumes/scripts/odoo_structural_census.py \
  --inventory /private/input.json --target /private/target.json \
  --output /private/census.json
```

Every retained view is statically inventoried, including obsolete stock such as
the Calendar regression. Inheritance fragments whose model cannot be inferred
are marked native-required; relational field domains use the comodel. Form/list
field contexts use the comodel, while search-field contexts and `filter_domain`
use the view model. Unsupported view types remain
structural findings, including retained hierarchy/form mismatches. Dynamic
expressions/code, numeric action IDs without target binding, withheld text and
auxiliary sources are visible gaps. Static blockers and native-unreached counts
are separate: `static-findings` counts components with static blockers, and
`native-unreached` counts components without complete native checks. The finding
state counts keep their own names, with `static-only` represented solely by
`static-findings`. An unbound XMLID reference with a nonzero or unknown excluded
count is `xmlid-scope-unresolved`: the count does not identify it as a business
XMLID. Known technical identities and blocked owners retain separate findings.
Groups and action-button references have a known technical type, so an absent
reference is a static blocker when the technical XMLID bucket is complete and
unmodified. Incomplete/withheld technical inventories and untyped expression
references retain scope uncertainty.

Context expressions keep producer values separate from destination keys.
Form/list relational-field defaults use the comodel while their values use the
surrounding model; search-field defaults use the search view model. Concrete
nested views keep their own view type and model, and inheritance fragments stay
uncertain. Action buttons resolve symbolic names or the view's original numeric
`action_bindings` through qualified target XMLID class/key facts to one action
definition in the same source inventory. Its retained `noupdate` window-action model owns
`default_*` and `group_by`; producer field expressions remain checked on the view
model. Source and target numeric IDs are never compared. This checks the retained
source definition, not a native action execution or a stock-disposition proof.
Missing, conflicting, withheld, minimized, reloaded stock or replaced action definitions leave
`context-consumer-uncertain` in `data-dependent/unreached`. Object-button return
values and standalone inherited context attributes have the same uncertainty;
the collector does not infer Python return values. Thus the phone blacklist and
picking Operations object buttons no longer demand wizard/move defaults on their
producer models, without claiming their destinations were tested.
Typed action links use the same attribution. Numeric links without extracted
bindings stay uncertain; this change does not expand native link rebinding.

`search_default_*` may name a destination field or a named search filter.
An explicitly linked same-inventory search architecture can establish filter
names with complete technical XMLID scope and no unresolved inheritance, changed/withheld/minimized text,
or replacement. An unbound source search must have no aliases or only private
`__` aliases absent from the target. Otherwise every alias must resolve to one
retained `noupdate` view identity, claimed by exactly one source view including
withheld or inactive rows, with complete target parentage and no parent or child.
Missing module aliases never establish custom origin or a surviving record.
Otherwise an absent field stays `search-filter-unresolved`.
Nested payload dictionaries do not inherit the outer context's destination:
they remain consumer-uncertain, including in record-level contexts. Their value
expressions and the existing dynamic/native limits remain visible. Record-level
action/filter context names are evaluation globals, not producer record fields.
`checked` excludes records with untested/blocked
material and is recalculated at each checkpoint. Missing technical XMLIDs on an
empty target require an upgrade disposition; business XMLIDs are outside scope.

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
sources and each migration/rename path are checked against the existing
namespaced inventories. For `base:runtime` and `base:devtools`, the collector reads
`/opt/launchplane/evidence/base-runtime-inventory.json` and
`base-devtools-inventory.json` (the existing lists of classified file records),
then reuses `build_inventory` with the actual addon paths to reproduce each
`source-inventory.sha256` declaration. Other source namespaces retain their
existing declared files. The actual loaded core files and all requested source
paths must match those bound records; `native_sources` records their hashes.
A stock/migration attestation may use a verified source path from any namespace:
include every such proof path in `migration_sources`. No caller hash catalog is
maintained. Binding failures are checkpointed before raising. Old19/18
sources are static references only. Missing binding is not guessed.

With a report output such as `/private/census.json`, the collector atomically
retains `/private/census.target.json` at each successful static-analysis point.
This is the actual baseline-enriched target passed to `collect_static`, including
pending/blocked declarations, conditional state, native identities and view
topology. It excludes the static collector's private working copy and source
records. Use it unchanged as `--target /private/census.target.json` with the same
retained inventory and a separate `--output /private/census.replay.json` for
cheap static replay, preserving the native report. Keep both files private in
the existing results artifact group with the invocation record, original
artifact and bound source evidence.
Replay is static analysis, not another native observation or product pass.

The last qualified target survives later manual preparation, probe or cleanup
failure. No target is published before a healthy registry and successful static
analysis. A pre-existing target path, including a symlink, refuses the invocation
before replacing its report; choose a fresh report path in the caller's existing
results directory rather than reusing or deleting unknown evidence. `output=None`
keeps collection in memory and writes neither file. Historical runs that discarded
their target cannot be reconstructed from their census report; only the next
already-required native episode can supply this artifact.

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
The collector uses the actual target record unchanged and does not scan the
discarded old architecture as a blocker, so custom extensions are checked against20.
This is root's attestation of an already-proven guard, not a new guard evaluator.
Other covered findings match their exact `code`/`reference`
and may link their owning `issue`. Changed inputs invalidate the annotation.
Coverage is also classified after native observations. The original
`native_checks` and native failure state remain visible; a covered error never
turns the original family into a pass. Discarded replacement-action paths do not
compete with custom paths; the target's action paths remain authoritative.
Only `noupdate` qualifies a matched stock view with no replacement disposition
for a retained overlay. `arch_updated` and per-user customization copies do not
prove retention.
An absent target XMLID is never blanket-excluded; retained obsolete stock is
probed as retained. Unqualified matched stock carries
`stock-upgrade-disposition-required` alongside its original structural findings,
so discarded-definition candidates remain distinguishable from retained failures.
Known unreached causes have separate codes for stock disposition, architecture,
website, parent, groups and action data/identity.
No custom diff is rewritten onto a new stock view.

The healthy pre-manual target is statically diagnosed and checkpointed before
schema preparation, with manual-dependent material marked pending. Safe manual
declarations enter a committed baseline in a batch. Missing relation models,
related paths, known type/comodel mismatches and non-searchable existing hops
are blocked before creation. Pending declarations supply their declared relation
and type for the remaining path check; native setup still owns full validation.
Failure rolls back and reloads the last committed baseline before individual
declarations are diagnosed in dependency-ready order, with finite progress and
the same deadline. Every successful schema-changing commit can reload the
registry through native signaling; the driver retrieves that current registry
without another explicit success reload. Those native loads still cost time.
A deadline is not a declaration failure and never starts another explicit
reload. Field paths check every model and hop for pending versus genuinely
blocked material. On a manual deadline or setup failure the pre-manual static
report survives with the specific unreached cause on every dependent bucket;
the possibly changed registry is not probed. Stored compute, callable
selection/domain and unsupported declarations remain blocked. This is schema
preparation, never business data or stored-code execution.

View/action probes make no schema changes. Each family/language or non-executable
window action uses a fresh native cursor/transaction on that owned baseline,
then full rollback. Odoo20 transaction rollback
restores its ORM-cache layers; these sources are pinned above. No per-family DB
clone or registry reload is needed. The regression primes XMLID caches and proves
both row and cache isolation. Schema changes are confined to baseline preparation.
A candidate override that changes schema during a view/action write needs separate
qualification rather than this data-only probe contract.

A view family includes its actual root, extensions and sibling primary views.
Native grouping uses the healthy target's technical ID/parent map for matched
stock that stays native, including qualified reparented replacements. Retained
`noupdate` and newly created rows keep their source parents. Grouping and load
dependencies follow those effective ancestors without rewriting original
fragments, including source ancestors reached through native-only intermediate
views. Missing, ambiguous or cyclic effective topology stays unreached.
An operation failure is attributed to that operation. A final combined-family
validation failure is recorded separately in `family_failures` as native-reproduced,
with the validation record and all family members; individual members remain
unreached because the cause is not isolated. Independent families continue.
Static scanning can find several bad references inside one family; native
validation still stops at its first error. This does
not promise every latent native error behind a failed dependency. No diagnostic
fragment removal, deactivation, forced readiness or validation bypass occurs.
Warnings/errors are retained privately with source/logger/severity. Order
comparisons use active extension siblings at their actual target parent and
priority, including attested replacements. Real upgrades preserve original IDs;
a created sibling that precedes a native sibling in source order can follow it
in the disposable target. Reproduced inversions are conditional; equal priorities
alone are not a blocker. Native20 extensions with no source row have no old ID
to compare; their ordering and real-upgrade loaded-view filtering remain
final-proof territory.
Inactive/translated/website/QWeb/action/data-dependent limits remain explicit.
Per-user UI reads, if needed later, must follow native pruned architecture.

The single private report is checkpointed initially, around bootstrap, on fatal
propagated errors and at final cleanup. Ordinary family/action progress
and handled component diagnostics write at most once per30seconds, bounding repeated
whole-report serialization. A forced stop can lose the intervening progress;
atomic writes preserve the prior checkpoint. The report retains unknown totals
and each automation source count even when no registry can be built. The work deadline reserves10% of remaining time, capped at30seconds,
for cleanup; the queue's process deadline remains the outer bound. Cleanup only
accepts this invocation's generated databases. A cleanup failure records
`incomplete`, retains findings/owned names in the report and raises; the existing
queue still owns its disposable containers and cleanup receipt. No sweep service
or second report store is introduced. A census is a structural diagnosis, not a
product migration/permission/workflow or preservation pass.

## Source delivery checks

Run `uv run python -m unittest discover -s tests -p 'test_structural*.py'` and
full touched safety modules, declared lint/format, native IDE inspection and the
weighed other-provider review. Real loader/stock-root/manual-field/cache/action
regressions live in the existing `devkit_live_checks` disposable suite. Root owns
coordinated native/CI/consumer/train/adoption checks under Q1097; a source worker
submits no CT125 or new per-PR heavy job. Actual product technical coverage and
final real-data migration remain with their product owners.
