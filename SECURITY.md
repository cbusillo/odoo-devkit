# Security Policy

## Supported Versions

Security fixes target the current `main` branch. There are no separate
supported release lines.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/odoo-devkit/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the commit, the command you ran, the impact, and the smallest steps
that reproduce it.

Do not send database dumps, customer data, credentials, tenant names, or
other personal data. Use redacted or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- generated workspace or runtime files exposing secrets or credentials;
- data workflow commands that leak or mishandle database contents;
- commands that act on a different environment or database than the one
  requested; and
- dependency or GitHub Actions supply-chain problems.

Problems in Odoo itself should go to Odoo S.A.
