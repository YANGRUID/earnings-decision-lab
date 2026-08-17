import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from services.llm.errors import LLMError
from services.research_orchestration import UnsupportedSymbolError

log = logging.getLogger("api.errors")


class NotFoundError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class RateLimitedError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def _error_body(request: Request, message: str) -> dict:
    return {"error": message, "request_id": getattr(request.state, "request_id", None)}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content=_error_body(request, exc.message))

    @app.exception_handler(RateLimitedError)
    async def rate_limited_handler(request: Request, exc: RateLimitedError):
        return JSONResponse(status_code=429, content=_error_body(request, exc.message))

    @app.exception_handler(UnsupportedSymbolError)
    async def unsupported_symbol_handler(request: Request, exc: UnsupportedSymbolError):
        return JSONResponse(status_code=422, content=_error_body(request, exc.reason))

    @app.exception_handler(LLMError)
    async def llm_error_handler(request: Request, exc: LLMError):
        log.warning("LLM error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse(
            status_code=503, content=_error_body(request, f"AI service unavailable: {exc}")
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content=_error_body(request, str(exc)))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        log.exception("unhandled exception", extra={"request_id": request_id})
        return JSONResponse(status_code=500, content=_error_body(request, "internal server error"))
