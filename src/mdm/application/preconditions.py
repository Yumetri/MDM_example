"""Application failures for conditional resource mutations."""


class PreconditionRequired(RuntimeError):
    """A required conditional request token was omitted."""


class InvalidIfMatch(RuntimeError):
    """An If-Match header does not contain one canonical strong tag."""


class PreconditionFailed(RuntimeError):
    """The supplied conditional request token is stale."""
