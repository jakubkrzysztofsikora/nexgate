# Portable Quickstart

The demo path does not contact a provider, download a model, or require a
cloud or agent account. The full NexGate stack uses an ignored `.env` overlay:
the operator supplies credentials and endpoints only for the routes they want.

```bash
make doctor
make configure-demo
make demo
make validate
```

`make configure-demo` writes `.env.demo` with a unique local demo key and never
overwrites `.env`. `make demo` runs only `compose.demo.yaml`, which exposes an
OpenAI-compatible mock endpoint on `127.0.0.1:4010`. Stop it with:

```bash
make demo-down
```

The mock accepts the generated `DEMO_API_KEY` for `/v1/models` and
`/v1/chat/completions`; it never contacts an external service. Cloud,
native-model, observability, and account-linked workflows are evaluated only
as opt-in modules after their source, provenance, and fixtures pass review.

## Full Stack

Run `make configure`, edit the generated `.env`, then run `make up`. NexGate
will start LiteLLM, Postgres, and Redis on loopback and render only the routes
whose variables are complete. Add `make observability` for Prometheus and the
provisioned Grafana dashboard. See [Provider And Model Matrix](PROVIDER-MATRIX.md)
and [Harness Integration](HARNESS-INTEGRATION.md).

ChatGPT subscription aliases stay disabled by default. Their OAuth state is
persisted under ignored runtime/state/chatgpt; once the operator has completed
that login, set NEXGATE_ENABLE_SUBSCRIPTION_ROUTES=true and restart NexGate.
