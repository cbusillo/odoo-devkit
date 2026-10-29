# When Odoo source is not the final answer

Keep this reference at **550 words maximum**, including headings and link labels.
Checked against cached Community source on October 29, 2026.
Target Odoo 20; Odoo 19 appears
only for migration differences. The linked revisions are evidence snapshots,
not tenant pins. Use the exact tenant registry for installed-addon or database
customizations; the full two-version source audit is on [PR #301](https://github.com/cbusillo/odoo-devkit/pull/301).

**Models.** A class declaration is one contribution. Addon dependencies
constrain loading; `_inherit` contributions form the registry class.
Inspect `type(env[model]).__mro__`, each class's own `__dict__`, and its
`super()` calls to trace a method. `_inherits` delegates fields through
relations; it does not add the parent's methods to that MRO.
See [class assembly](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/odoo/orm/model_classes.py#L167-L240)
and [delegated fields](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/odoo/orm/model_classes.py#L512-L540).

**Fields.** On the concrete model using the field, inspect
`env[model]._fields[field]` after registry setup for the
effective `compute`, `related`, `store`, `compute_sudo`, and relation metadata;
one declaration is insufficient. Dependencies use explicit field `depends`,
else the `related` path, else compute methods' metadata (across the MRO for
named methods).
Compute may also be callable; related setup supplies `_compute_related`.
Inspect `env.registry.field_depends[field_object]`, follow relational hops
in their own models, and prove recomputation with a small native test.
See [dependency and related setup](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/odoo/orm/fields.py#L599-L675).

**Views.** A list of XML files or regex matches in individual view records
does not show the rendered form. Resolve the action's actual primary view,
then its inheritance tree: extensions apply depth-first, siblings by priority
then ID. XPath sees preceding extensions' output. Inspect the combined
architecture and user-facing postprocessed view as the intended user with
the intended context before declaring a node missing or changing priority. See
[ordering](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/odoo/addons/base/models/ir_ui_view.py#L738-L784)
and [combination](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/odoo/addons/base/models/ir_ui_view.py#L984-L1055).

**Permissions.** Odoo 20 uses `ir.access`: applicable group permissions unite,
global restrictions intersect; no permission means denial. Parent access
domains restrict `_inherits` children when `_check_inherits_access` is true
(the default); native models such as `mail.mail` explicitly set it false.
Odoo 19 first checks ACL grants, then combines global/group record rules;
`ir.rule._compute_domain` also adds stored parent-relation rule domains.
Do not infer parent-rule preservation from field delegation. See
[20 evaluation](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/odoo/orm/models.py#L3603-L3653),
[mail exception](https://github.com/odoo/odoo/blob/d60b687b047701f0bac45f47f4dee875dea5ba3e/addons/mail/models/mail_mail.py#L28-L41)
and [19 rules](https://github.com/odoo/odoo/blob/977f36dc9a6f473b9cb32fa6f9e3db396fb4de13/odoo/addons/base/models/ir_rule.py#L141-L173).
Check the actual operation, record and field as the intended user, without
superuser mode, with the relevant company context.

**Registry evidence.** For code-only questions, use a small native test through
[addon-CI](tooling/addon-ci.md) with the selected tenant image and addons. For
database customizations, use an authorized disposable, sanitized restore with
matching source, image and module state; a fresh database cannot settle these.
Submit heavy workloads through the existing
[heavy-run entrypoint](https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/heavy-runs.md)
and run a bounded native Odoo shell script inside the job. The native shell
starts as superuser; change user and disable sudo for user-facing probes.
For an existing local workspace, the
[manifest-backed shell](tooling/workspace-cli.md)
is local-only. Follow its documented command contract; it is not a remote
execution route. Retain source/image versions, module state, user/context and
output as evidence. Limit the conclusion to the inspected fixture.
