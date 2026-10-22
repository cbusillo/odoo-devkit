# Renovate

Renovate keeps dependencies current in the owner's repositories
([Q657](https://github.com/cbusillo/odoo-devkit/issues/248#issuecomment-6342458732)).
Dependabot no longer runs version updates; GitHub security alerts stay on, and
Renovate opens a pull request for each alert it can fix.

## Presets

- [`renovate/default.json`](../../renovate/default.json) is the shared setup:
  a weekly schedule, one grouped pull request for minor and patch updates,
  majors as separate pull requests, no automerge, and the Client test notes
  section that Client repositories require. Pull requests land through the
  merge train after an agent's review, like any engineering change.
- [`renovate/devkit-pin.json`](../../renovate/devkit-pin.json) moves a
  consumer's exact odoo-devkit pins, in `workspace.toml` and in workflow
  `uses:` references, to the head commit of devkit `main`
  ([build-tool upgrades](build-tool-upgrades.md#moving-the-pin)).

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
