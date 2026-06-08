"""SDK exception hierarchy."""


class ThalovantError(Exception):
    """Base exception for Thalovant SDK failures."""


class ThalovantIdentityError(ThalovantError):
    """Raised when identity material is missing or invalid."""


class ThalovantConnectionError(ThalovantError):
    """Raised when the HiveMind HTTP connection cannot be established."""


class ThalovantTimeoutError(ThalovantError):
    """Raised when a hub does not answer before the configured timeout."""


class ThalovantRuntimeError(ThalovantError):
    """Raised when the hub reports that a request could not be handled."""


class ThalovantAPIError(ThalovantError):
    """Raised when the Thalovant control-plane API request fails."""


class ThalovantUnsupportedProtocolError(ThalovantError):
    """Raised when a requested data-plane protocol is not supported locally."""
