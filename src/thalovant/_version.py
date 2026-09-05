"""Single source of truth for the package version and derived user agents.

This module intentionally imports nothing from the rest of the package so that
low-level modules -- including the ones ``thalovant/__init__.py`` imports at
import time -- can read the version without a circular import.

Every user-agent string the SDK sends is built here. Never hard-code a version
inside a user-agent literal elsewhere; ``tests/test_version.py`` enforces it.
"""

from __future__ import annotations

__version__ = "0.4.39"

#: Product token shared by every Thalovant Python SDK user agent.
USER_AGENT_PRODUCT = "ThalovantPythonSDK"

#: User agent sent by both the data-plane and control-plane surfaces.
USER_AGENT = f"{USER_AGENT_PRODUCT}/{__version__}"

__all__ = ["USER_AGENT", "USER_AGENT_PRODUCT", "__version__"]
