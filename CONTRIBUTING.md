# Contributing

Contributions are welcome when they preserve the portable, offline-first
baseline. Start with an issue for substantial behavior or design changes.

The public baseline uses `make doctor`, `make configure`, `make demo`, and
`make validate`. These commands must work without an account, `.env`, home
directory credential mounts, or live provider access. Keep provider adapters
disabled by default, use sanitized fixtures, and do not add personal endpoints
or routing policy.

Before submitting a change, run `make validate`, `make adapter-contract`, and
`make secrets`. Install the local prevention hook with `make hooks`.

Never commit credentials, database archives, logs, generated state, or
account-linked configuration. Keep adapter tests local and sanitized; live
operator diagnostics are manual-only and must never become a CI requirement.
