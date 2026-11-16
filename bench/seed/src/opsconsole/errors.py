class OpsError(Exception):
    """Base class of every error this package raises on purpose."""


class NotAuthorized(OpsError):
    pass


class NotFound(OpsError):
    pass
