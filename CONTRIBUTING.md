# Contributing

Contributions will be accepted only in the clean public repository after its
publication gate passes. Until then, this repository is a private operational
workspace.

The public baseline uses `make doctor`, `make configure`, `make demo`, and
`make validate`. These commands must work without an account, `.env`, home
directory credential mounts, or live provider access. Keep provider adapters
disabled by default, use sanitized fixtures, and do not add personal endpoints
or routing policy.

Before submitting a change, run `make validate`. Never commit credentials,
database archives, logs, generated state, or account-linked configuration.
