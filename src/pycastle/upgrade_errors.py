"""Exceptions shared by fixture migrations and the upgrade engine."""


class FixtureUpgradeError(Exception):
    """Raised when an upgrade cannot complete without risking the fixture."""
