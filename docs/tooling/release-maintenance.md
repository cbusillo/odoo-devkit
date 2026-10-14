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

Each base source carries one `source-inventory.sha256` dependency record: its
SHA-256 covers sorted-key compact JSON of every inventoried file, ordered by
path, including content hashes and classifications. The digest binds additions,
removals and file counts without embedding the framework/Enterprise file list
in the uploaded manifest. The full resolved module graph and detailed tenant,
addon, tool and external inputs remain in the declaration. Base changes still
require the examined-input plan above; the bounded record uses the existing
Launchplane file schema. Architecture differences retain platform-prefixed
digest records, which the running-image declaration check verifies as before.
Detailed inventory paths retain safe components. Names containing spaces or
other unsupported characters use a `name-<sha256>` component derived from the
exact original name, preserving the suffix and content hash. This keeps tracked
IDE run configurations ingestible without omitting their dependency evidence.

Unreadable declared XML or computed menu-icon paths mark the inventory
incomplete and identify the module/path in the build log. Legacy publication
continues; targeted execution refuses. Use the standard attribute, text or
literal-expression icon path to make its database resource dependency explicit;
builds never execute an XML expression to guess it.

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

## Native website settings

The environment-overrides workflow and web startup consume Launchplane's
optional `website_settings` object in the same Odoo transaction as website
bootstrap. Launchplane supplies the accepted artifact's native `website_id`,
public HTTPS origin, CM indexing choice, GA4 Measurement ID (or empty),
`cookies_bar` and `block_third_party_domains`. Analytics requires both consent
controls. The native domain must match the declared origin; a mismatch refuses
startup before any settings write. Changing the managed domain goes through
Launchplane's supported website intent and accepted release path. Write and
flushed/invalidation readback disagreement aborts the transaction. After commit,
the caller emits the verified digest without printing the Measurement ID.

Every nonproduction data workflow and web startup clears each native website's
GA4 key and disables `cm_website.public_indexing_enabled`, including restored
production copies, empty/unknown instances and `--no-sanitize`. The diagnostic
is scoped to native GA4 and the CM addon parameter. It does not claim generic
indexing exclusion for tenants without CM, or removal of Plausible, GTM or
arbitrary custom-code tracking. Exclusion needs no settings payload.

Native Odoo consent remains Essential only / Accept all. Client acceptance of
wording, event choices and actual collection remains with the product. This
consumer creates no acceptance, credentials or remote operation.

Launchplane selects the previous artifact's accepted settings on rollback. For
an artifact predating this contract, it must send an explicit `disable_collection` object using the forward intent's verified
website ID and public origin. That operation disables native GA4 and the CM
indexing parameter while preserving unrequested cookie/third-party controls. Launchplane's
stopped-web maintenance driver executes its generated copy of this committed
consumer through the runtime's existing native shell, even when the older image
has no consumer. An absent production object preserves existing fields; it is
not a rollback instruction. Consumer or commit failure keeps web stopped.
