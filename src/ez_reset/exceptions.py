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


class D4ProtocolError(ProtocolError):
    """Raised when an IEEE 1284.4 D4 protocol packet violation or timeout occurs."""
    pass


class BackupError(Exception):
    """Raised when EEPROM backup or dump fails (e.g. read error on any cell)."""
    pass


class RestoreValidationError(Exception):
    """Raised when an EEPROM restore file fails safety or integrity validation."""
    pass

