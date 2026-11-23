"""Browser-independent errors shared by the HTTP server and client."""


class ServiceError(RuntimeError):
    def __init__(self, code: str, detail: str, status: int = 500):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail}


class UCServiceGone(ServiceError):
    def __init__(self, detail: str):
        super().__init__("service_gone", detail, 503)
