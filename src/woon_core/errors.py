"""User-facing error types."""


class WoonError(RuntimeError):
    """Raised when a Woon operation cannot be completed safely."""
