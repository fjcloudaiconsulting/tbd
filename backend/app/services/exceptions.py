"""Domain exceptions for the service layer.

These are raised by services and mapped to HTTP responses by routers.
They carry no HTTP semantics — just domain meaning.
"""


class NotFoundError(Exception):
    def __init__(self, resource: str):
        self.resource = resource
        super().__init__(f"{resource} not found")


class ValidationError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class ConflictError(Exception):
    def __init__(self, detail: str, *, code: str | None = None):
        self.detail = detail
        # Optional machine-readable hint for routers that want to surface
        # ``{"code": ..., "message": ...}`` to clients. Existing call sites
        # that pass only a message continue to work; new precondition-style
        # services (e.g. admin_users_service.delete_user) populate ``code``
        # so the frontend can branch on the failure reason without parsing
        # English.
        self.code = code
        super().__init__(detail)


class MissingCategoryTypeError(Exception):
    """Raised by import preflight when the org has no category of a type
    that the parsed rows require.

    Carries the sorted list of missing types ("income", "expense") so the
    router can render a structured 400 the frontend can read mechanically.
    Category Fallback design Layer B (post-L3.10).
    """

    def __init__(self, missing_types: list[str], message: str):
        self.missing_types = missing_types
        self.message = message
        super().__init__(message)
