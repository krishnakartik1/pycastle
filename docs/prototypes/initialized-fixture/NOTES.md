# Prototype verdict

The proposed initialized fixture is accepted as the concrete reference for the
language-agnostic specification.

- `pycastle init` never detects or describes detecting a language, manifest,
  package manager, or toolchain.
- An attached user chooses `host` or `docker`; scripted initialization supplies
  `--sandbox`. Non-interactive initialization without that flag fails with
  remediation rather than selecting implicitly.
- Host and Docker initialization produce the same tree and content except for
  the `sandbox` marker.
- `setup` is a mandatory, executable, documented no-op.
- `gate` is a mandatory, executable, explanatory failure until the project
  replaces it with its verification policy.
- The default Item graph has one Gate node:
  `plan -> implement -> review -> verify -> DONE`. Review fixes its own findings;
  a failed `verify` visit enters `repair -> verify`.
- The default After-Run graph likewise has one Gate node. Run review fixes and
  commits its own findings, then Run report describes the candidate diff before
  the final Gate. A failed Gate enters Run repair, regenerates the report, and
  revisits the Gate; no Runtime node runs after a passing Gate.
- The Gate-node name `verify` is graph identity only; it invokes the one frozen
  `.pycastle/gate` executable.
- The project Dockerfile contains PyCastle's neutral Runtime bootstrap and a
  visible project-toolchain extension point, never inferred content.

The production scaffold is intentionally unchanged by this prototype. Delete
the prototype after its decisions have been absorbed by the specification.
