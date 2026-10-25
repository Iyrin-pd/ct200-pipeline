class ServiceError(Exception):
    status = 400
    code = "error"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message, self.extra = message, extra


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Unprocessable(ServiceError):
    status, code = 422, "unprocessable"


class LLMFailure(ServiceError):
    status, code = 502, "llm_failure"