# Renovate

Renovate keeps dependencies current in the owner's repositories
([Q657](https://github.com/cbusillo/odoo-devkit/issues/248#issuecomment-6342458732)).
Dependabot no longer runs version updates; GitHub security alerts stay on, and
Renovate opens a pull request for each alert it can fix.

## Presets

- [`renovate/default.json`](../../renovate/default.json) is the shared setup:
  a weekly schedule, one grouped pull request for minor and patch updates,
  majors as separate pull requests, no automerge, and the Client test notes
  section that Client repositories require. It also tracks dependencies
  pinned in PEP 723 `# /// script` blocks in any Python file, including under
  `tests/`, which `config:recommended` otherwise skips; Renovate's `pep723`
  manager matches no files unless a config names them. Pull requests
  land through the merge train after an agent's review, like any engineering
  change.
- [`renovate/devkit-pin.json`](../../renovate/devkit-pin.json) moves a
  consumer's exact odoo-devkit pins, in `workspace.toml` and in workflow
  `uses:` references, to the head commit of devkit `main`
  ([build-tool upgrades](build-tool-upgrades.md#moving-the-pin)).
  It also moves `[repos.shared_addons].ref` to shared-addons `main` as an
  exact commit when `[build].odoo_version` is `20.0` and the repository is
  `cbusillo/odoo-shared-addons`. Renovate reads the consumer's base branch
  manifest, so landing its Odoo 20 conversion activates these updates; an
  unmerged conversion does not. Odoo 19 shared-addons pins stay unchanged.
  Shared-addons updates use ordinary visible pull requests, preview checks
  and the merge train. Production still requires Client release acceptance.

## Repository Config

A repository with dependencies to keep current has a root `renovate.json`
that extends the shared preset and adds only its own rules, each with a
`description` of why it exists:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github>cbusillo/odoo-devkit//renovate/default"]
}
```

A repository with nothing to manage sets `"enabled": false` instead, so the
Renovate App opens no onboarding pull request there. Forks are skipped.

## Native CI Images

Devkit's live-check matrix keeps each image pinned as `series-devtools@sha256:…`.
The repository's regex manager extracts these matrix inputs, which the built-in
GitHub Actions manager does not track. Renovate's `exact` versioning keeps each
tag unchanged and proposes digest updates from its existing stable devtools
channel. CI still uses the pinned digest and runs both series' full suites and
planted faults before the update can land.

The shared weekly schedule and review process still apply. This reduces manual
repinning; it does not guarantee that a pin survives the producer's rolling
retention across many publications or a delayed review.
