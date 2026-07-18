"""Regression tests for PyCastle's public trust boundary."""

import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
LICENSE = ROOT / "LICENSE"
SECURITY = ROOT / "SECURITY.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PYPROJECT = ROOT / "pyproject.toml"

APACHE_2_LICENSE_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)


def test_distribution_uses_the_canonical_apache_2_license() -> None:
    metadata = tomllib.loads(PYPROJECT.read_text())
    license_bytes = LICENSE.read_bytes()

    assert metadata["build-system"]["requires"] == ["hatchling>=1.27"]
    assert metadata["project"]["license"] == "Apache-2.0"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert hashlib.sha256(license_bytes).hexdigest() == APACHE_2_LICENSE_SHA256


def test_security_policy_provides_a_private_reporting_path() -> None:
    security = SECURITY.read_text()

    assert "latest tagged PyCastle release" in security
    assert "Do not open a public issue" in security
    assert (
        "https://github.com/krishnakartik1/pycastle/security/advisories/new" in security
    )
    assert "krishnakartik1@gmail.com" in security


def test_contributing_documents_setup_ci_and_license_terms() -> None:
    contributing = CONTRIBUTING.read_text()

    assert "uv sync --locked --extra dev" in contributing
    assert ".pycastle/gate" in contributing
    assert "/setup-matt-pocock-skills" in contributing
    assert "`Required checks`" in contributing
    assert "Apache License 2.0" in contributing
    assert "does not require a separate Contributor License Agreement" in contributing
    assert "Developer Certificate of Origin" in contributing
