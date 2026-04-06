from __future__ import annotations


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "backend_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
