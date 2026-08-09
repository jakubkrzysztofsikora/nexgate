# Portable Quickstart

This is the only proposed public quickstart. It does not read `.env`, mount a
home directory, contact a provider, download a model, or require a cloud or
agent account.

```bash
make doctor
make configure
make demo
make validate
```

`make configure` writes `.env.demo` with a unique local demo key and never
overwrites `.env`. `make demo` runs only `compose.demo.yaml`, which exposes an
OpenAI-compatible mock endpoint on `127.0.0.1:4010`. Stop it with:

```bash
make demo-down
```

The mock accepts the generated `DEMO_API_KEY` for `/v1/models` and
`/v1/chat/completions`; it never contacts an external service. Cloud,
native-model, observability, and account-linked workflows are evaluated only
as opt-in modules after their source, provenance, and fixtures pass review.

## Optional adapters

`config/provider-catalog.yaml` documents public adapter families. All start
disabled. An operator enables one through an untracked private overlay, sets
the listed variables, and accepts the selected provider's data-routing and
cost terms. No provider route is part of the public quickstart.
