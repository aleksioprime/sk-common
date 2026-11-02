"""Журнал запросов и общие middleware приложения FastAPI."""

import logging
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from sk_common.csrf import OriginCheckMiddleware
from sk_common.runtime import settings

logger = logging.getLogger('sk_common.requests')


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        if request.url.path not in ('/api/health', '/api/health/ready'):
            logger.info('%s %s → %s (%.0f ms)', request.method, request.url.path, response.status_code, elapsed)
        return response


def add_common_middleware(app: FastAPI) -> None:
    """Проверка Origin (CSRF), журнал запросов и CORS для адресов из настроек."""
    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
