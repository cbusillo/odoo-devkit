# Artifact Inputs

Purpose

- Define the repo-owned source-input contract for publish and runtime selection.

When

- When a repo needs publish-time source selection to live with the product repo
  instead of hiding in runtime config.

## Principle

- Keep source input intent versioned in the repo that owns the build.
- Keep `workspace.toml` focused on workspace assembly and runtime targeting.
- Keep runtime stack config focused on local runtime selection state.
- Keep hosted runtime values in Launchplane records. `artifact-inputs.toml`
  may override source repository selection, not hosted runtime authority.
- Keep artifact, release, and deploy truth pinned to resolved SHAs and image
  digests after publish.

## File posture

- The default file name is `artifact-inputs.toml` beside `workspace.toml`.
- `workspace.toml` can override the path with `[artifacts].inputs_file` when a
  repo needs a different layout.
- `platform runtime` reads this file for source repository selection used by
  local runtime and publish flows.

## Immutable publish handoff

Before `platform runtime publish`, run `platform dependencies check` and commit
the exact tenant, devkit, and shared-addon inputs. Publish fails closed for
dirty source repos, nonordinary index flags or Git replacement refs, untracked
or symlinked staged files, stale/missing tenant lock pairs, mutable VCS refs,
source-supplied `.odoo-python-source.json` markers, staged-byte changes, or an
addon build backend that is absent at the exact version from both the devkit
support lock and tenant lock catalog. When the manifest includes the devkit
repo, `platform dependencies check` reports that build-tool mismatch before the
publish workflow reaches Buildx.
Devkit alone writes those markers from the verified Git snapshots used for the
build. Each recorded source commit must also be advertised by a ref in its
normalized GitHub origin; changing only `.git/config` cannot reattribute a
local commit to another repository.

The artifact build uses two explicit uv roots:

- `/opt/runtime` contains devkit's static support/runtime catalog and lock; its
  source locator remains `docker/runtime-python/uv.lock` in the devkit commit.
- `/opt/project` contains the tenant root workspace, tenant lock, and the exact
  tenant/shared-addon member metadata validated by the dependency checker.

Artifact publish uses `docker/artifact.Dockerfile`; the existing
`docker/Dockerfile` remains the local Compose build contract. Shared-addon
members receive a nested source marker so package inventory attributes their
installed distributions to the shared repository/commit while the tenant lock
remains tenant-owned. Payload-owned config, scripts, external addons, and prior
dependency evidence are cleared before the staged artifact inputs are copied,
so base-image residue cannot survive into the final image.

Both configured base images are resolved to registry digests before the build,
and their OCI source/revision labels must agree across every requested
platform. After Buildx pushes the artifact, devkit extracts dependency evidence
from the resulting immutable image digest—not from the mutable tag—and emits
Launchplane artifact-manifest schema v2.

The v2 handoff records both lock hashes and source commits, exact per-platform
Python package inventories, external compatibility descriptors, base-image
provenance, devkit build-tool provenance, and the final artifact digest. It
never persists secret values, authenticated URLs, local absolute paths, or
operator-local overrides. Dependencies may not be installed after evidence is
written; they must be represented by one of the two locks or exact external
compatibility evidence.

The current layout-2 producer marker contract uses GitHub-style
`owner/repository` identities. A non-GitHub tenant, devkit, or shared-addon
origin fails before build rather than passing a URL that the image producer
cannot consume.

## Current schema

```toml
schema_version = 1

sources = [
  { repository = "owner/repo", selector = "main" },
  {
    repository = "owner/another-repo",
    exact_ref = "411f6b8e85cac72dc7aa2e2dc5540001043c327d",
  },
]

[contexts.testing]
sources_add = [
  { repository = "owner/release-overrides", selector = "release-19" },
]

[contexts.testing.instances.prod]
sources_add = [
  {
    repository = "owner/hotfix",
    exact_ref = "89e649728027a8ab656b3aa4be18f4bd364db417",
  },
]
```

Rules:

- `schema_version` must be `1`.
- Each `sources` or `sources_add` entry must set exactly one of `selector` or
  `exact_ref`.
- Context and instance entries merge by repository identity, so a later entry
  for the same repository overrides an earlier one.

## Odoo example

This is the current live use case for tenant runtime publish:

```toml
schema_version = 1

sources = [
  { repository = "cbusillo/disable_odoo_online", selector = "main" },
]
```

## Non-Odoo example

This example is intentionally product-neutral. It shows the same selector file
shape for an application repo that wants publish-time source intent tracked in
git even when the surrounding runtime workflow is not Odoo-shaped.

```toml
schema_version = 1

sources = [
  { repository = "every-inc/verireel-core", selector = "stable" },
  { repository = "every-inc/verireel-ui", selector = "release-2026-04" },
]

[contexts.preview]
sources_add = [
  { repository = "every-inc/verireel-ui", selector = "pr-preview" },
]

[contexts.preview.instances.demo]
sources_add = [
  {
    repository = "every-inc/verireel-branding",
    exact_ref = "7d33f3a0c6c1f8b675d619d8ad5ec4d9820f2c19",
  },
]
```

Today, `platform runtime publish` still wires these resolved entries into the
current Odoo runtime environment. The schema itself is repo-owned and neutral,
which is why this example belongs here even before a full non-Odoo runtime path
lands.

For v1, keep this file fully repo-local rather than introducing a shared
catalog. The neutral source-input surface is already the cross-product contract;
the current Odoo-facing runtime and artifact edge names can stay product-
specific until a second real driver/runtime path proves a rename is worth it.

## Working rule

- If a repo needs publish-time source selection, add or edit
  `artifact-inputs.toml` in that repo.
- Do not put source selector intent back into `stack.toml` as a convenience
  shortcut.
