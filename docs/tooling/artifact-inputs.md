# Artifact Inputs

Purpose

- Define the repo-owned source-input contract for publish and runtime selection.

When

- When a repo needs publish-time source selection to live with the product repo
  instead of hiding in runtime config.

## Principle

- Keep source input intent versioned in the repo that owns the build.
- Keep `workspace.toml` focused on workspace assembly and runtime targeting.
- Keep runtime stack config focused on runtime selection state.
- Keep artifact, release, and deploy truth pinned to resolved SHAs and image
  digests after publish.

## File posture

- The default file name is `artifact-inputs.toml` beside `workspace.toml`.
- `workspace.toml` can override the path with `[artifacts].inputs_file` when a
  repo needs a different layout.
- `platform runtime` reads this file for source repository selection used by
  local runtime and publish flows.

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

## Working rule

- If a repo needs publish-time source selection, add or edit
  `artifact-inputs.toml` in that repo.
- Do not put source selector intent back into `stack.toml` as a convenience
  shortcut.
