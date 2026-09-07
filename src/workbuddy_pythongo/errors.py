class BridgeError(Exception):
    """Structured error safe to expose through the local RPC boundary."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


class ValidationError(BridgeError):
    def __init__(self, message, details=None):
        super().__init__("INVALID_REQUEST", message, details)

