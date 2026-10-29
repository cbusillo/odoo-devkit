# When Odoo source is not the final answer

Keep this reference at **550 words maximum**, including headings and link labels.
These lessons were distilled from the retired
[Odoo Intelligence code](https://github.com/cbusillo/odoo-intelligence-mcp/tree/4f2f5aa068acc7aeac32ca6c1e798a6b840bfe87/src/odoo_intelligence_mcp).
Use source search to locate candidates; use the exact tenant registry to settle
questions that depend on installed addons or database customizations.

**Models.** A class found by name is only one contribution. The archived AST
index skipped extensions without `_name` and replaced earlier entries with the
same name; its inheritance graph could not establish the final model. Addon
dependencies constrain load order, while `type(env[model]).__mro__` shows the
assembled method order. Inspect each class's own `__dict__` for the method in
question and follow its `super()` calls. Delegation through `_inherits` exposes
parent fields through a relation, not the parent's methods. See
[ORM inheritance](https://www.odoo.com/documentation/19.0/developer/reference/backend/orm.html#inheritance-and-extension).

**Fields.** Inspect `env[model]._fields[field]` after registry setup for the
effective `compute`, `related`, `store`, `compute_sudo`, and relation metadata;
one field declaration or one method's decorator is insufficient. Compute can
be a method name, callable or generated related-field computation. Inspect
setup and the whole MRO, rather than assuming one decorator supplies all
dependencies. Follow each relational hop in its own model. Use the selected
Odoo version's resolved dependency metadata and prove
recomputation with a small native test. The archived substring-based reverse
dependency search and swallowed resolution errors were approximations, not
proof that no dependency exists. See
[field resolution](https://github.com/odoo/odoo/blob/19.0/odoo/orm/fields.py).

**Views.** A list of XML files or regex matches in individual view records
does not show the rendered form. Resolve the action's actual primary view,
then its inheritance tree: extension children apply depth-first, with sibling
order governed by priority. XPath sees the architecture produced by preceding
extensions. Inspect the combined view for the intended user and context before
concluding a node is missing or changing priority. See
[view resolution](https://www.odoo.com/documentation/19.0/developer/reference/user_interface/view_records.html#view-resolution).

**Permissions.** A model-level grant does not prove access to a record or
field. In Odoo 19, ACL grants add together; applicable global record rules
intersect, applicable group rules unite, and the two sets intersect. No
applicable record rule means default allow after ACL access. This corrects the
archived checker's contrary suggestion. Odoo 20 uses `ir.access`; consult that
version's native access implementation rather than carrying old table queries
forward. Check the exact operation and record as the intended user, without superuser mode,
with the relevant company context and field restrictions. See
[19 security](https://www.odoo.com/documentation/19.0/developer/reference/backend/security.html)
and [20 access](https://github.com/odoo/odoo/blob/20.0/odoo/addons/base/models/ir_access.py).

**Registry evidence.** For code-only questions, use a small native test through
[addon-CI](tooling/addon-ci.md) with the selected tenant image and addons. For
database customizations, use an authorized disposable, sanitized restore with
matching source, image and module state; a fresh database cannot settle these.
Submit heavy workloads through the existing
[heavy-run entrypoint](https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/heavy-runs.md)
and run a bounded native Odoo shell script inside the job to inspect the
objects above. For an existing local workspace, the
[manifest-backed shell](tooling/workspace-cli.md)
is local-only. Follow its documented command contract; it is not a remote
execution route. Retain source/image versions, module state, user/context and
output as evidence. Limit the conclusion to the inspected fixture.
