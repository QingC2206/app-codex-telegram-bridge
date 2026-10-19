class DesktopIpcError(RuntimeError):
    pass


class DesktopIpcUnavailable(DesktopIpcError):
    """The route failed before a mutating request could be submitted."""


class DesktopIpcInactive(DesktopIpcError):
    pass


class DesktopBridgeSafetyError(DesktopIpcError):
    """The Desktop instance or profile cannot be identified safely."""


class DesktopSubmissionUnknown(DesktopIpcError):
    """A mutating request may have been accepted; never fall back or retry."""
