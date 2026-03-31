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
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)
