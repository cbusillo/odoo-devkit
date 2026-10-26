# Tenant Consumer-Build Checks

## Source-only PR screening

After checking out the tenant, devkit and shared addons at the consumer's
declared revisions, a secretless PR can invoke:

```bash
uv --directory ../odoo-devkit run --locked platform workspace check-source \
  --manifest "$PWD/workspace.toml" --related-repo-root "$PWD"
```

`--related-repo-root` resolves relative devkit/runtime/shared paths from that
directory; omit it when the manifest's paths already match the checkouts.
The command never runs workspace sync. Missing required local checkouts fail.
Setup may download devkit's public Python dependencies; validation itself uses
only local Git metadata and offline uv lock checking and frozen export.
It requires the complete `[build]` declaration, validates every declared
artifact source (including context/instance overrides) without resolving it,
and checks the tenant/shared dependency workspace and tracked Python/XML
syntax without importing code or writing bytecode. It does not run Odoo or
addon suites, resolve private refs, read runtime environment payloads, log in,
or inspect/pull/build/publish images. Keep existing repository-specific Node,
SCSS and helper checks alongside it.

Success prints screening JSON with `validation = "source-only"` and
`full_build_required = true`. There is no artifact-manifest output option or
deployable image. Keep the production manifest complete, including private
inputs; this command defers their resolution rather than removing them.
`runtime publish` and `runtime check-artifact` still require every intended
input and fail on missing private sources, base images or build/test errors.
Source authentication comes only from the source-token resolver; a registry
read or publish token is never substituted for it.

Consumer rollout is gated by the deployed Launchplane admission guard from
[launchplane#5035](https://github.com/cbusillo/launchplane/pull/5035), not its
source merge alone. Confirm it is active before enabling source-only workflow
success. Secretless screening cannot replace full Build and applicable addon
tests on the exact trusted train candidate. Preserve ordinary trusted PR
previews and main's immutable artifact publication. A trusted run missing
private inputs must fail rather than selecting screening automatically.
Recheck repository merge methods and CODEOWNERS; any original-PR landing route
needs full Build coverage on its constructed candidate through the canonical
train push before source-only admission is enabled.

## Full production consumer build

A devkit PR is checked against tenant `main` before the preparing agent posts
`Ready for the merge train`. The tenant runs the check with its existing source
and base-image read access. The agent dispatches and watches through the
configured GitHub App; devkit CI needs no tenant token. The rollout scope and
dispatch decision are recorded on
[odoo-devkit#130](https://github.com/cbusillo/odoo-devkit/issues/130#issuecomment-6061496295).
Use the tenant repositories named by that work record and verify their current
manifests rather than keeping another site inventory here.

## Dispatch and evidence

1. Commit and push the devkit task branch through the bot helpers. Capture the
   PR's exact current head SHA from a branch in the declared devkit repository.
   For a fork contribution, first review and bring the change onto an authorized
   bot-pushed task branch in that repository. The tenant workflow verifies branch
   containment before executing candidate code with its existing secret bindings.
2. In each affected tenant repository, dispatch `build-tool-drift.yml` with
   `devkit_commit` set to that SHA to run its `consumer-build` job. Build-tool
   drift surfaces on the tenant's Renovate pin pull request instead
   ([build-tool upgrades](build-tool-upgrades.md#moving-the-pin)).

   ```bash
   uv run /path/to/codex-skills/skills/github/scripts/github_workflow_babysit.py dispatch \
     --repo <owner/tenant-repository> \
     --workflow build-tool-drift.yml --ref main \
     --field devkit_commit=<40-character-devkit-pr-head> \
     --timeout-seconds 2100
   ```

   For initial workflow validation, use the tenant PR's task branch as `--ref`.
   The job still checks out tenant `main`. Subsequent devkit PRs use the landed
   tenant workflow on `main`. No protected-environment approval is part of this
   build-only path.
3. Wait for each exact returned run ID. Read its `consumer-build` job result and
   retained `consumer-build-check-<attempt>` JSON. Verify `runtime_commit`
   matches the devkit PR head, `source_commit` identifies the tenant commit,
   and the platform matches the tenant's production build. Record tenant and
   devkit SHAs, run URLs/attempts, conclusions, and evidence on the devkit PR.
4. A failure, missing run/evidence, skipped consumer job, or changed devkit head
   prevents Ready. Resolve the failure and dispatch the current head again.
   Keep ordinary devkit CI and the applicable model review/inspection evidence
   alongside the consumer results. Then post `Ready for the merge train` and
   hand routing to the receiving session under the repository's landing rules.

This is an agent-run readiness gate, not a new GitHub required status check.
The supported path does not give the App Checks or Commit statuses write
access, put tenant credentials in devkit, or introduce a CI dispatcher.

## Build-only command

The tenant job checks out the requested devkit commit as both devkit and
runtime, without editing its tracked manifest. It invokes:

```bash
uv --directory ../odoo-devkit run platform runtime check-artifact \
  --manifest "$PWD/workspace.toml" --instance artifact \
  --devkit-commit <40-character-devkit-pr-head> --platform linux/amd64 \
  --output-file /path/outside/source/consumer-build-check.json
```

The existing GHCR publish binding is used only for base-image reads in this
check; it remains capable of writing packages. The workflow's branch-containment
check therefore runs before candidate code and credentials meet.

The command requires the executing devkit and runtime to be the same checkout
at the requested exact SHA. It overrides their manifest refs in memory and
uses the production artifact publisher's source verification, staging, lock
and build-tool checks, base-image provenance, dependency preflight, and
`docker/artifact.Dockerfile` production target. Buildx Bake passes every requested
platform's production result directly to the shared
`docker/dependency-evidence.Dockerfile` through a
[named target context](https://docs.docker.com/build/bake/contexts/#using-a-target-as-a-build-context).
Only evidence is exported locally; the image stays in BuildKit's cache without
being pushed or loaded. The publisher's dependency aggregator validates the
evidence schema, platform coverage, and exact staged tenant/support lock hashes
before the check succeeds. Base-image authentication uses the existing read
path; image push authentication is omitted. Temporary evidence is removed when
the command exits.

The JSON records built source commits, platforms, resolved addon inputs and
base-image digests, staged lock hashes, and validated in-image
`dependency_provenance`. It is build-check evidence, not a
deployable artifact manifest. The workflow does not publish an image or ask
Launchplane to deploy, promote, or change data. Credentials remain in the
tenant's existing secret bindings. Local Compose `runtime build` continues to
serve local development; it does not validate the production artifact.
