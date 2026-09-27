import logging
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pika.exceptions import AMQPError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from helpflow.api import router
from helpflow.config import get_settings

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("helpflow.http")
logging.getLogger("pika").setLevel(logging.WARNING)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    summary="Support ticket workflow with RabbitMQ and Telegram integration",
)
app.include_router(router)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = str(uuid4())
    started_at = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        getattr(request.scope.get("route"), "path", "unmatched"),
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": settings.app_name, "docs": "/docs", "health": "/health"}


@app.exception_handler(OperationalError)
@app.exception_handler(PoolTimeout)
@app.exception_handler(AMQPError)
async def dependency_unavailable(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("dependency_unavailable kind=%s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "Зависимый сервис временно недоступен"},
        headers={"Retry-After": "1"},
    )
