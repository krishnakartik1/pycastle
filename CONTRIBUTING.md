# Contributing to PyCastle

PyCastle accepts focused bug fixes, documentation improvements, and changes
that advance an agreed GitHub issue.

## Set up the repository

Fork or clone the repository, then install the locked development environment:

```bash
uv sync --locked --extra dev
```

Before opening a pull request, run the project-owned Gate from the repository
root:

```bash
.pycastle/gate
```

## Set up the Issue source

GitHub Issues is PyCastle's Issue source. Follow the repository's committed
agent guidance when triaging or working an issue. To configure a repository
that does not have that guidance yet, install Matt Pocock's engineering skills
and invoke the upstream setup workflow in that repository:

```text
/setup-matt-pocock-skills
```

Choose GitHub Issues and keep the default triage labels. The complete tracker
onboarding path, including PyCastle's required workflow labels, is documented
in the README under **Configure the GitHub Issue source**.

## Open a pull request

Keep each pull request scoped to its issue, add regression coverage for changed
behavior, and explain how the change was verified. Pull requests run the full
Gate, build the package, and test supported Python versions in CI. The aggregate
`Required checks` result must pass before merge.

## Contribution license

PyCastle is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution, you agree that it may be distributed under that license. The
project does not require a separate Contributor License Agreement (CLA) or
Developer Certificate of Origin (DCO) sign-off.
