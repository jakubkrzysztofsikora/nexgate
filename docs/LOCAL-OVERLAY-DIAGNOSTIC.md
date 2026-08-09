# Local Overlay Adapter Diagnostic

`make adapter-diagnostic` makes exactly one small completion request to an
operator-selected adapter. It is manual-only, refuses to run when `CI` is set,
has a maximum ten-second network timeout, and never prints provider responses,
endpoint values, configuration errors, or credentials.

Create an ignored local environment file outside the repository, source it in
your shell, and explicitly confirm the action:

```bash
export GATEWAY_PROVIDER=openai-compatible
export OPENAI_COMPATIBLE_BASE_URL=https://operator-selected.example
export OPENAI_COMPATIBLE_API_KEY=operator-owned-value
export GATEWAY_DIAGNOSTIC_MODEL=operator-selected-model
export GATEWAY_DIAGNOSTIC_CONFIRM=run
export GATEWAY_DIAGNOSTIC_TIMEOUT_SECONDS=10
make adapter-diagnostic
```

For an Anthropic-compatible endpoint, substitute
`GATEWAY_PROVIDER=anthropic-compatible` and the two `ANTHROPIC_COMPATIBLE_*`
variables. Do not put this overlay in `.env`, shell history, issue reports, or
the repository. The command is intentionally not part of `make validate` or
CI and a successful probe is not a provider certification.
