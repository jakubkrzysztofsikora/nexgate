# Security Policy

## Supported versions

The future public `v0.1.x` release will receive security fixes. This private
candidate repository is not yet a supported public distribution.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret.
Use the repository's private GitHub Security Advisory reporting channel once
the public repository exists. Include reproduction steps, impact, and affected
version, but never include credential values. If that channel is unavailable,
contact the published maintainer address after the release lead has configured
one.

We target acknowledgement within 7 days and an initial remediation assessment
within 30 days. We follow coordinated disclosure: reporters and maintainers
agree on a fix and disclosure timeline before publication, except where users
need earlier protection.

## Secrets

Treat any credential committed to this repository's legacy history as
compromised. Do not test, validate, or paste suspected credentials into issues,
logs, CI output, or pull requests. Report only the opaque path, commit, and
scanner rule to the private channel.
