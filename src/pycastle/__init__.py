"""PyCastle: a reusable, installable autonomous development loop."""

from importlib.metadata import version

from packaging.version import Version

# The built distribution metadata, generated from ``pyproject.toml``, is the
# single package-version authority.  Normalizing at this boundary gives the CLI,
# scaffolder, and compatibility checker exactly one representation.
__version__ = str(Version(version("pycastle")))
