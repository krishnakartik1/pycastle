# PROTOTYPE — language-agnostic initialized fixture

This throwaway prototype answers one question: what should `pycastle init`
create when PyCastle cannot infer a language, package manager, Setup command, or
Gate policy?

Run it from the repository root:

```bash
uv run python docs/prototypes/initialized-fixture/prototype.py
```

The prototype renders the complete proposed `.pycastle/` tree, the exact
content of every generated file, executable modes, and the message shown after
initialization. Toggle between host and Docker to confirm that only the
`sandbox` marker changes.

The real `pycastle init` interaction asks an attached user to choose `host` or
`docker`, and `--sandbox host|docker` skips that prompt. A non-interactive call
without the flag fails with remediation instead of choosing a Sandbox
implicitly.

The proposal deliberately makes a fresh fixture safe but not immediately able
to finish a Run:

- `setup` is a documented, executable no-op. The project opts into durable
  preparation explicitly.
- `gate` is executable and fail-closed. It tells the maintainer what to replace
  and never reports a vacuous pass.
- `main.py` places Gate nodes visibly in the Item and After-Run execution
  graphs, including repair cycles.
- `Dockerfile` supplies PyCastle's neutral Runtime substrate and marks one
  project-owned toolchain extension point.
- no repository files or dependency manifests affect the generated tree.

Once the fixture shape is settled, capture the verdict in the Wayfinder ticket
and delete this directory rather than turning the prototype into production
scaffolding.
