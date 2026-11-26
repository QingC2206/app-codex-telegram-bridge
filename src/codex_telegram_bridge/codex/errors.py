class DesktopIpcError(RuntimeError):
    pass


class DesktopIpcUnavailable(DesktopIpcError):
    """The route failed before a mutating request could be submitted."""


class DesktopOwnerDiscoveryUnavailable(DesktopIpcUnavailable):
    """An initialized broker could not answer owner discovery; no input was sent."""


class DesktopFollowerMissing(DesktopIpcUnavailable):
    """Windows reported the verified follower pipe missing before any frame."""


class DesktopIpcInactive(DesktopIpcError):
    pass


class DesktopBridgeSafetyError(DesktopIpcError):
    """The Desktop instance or profile cannot be identified safely."""


class DesktopSubmissionUnknown(DesktopIpcError):
    """A mutating request may have been accepted; never fall back or retry."""
