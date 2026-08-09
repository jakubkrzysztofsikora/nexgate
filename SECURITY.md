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
