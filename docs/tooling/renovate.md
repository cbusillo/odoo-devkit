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
  manager matches no files unless a config names them. Pull requests land
  through the merge train. Qualifying signed dependency updates can enter
  automatically, and updates that need agent review use checked Ready.
- [`renovate/devkit-pin.json`](../../renovate/devkit-pin.json) moves a
  consumer's exact odoo-devkit pins, in `workspace.toml` and in workflow
  `uses:` references, to the head commit of devkit `main`
  ([build-tool upgrades](build-tool-upgrades.md#moving-the-pin)).
  It also moves an already exact `[repos.shared_addons].ref` pin for
  `cbusillo/odoo-shared-addons` to its `main` head commit when
  `[build].odoo_version` is `20.0`. Renovate reads the consumer's base branch
  manifest, so landing its Odoo 20 conversion activates these updates; an
  unmerged conversion does not. Odoo 19 shared-addons pins stay unchanged.
  Shared-addons updates use ordinary visible pull requests, preview checks
  and the merge train. Production still requires Client release acceptance.
  For Community 20, the same preset proposes both digest-only base-image pins
  together from the stable runtime and devtools tags. It also repairs one stale
  pin when the other already matches. Before landing, check that both proposed
  digests identify the same producer run in their manifest annotations. The two
  stable tags are promoted separately.
  When the base image changes Python series, update the image pins, the
  workspace Python version and the affected projects' `requires-python`
  ranges together, then regenerate the lockfile. The tenant preset disables
  Renovate's independent updates to those ranges.

## Landing product pins

Before marking a dependency update Ready, read the product's current release
record and check whether an exact build is waiting for a person. When the
release seat is preparing that handoff, coordinate with it and post a
[waiting handoff](https://github.com/cbusillo/launchplane/blob/main/docs/merge-train-policy.md#ready-handoffs)
on the product's open dependency update PRs. Start each trusted PR comment
with this line.

```text
Engineering ready; waiting for the release seat to confirm the exact-build attempt ran.
```

Apply the same wait as new proposals and refreshed heads appear, including
updates that qualify for automatic admission. Verify the train shows the wait.
If a candidate is already in the train, reconcile its current state with the
release seat. If a landing has already started, check which build remains
eligible for the release attempt after it finishes.

Keep the wait until the release seat confirms that the release attempt ran
and records its candidate and result in the release issue. A release approval
or elapsed time leaves the wait in place. Then check the current PR heads
and resume through the normal checked handoff. Renovate continues proposing
updates, and other products continue through their normal train.

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
