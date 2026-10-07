class BackendError(Exception):
    """General backend communication error."""
    pass


class VerificationError(Exception):
    """Raised when written EEPROM verification fails (read back value != written value)."""
    pass


class DeviceError(Exception):
    """Raised when printer device identification or definition fails."""
    pass


class ProtocolError(Exception):
    """Raised when an unexpected or malformed response is received."""
    pass
