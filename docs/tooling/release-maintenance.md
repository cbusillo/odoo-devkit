# Targeted release maintenance

Launchplane owns classification, traffic, writer quiescence and authorization.
Its [release compatibility contract](https://github.com/cbusillo/launchplane/blob/HEAD/docs/release-database-compatibility.md)
is the authority for declaration fields and classification. Product builds never
call Launchplane or require its credentials.

Production artifacts generate `release-compatibility.json` alongside dependency
evidence and include it in the schema-2 manifest. Tenant, shared-addon and devkit
inventories hash exact committed trees, including build configuration and symlink
blobs. Base inventories cover framework, Enterprise and runtime helper inputs
from the digest-bound images. Fetched addons and the resolved module graph come
from the assembled image. Platform disagreement or a missing source refuses
publication; missing graph dependencies mark declarations incomplete. Addon
Python is conservatively database-changing; static files and manifest asset
semantics are separate. Unexamined base/dependency changes require Launchplane's
examined-input plan, never a blanket upgrade. Legacy artifacts without complete
declarations remain conservative.

The orchestrator sets `ODOO_RELEASE_MODULE_PLAN_FILE` and `ODOO_RELEASE_IMAGE`
(the running candidate's exact `repository@sha256:digest`). The JSON file has
three fields: `database`, `release` (the stored `ReleaseDatabaseCompatibility`
record), and `candidate_manifest` (normalized verified `ArtifactIdentityManifest`
JSON including defaults). The release hashes that normalized representation,
rather than the earlier producer JSON. Inputs contain provenance and module
plans, never secret values.

The consumer verifies database/artifact/image/hash identities and the complete
image graph. Required installs are reconciled with installed modules; updates
expand only through installed reverse dependents. Pending work, missing
installed addons, omitted changes or incomplete plans refuse execution. A
compatible release missing a required module refuses rather than performing
hidden DB work during overlap.

The existing SQL-only credential boundary commits and reads back before any
Odoo registry or hook. One database-less Odoo shell explicitly creates the
updating registry with the resolved install/update flags, then reuses it for
existing settings, website, mail fencing, admin/schema/policy and service-user
checks. Odoo shell's ordinary implicit registry does not apply update flags,
so the explicit startup is required. Required module-state and pending-work
readback must pass before a receipt is emitted. This does not unset `noupdate`,
force editor data, switch traffic, start web or resume writers.

`ODOO_RELEASE_MAINTENANCE_RECEIPT_FILE` optionally records plan/candidate
identities, changed/install/update lists, dependency effects, actual new installs,
module-state readback and total elapsed time. Timing includes reconciliation and
credential preparation. Isolated tests count registry-load log events and
compare the whole run with the legacy multi-process path. A receipt does not
attest public traffic or cutover readiness.

Without a plan file, existing restore/bootstrap and legacy post-deploy callers
retain their behavior. New zero-downtime orchestration must supply a complete
plan and must not treat absence/refusal as an empty update list. Launchplane's
cutover issue owns adoption.
