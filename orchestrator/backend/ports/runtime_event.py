"""Singular compatibility module for the Runtime Event port."""

from ports.runtime_events import RuntimeEventPort, RuntimeEventRecoveryPort  # noqa: F401

__all__ = ["RuntimeEventPort", "RuntimeEventRecoveryPort"]
