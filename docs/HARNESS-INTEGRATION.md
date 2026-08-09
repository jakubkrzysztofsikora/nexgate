# Claude Code And Codex Integration

Start NexGate first with `make up`, then wire a project:

```bash
bin/nexgate install /path/to/project
```

The installer reads the local NexGate `.env`, merges a gateway overlay into the
target project's `.claude/settings.local.json`, and adds a managed LiteLLM
Responses API provider to `~/.codex/config.toml`. Existing unrelated settings
are preserved and NexGate-managed changes can be removed with:

```bash
bin/nexgate restore
```

Claude Code should be launched with `claude --strict-mcp-config` so MCP
servers cannot silently bypass the configured gateway. Codex uses the NexGate
Responses API provider and the `CODEX_DEFAULT_MODEL` value from `.env`; set
`CODEX_GATEWAY_URL` only when the gateway is intentionally remote.

The installer writes the LiteLLM master key into a mode-600 Codex environment
file because Codex resolves provider credentials from its process environment.
Treat that local file as a credential, never commit it, and use `restore` when
removing NexGate from a machine.
