# Security Policy

## Supported versions

The current `0.1.x` release line receives security fixes.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret.
Use the repository's private GitHub Security Advisory reporting channel.
Include reproduction steps, impact, and affected version, but never include
credential values. If the advisory channel is unavailable, open a minimal
public issue asking a maintainer for a private contact route; do not include
the vulnerability details in that issue.

We target acknowledgement within 7 days and an initial remediation assessment
within 30 days. We follow coordinated disclosure: reporters and maintainers
agree on a fix and disclosure timeline before publication, except where users
need earlier protection.

## Secrets

Treat any credential committed to the repository as compromised. Do not test,
validate, or paste suspected credentials into issues, logs, CI output, or pull
requests. Report only the opaque path, commit, and scanner rule to the private
channel.

## A2A Caller Scope

Agent registration and caller authorization are separate operations. The
provisioning script reads the admin credential from `LITELLM_MASTER_KEY` and
prints only the new agent ID. It does not enable public access or issue keys.
Give application callers explicit agent grants in their key/team object
permissions; do not treat a model allowlist or an empty agent list as an agent
deny rule. LiteLLM's unscoped keys can access registered agents.

Run the opt-in A2A compatibility gate with disposable test credentials before
deployment. It checks authentication and rejection of a key scoped to a
different agent against a real proxy. The test containers do not mount the
operator overlay or publish ports, and the database is disposable.
