# Technical structural census

The census compares retained technical metadata with the exact Community20
candidate. It is a diagnostic before the real migration, not a migration engine
or a per-PR gate. Root keeps original-data custody and runs actual product probes
through the existing [heavy queue](https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/heavy-runs.md).
Ordinary source tests use [addon CI](addon-ci.md). OPW, CM inventory and the CM
website each supply their own installed-module inventory and report; CM's fresh20
foundation does not acquire an invented19 migration.

## Root extraction before delegation

Do not give a worker an archive, database connection, credentials or business
records. Root first runs a read-only extraction on an already authorized source,
inside its existing custody. No baseline or file-store restore is needed. Read
only these structural columns, using the source version's actual schema:

- `ir.module.module`: technical name, state, installed version and dependency
  names. Include unavailable Enterprise, renamed, merged and custom modules.
- `ir.model` / `ir.model.fields`: technical model/field names, state, declaring
  modules/XMLIDs, type, relation/related/depends, compute presence, store,
  required, readonly, index and safe selection structure. Stored compute text
  stays private and is never executed. ACL ownership reuses the existing OPW389
  census; this does not attempt rights equivalence.
- `ir.model.data`: module/name/model, technical record key and noupdate.
- Every `ir.ui.view`: opaque stable key and source numeric order, XMLIDs,
  model/type/mode, parent key, priority, active, language variants, arch_fs,
  arch_updated, noupdate, customization/history presence and exact architecture
  only after screening. Include stock, obsolete, inactive and QWeb records and
  relevant sibling primary views. Preserve original order, flags and fragments.
- All action subclasses and their action/view relation rows: technical class,
  model bindings, path, view modes, view/XMLID references, safe context/domain
  structure, report/client/URL reference shape and stored-code presence.
- Menus, saved filters, defaults, automation, mail templates and QWeb:
  opaque keys, technical references and counts; never default values, recipients,
  bodies or URLs containing business values. Expressions requiring those values
  remain withheld and untested.

Map numeric relations to stable technical keys while still in root custody.
Keep source19/18 pinned static references and migration rename/disposition files
with the candidate's existing source receipt. Record source/input identity and
counts per bucket before projecting. A missing table/class is an explicit gap,
not a zero. Count any withheld row/column or changed expression separately.

Architecture, context, domain, code, translations, XPath predicates and even
technical identifiers may contain business values or secrets. Root screens and
minimizes these **before** delegation; an allowlisted column alone is not safe.
Remove business names/labels, addresses, record values, chatter, attachments,
credentials and private endpoints. Mark each changed/withheld expression on its
record. Never substitute synthetic literals and claim that expression was tested.
Keep full technical packets and native logs private. Public results contain only
counts, technical causes and owning issue links. This recipe does not authorize
this worker to read originals or extract a real packet.

## Input and callers

`docker/scripts/odoo_structural_extract.py` exposes
`extract_private_technical(cursor, binding)` for root's dedicated, new read-only
transaction. It reads only its table/column allowlist, including all action
classes through the inherited action table, with unavailable tables/columns
reported. Its return is **unscreened private material**, not a delegation packet.
It reads no business tables and changes no source data. Root screens it, maps
foreign keys to technical keys and projects the following JSON. Keep the source
counts even when screening withholds rows.

```json
{
  "binding": "source-receipt-identity",
  "records": {
    "modules": [{"key": "base", "state": "installed"}],
    "models": [], "fields": [], "xmlids": [],
    "views": [{
      "key": "custom.retained", "xmlids": ["custom.retained"],
      "model": "res.users", "type": "form", "mode": "primary",
      "active": true, "priority": 16, "source_order": 1,
      "noupdate": true, "arch_updated": true,
      "history_present": false, "customization_present": true,
      "architectures": {"en_US": "<form><field name=\"name\"/></form>"}
    }],
    "actions": [], "menus": [], "filters": [], "defaults": [],
    "automation": [], "templates": [], "qweb": []
  },
  "totals": {
    "modules": 1, "models": 0, "fields": 0, "xmlids": 0,
    "views": 1, "actions": 0, "menus": 0, "filters": 0,
    "defaults": 0, "automation": 0, "templates": 0, "qweb": 0
  },
  "action_totals": {}
}
```

Rows can add `owners`, `references` (`kind`, `name`, and optional `model`),
`withheld`, and safe `context`/`domain`. References support model, field,
module and XMLID. Manual fields use technical `name`, `model`, `state`, `ttype`,
`relation`, `related`, `depends`, `compute` presence/text and retained flags.
Views use `parent` keys, `groups` as group XMLIDs, optional original `view_key`,
`architectures` per language and `changed_languages` for minimized variants.
Actions use `type` for the action class, `model` for its target model, `path`,
`view_mode` and explicit references. `view_references` and `res_id` mark native
action coverage needing unavailable retained views/data. Do not omit such facts
just to make a window action probe reachable. Put every unsupported auxiliary
source in its own counted bucket rather than dropping it. `action_totals` counts
each actual action class, including classes the target no longer supports.

For a source-only comparison, supply a source-bound target surface with
`binding`, `surface`, `modules` (actual intended closure), `models` (each with
`fields`, relational `relation`, and known public `methods`), `xmlids` (each
with its record model), `view_types`, `action_paths` and `migration_sources`.
The target can be exported by `registry_surface` from a healthy empty registry;
a statically assembled surface must identify itself as static and cannot claim
native validation. Run the existing container script directly:

```bash
python /volumes/scripts/odoo_structural_census.py \
  --inventory /private/input.json --target /private/target.json \
  --output /private/census.json
```

For native probes, root uses an Odoo Python caller **inside its existing queued,
network-isolated disposable job**. Initialize native config with `db_template`
set to `template0`, no cron/server threads or test discovery. The candidate
object names its existing artifact/receipt `binding`, `platform`,
`intended_modules`, `release_compatibility`, exact `native_sources` SHA256 values
for `registry`, `loading`, `convert`, `views`, `actions`, and `migration_sources`
(mapping actual rename/migration file paths to hashes; empty only if there are
none). Pin these from the actual candidate, not the older design review cache.
Reuse Build's `artifact-manifest.json`: take `release_compatibility` directly,
derive `binding` with the existing release `digest(artifact)` and take the
initial intended install roots from `odoo_install_modules`, augmented only by
the product owner's actual intended closure. Do not rebuild another source
graph. Build's `complete=true` describes available source, not installed modules,
auto-install results or retained-record coverage; native bootstrap reports its
actual installed set separately.
Pass the baked image declaration from
`/opt/launchplane/evidence/release-compatibility.json` as `declaration`; the
existing release verifier binds it to the candidate. Addon roots must be the
actual ordered native namespace, including the product's intended custom addons.

```python
from pathlib import Path
import json
import time
import odoo.addons
from odoo_structural_native import collect_native

report = collect_native(
    inventory,
    candidate,
    [Path(root) for root in odoo.addons.__path__],
    declaration=declaration,
    deadline=time.monotonic() + remaining_queue_seconds,
)
Path("/private/census.json").write_text(json.dumps(report, indent=2))
```

The existing queue's process deadline is the outer bound; the collector also
checks it before each finite bootstrap/probe step. It never clones a caller's
database. It creates an empty random named database, uses the unchanged native
loader on the full intended closure, and removes failed modules and reverse
dependencies only for a clean diagnostic rebuild. Unknown failing-module identity
stops native bootstrap with unreached coverage. A reduced closure is always
conditional and never clears the original request. Cleanup accepts only databases
this invocation created. Keep the queue's cleanup receipt and private logs.

Safe manual model/primitive-field declarations enter the empty baseline one at a
time, commit and reload the registry before probes. A failed declaration rolls
back and reloads the last committed baseline; executable compute/related/selection
or unsupported declarations stay unavailable. Each view family/language and
non-executable window-action probe uses a new disposable clone of that empty
baseline, rolls back and invalidates caches, then drops its entire registry/DB.
No savepoint is treated as sufficient schema/registry isolation. Stored actions
are never run or instantiated to produce missing schema.

Native view families retain all source roots/extensions/primary siblings; checks
preserve native constraints. One failure leaves the remainder of that family
unreached, while independent families continue. Warnings and actual relative-order
inversions are separate findings. No fragment is rewritten, no check is skipped,
and no view is deactivated for diagnosis. Inactive views, QWeb rendering,
translations without a faithful full family, real-upgrade filtering/order,
non-window actions and data-dependent expressions keep their explicit limits.
A valid native check is a structural check on this empty fixture, never a real
upgrade, data-preservation, user-permission or workflow pass. This collector does
not perform per-user UI reads; those must follow native pruned architecture.

A root-qualified candidate migration disposition can annotate a finding using a
row's `candidate_coverage`: exact `candidate` binding, `record_sha256` (existing
release digest over the row without that annotation), `source_sha256` present
in the candidate's migration sources, `guard_receipt`, matching `code` and
`reference`, and owning `issue`. This records root's already-proven guard; it
does not run migration code or guess a rename. Changed inputs invalidate it.
Original reference failures remain visible with `candidate-covered` status.

`components` and `coverage` distinguish static checking, native reproduction,
conditional results, blocked/unreached work, warnings and candidate coverage.
Per-language native checks and per-action-class counts stay separate. Missing
source totals and withheld rows are gaps. `clear` stays false: a census cannot
authorize the real-data proof or declare the product migrated. Root routes the
consolidated technical root causes to existing owners before opening new slices.

## Source delivery checks

Portable tests: `uv run python -m unittest discover -s tests -p test_structural_census.py`.
The real registry/loader/action/manual-field/rollback regressions live alongside
`devkit_live_checks` in the existing disposable addon suite, using only synthetic
fixtures. Root runs that coordinated native suite/CI before adoption under Q1097;
a source worker does not submit CT125 or a new per-PR heavy job. Product input
coverage and the final real-data migration remain the product owners' proof.
