"""Custom exceptions for the GeoGuessr Research framework."""


class GeoGuessrError(Exception):
    """Base exception for all GeoGuessr framework errors."""
    pass


# ─────────────────────────────────────────────────────────────
# Client / Host Errors
# ─────────────────────────────────────────────────────────────
   
class HostError(GeoGuessrError):
    """Base exception for Street View host errors"""
    pass

class HostNotStartedError(HostError):
    """Raised when trying to use host before it's started."""
    pass

class HostTimeoutError(HostError):
    """Raised when host request times out."""

    def __init__(self, method: str, timeout: float, req_id: int = None):
        self.method = method
        self.timeout = timeout
        self.req_id = req_id
        msg = f"Host request '{method}' timed out after {timeout}s"
        if req_id:
            msg += f" (req_id={req_id})"
        super().__init__(msg)

class HostResponseError(HostError):
    """Raised when host returns an error response."""

    def __init__(self, error: str, method: str = None):
        self.error = error
        self.method = method
        msg = f"Host error: {error}"
        if method:
            msg = f"Host error on '{method}': {error}"
        super().__init__(msg)

# ─────────────────────────────────────────────────────────────
# Navigation Errors
# ─────────────────────────────────────────────────────────────
class NavigationError(GeoGuessrError):
    """Base exception for navigation errors."""
    pass

class InvalidStateError(NavigationError):
    """Raised when state JSON is invalid or missing required keys."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Invalid state: {reason}")

class InvalidCommandError(NavigationError):
    """Raised when command is invalid or missing required keys."""
    
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Invalid command: {reason}")


class DirectionNotAvailableError(NavigationError):
    """Raised when attempting to move in an unavailable direction."""
    
    def __init__(self, direction: str, available: list = None):
        self.direction = direction
        self.available = available or []
        msg = f"Direction '{direction}' not available"
        if available:
            msg += f". Available: {', '.join(available)}"
        super().__init__(msg)

# ─────────────────────────────────────────────────────────────
# Tool Errors
# ─────────────────────────────────────────────────────────────
class ToolError(GeoGuessrError):
    """Base exception for tool execution errors."""
    pass

class MissingContextError(ToolError):
    """Raised when required context is missing."""

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"Missing required context: {key}")

class InvalidArgumentError(ToolError):
    """Raised when tool receives invalid arguments."""

    def __init__(self, arg: str, reason: str = None):
        self.arg = arg
        self.reason = reason
        msg = f"Invalid argument: {arg}"
        if reason:
            msg += f"- {reason}"
        super().__init__(msg)

