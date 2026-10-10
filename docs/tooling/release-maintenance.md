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
from the assembled image. Source identity or module-graph disagreement across
platforms refuses publication; missing graph dependencies mark declarations
incomplete. Native base tools retain platform-specific hashes. Addon
Python is conservatively database-changing; static files and manifest asset
semantics are separate. Unexamined base/dependency changes require Launchplane's
examined-input plan, never a blanket upgrade. Legacy artifacts without complete
declarations remain conservative.

For an examined dependency/base/tool change, first build the immutable inputs
with `runtime check-artifact`. The examination uses Launchplane's pure
`release_examined_inputs_sha256` on the normalized manifest; that contract owns
the fingerprint, so the build does not invent a second algorithm. Provide an
external JSON file containing only `examined_inputs_sha256` and
`database_update_modules` (an explicit array, including `[]` when examination
found no database work). Rebuild the same committed inputs using
`runtime check-artifact --examined-input-plan <file>` or
`runtime publish --examined-input-plan <file>`. Keep the file outside tracked
source trees to avoid changing its own fingerprint. The declaration is baked
into the image and published manifest; Launchplane verifies its fingerprint
against the actual complete inputs. Its contract requires a matching examined
fingerprint on the production baseline too. Stale evidence refuses execution.
This workflow needs no Launchplane connection, credentials or deployment.

The orchestrator sets `ODOO_RELEASE_MODULE_PLAN_FILE` and `ODOO_RELEASE_IMAGE`
(the running candidate's exact `repository@sha256:digest`). The JSON file has
three fields: `database`, `release` (the stored `ReleaseDatabaseCompatibility`
record), and `candidate_manifest` (normalized verified `ArtifactIdentityManifest`
JSON including defaults). The release hashes that normalized representation,
rather than the earlier producer JSON. Inputs contain provenance and module
plans, never secret values.

The single-registry runner supports the repository's Odoo 19 image API. Artifact
and runtime graph resolution use exposed addon paths and Odoo's first-path
precedence. The receipt includes Odoo's own `updated_modules` evidence; module
metadata timestamps alone are not proof of an upgrade.

Include `production_manifest` for manifest changes so assets-only differences
can be distinguished from changed database semantics. The consumer also checks
the declaration baked into its running image. Its default path is
`/opt/launchplane/evidence/release-compatibility.json`;
Environment payloads cannot override that path. Isolated Python fixtures inject
the runner's declaration-file attribute. This complements the orchestrator's exact runtime image
verification; matching module names alone are insufficient.

The consumer verifies database/artifact/image/hash identities and the complete
image graph. Required installs are reconciled with installed modules; updates
expand only through installed reverse dependents. Pending work, missing
installed addons, listed changes omitted from the module plan or incomplete plans refuse execution. A
compatible release missing a required module refuses rather than performing
hidden DB work during overlap.

The existing SQL-only credential boundary commits and reads back before any
Odoo registry or hook. One database-less Odoo shell explicitly creates the
updating registry with the resolved install/update flags, then reuses it for
existing settings, website, mail fencing, admin/schema/policy and service-user
checks. Odoo shell's ordinary implicit registry does not apply update flags,
so the explicit startup is required. Required module-state and pending-work
readback and Odoo's updated-module evidence must pass before a receipt is
emitted, so an already-installed state cannot hide skipped updates. This does not unset `noupdate`,
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

Artifact-specific tags and devkit commits remain in `image.tags` and
`build_provenance.build_tools`; they are not repeated as generic build flags.
The support lock remains compatible with the current digest-bound base image;
consumer builds check its resolver constraints before source inventory work.
