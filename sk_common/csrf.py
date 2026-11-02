from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from sk_common.runtime import settings

SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Защита cookie-сессий от CSRF: изменяющие запросы из браузера принимаются только с разрешённых адресов.

    Браузер всегда передаёт Origin в POST/PUT/PATCH/DELETE; запросы без Origin (curl, серверы) пропускаются,
    у них нет cookie пользователя.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get('origin')
        if request.method not in SAFE_METHODS and origin and origin.rstrip('/') not in settings.allowed_origins:
            return JSONResponse({'detail': 'Запрос с недопустимого адреса'}, status_code=403)
        return await call_next(request)
