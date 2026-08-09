# Hybrid LLM Stack Public Candidate

This is a new-root, private staging repository for a portable AI gateway. It
is not yet public and is not a copy of the legacy operational repository.

```bash
make doctor
make configure
make demo
make validate
```

The baseline requires no provider credentials, cloud account, model download,
or host credential mounts. See `MIGRATION.md` for the reviewed path to local
functional parity. No credential, archive, private endpoint, legacy Git
history, or account configuration belongs in this repository.
