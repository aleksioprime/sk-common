"""Вход: ссылка из письма, пароль, SkolStream; продление и завершение сессии.

Сервис подключает create_auth_router() под /api. Ответ /me сервис может расширить своей схемой
(наследник Me) и функцией me_extra, которая возвращает дополнительные поля.
"""

import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sk_common.audit import record
from sk_common.auth import service
from sk_common.auth.models import AuthSession
from sk_common.auth.schemas import (
    LoginResult,
    MagicLinkRequest,
    MagicLinkVerify,
    Me,
    PasswordChange,
    PasswordLogin,
    Providers,
)
from sk_common.captcha import Captcha, get_captcha
from sk_common.deps import CurrentUser, Db
from sk_common.people.models import User
from sk_common.people.schemas import UserOut
from sk_common.people.service import ensure_user, upsert_person_from_profile
from sk_common.queue import Queue, get_queue
from sk_common.redis import get_redis, hit_limit
from sk_common.runtime import settings
from sk_common.security import TokenError, decode_access_token, new_secret, read_state, sign_state, verify_password
from sk_common.skolstream import SkolstreamClient, SkolstreamError, get_skolstream_client

logger = logging.getLogger(__name__)

OAUTH_COOKIE = 'sk_oauth'
AUTH_PREFIX = '/api/v1/auth'
SKOLSTREAM_PATH = f'{AUTH_PREFIX}/skolstream'

MeExtra = Callable[[AsyncSession, Request, User], Awaitable[dict[str, Any]]]


def set_session_cookies(response: Response, access: str, refresh: str) -> None:
    common = {'httponly': True, 'secure': settings.cookie_secure, 'samesite': 'lax'}
    response.set_cookie(
        settings.auth.access_cookie, access, max_age=settings.auth.access_ttl_minutes * 60, path='/', **common
    )
    response.set_cookie(
        settings.auth.refresh_cookie,
        refresh,
        max_age=settings.auth.refresh_ttl_days * 86400,
        path=AUTH_PREFIX,
        **common,
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(settings.auth.access_cookie, path='/')
    response.delete_cookie(settings.auth.refresh_cookie, path=AUTH_PREFIX)


def client_info(request: Request) -> tuple[str | None, str | None]:
    forwarded = request.headers.get('x-forwarded-for', '')
    ip = forwarded.split(',')[0].strip() or (request.client.host if request.client else None)
    return request.headers.get('user-agent'), ip


async def session_method(db: AsyncSession, request: Request) -> str | None:
    """Способ входа текущей сессии: magic_link, password, skolstream или purpose личного входа."""
    token = request.cookies.get(settings.auth.access_cookie) or ''
    try:
        session_id = int(decode_access_token(token)['sid'])
    except (TokenError, KeyError, ValueError):
        return None
    return await db.scalar(select(AuthSession.method).where(AuthSession.id == session_id))


def create_auth_router(me_model: type[Me] = Me, me_extra: MeExtra | None = None) -> APIRouter:
    router = APIRouter(prefix='/v1/auth', tags=['auth'])

    @router.get('/providers', response_model=Providers, summary='Доступные способы входа')
    async def providers() -> Providers:
        return Providers(
            skolstream=settings.skolstream.enabled,
            captcha_site_key=settings.captcha.client_key if settings.captcha.enabled else None,
        )

    @router.post('/magic-link', status_code=status.HTTP_202_ACCEPTED, summary='Отправить ссылку для входа на email')
    async def request_magic_link(
        payload: MagicLinkRequest,
        request: Request,
        db: Db,
        queue: Annotated[Queue, Depends(get_queue)],
        captcha: Annotated[Captcha, Depends(get_captcha)],
    ) -> dict:
        _, ip = client_info(request)
        if not await captcha.verify(payload.captcha_token, ip):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Подтвердите, что вы не робот')
        if ip and await hit_limit(get_redis(), f'magic-ip:{ip}', settings.auth.magic_link_per_ip_hour, 3600):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, 'Слишком много запросов. Попробуйте через час.')
        try:
            await service.request_magic_link(db, get_redis(), queue, payload.email, payload.next)
        except service.RateLimited as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        await db.commit()
        return {'detail': 'Письмо со ссылкой для входа отправлено'}

    @router.post('/magic-link/verify', response_model=LoginResult, summary='Войти по ссылке из письма')
    async def verify_magic_link(payload: MagicLinkVerify, request: Request, response: Response, db: Db) -> LoginResult:
        try:
            user, next_path, purpose = await service.consume_magic_link(db, payload.token)
            method = 'magic_link' if purpose == 'email' else purpose
            access, refresh = await service.open_session(db, user, method, *client_info(request))
        except service.AuthError as exc:
            await db.commit()  # фиксируем погашение ссылки даже при отказе во входе
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await db.commit()
        await db.refresh(user)
        set_session_cookies(response, access, refresh)
        return LoginResult(user=UserOut.model_validate(user), next=next_path)

    @router.post('/login', response_model=LoginResult, summary='Войти по email и паролю')
    async def password_login(payload: PasswordLogin, request: Request, response: Response, db: Db) -> LoginResult:
        _, ip = client_info(request)
        try:
            user = await service.login_with_password(db, get_redis(), payload.email, payload.password, ip)
            access, refresh = await service.open_session(db, user, 'password', *client_info(request))
        except service.RateLimited as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        except service.AuthError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await db.commit()
        await db.refresh(user)
        set_session_cookies(response, access, refresh)
        return LoginResult(user=UserOut.model_validate(user), next=service.safe_next_path(payload.next))

    @router.post('/password', status_code=status.HTTP_204_NO_CONTENT, summary='Задать или сменить свой пароль')
    async def change_password(payload: PasswordChange, db: Db, user: CurrentUser) -> Response:
        if user.password_hash and not verify_password(payload.current_password or '', user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Текущий пароль указан неверно')
        try:
            service.set_password(user, payload.new_password)
        except service.AuthError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        record(db, 'auth.password', 'user', user.id, user.id)
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get('/skolstream/login', summary='Перейти ко входу через SkolStream')
    async def skolstream_login(
        client: Annotated[SkolstreamClient, Depends(get_skolstream_client)], next: str | None = None
    ) -> RedirectResponse:
        if not settings.skolstream.enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'Вход через SkolStream не настроен')
        state, verifier = new_secret(), new_secret()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        response = RedirectResponse(client.authorize_url(state, challenge), status_code=302)
        response.set_cookie(
            OAUTH_COOKIE,
            sign_state({'state': state, 'verifier': verifier, 'next': service.safe_next_path(next)}),
            max_age=600,
            httponly=True,
            secure=settings.cookie_secure,
            samesite='lax',
            path=SKOLSTREAM_PATH,
        )
        return response

    @router.get('/skolstream/callback', summary='Возврат из SkolStream')
    async def skolstream_callback(
        request: Request,
        db: Db,
        client: Annotated[SkolstreamClient, Depends(get_skolstream_client)],
        code: str | None = None,
        state: str | None = None,
    ) -> RedirectResponse:
        app_url = settings.app_url.rstrip('/')
        try:
            saved = read_state(request.cookies.get(OAUTH_COOKIE, ''))
            if not code or not state or saved.get('state') != state:
                raise service.AuthError('Сессия входа устарела')
            token = await client.exchange_code(code, saved['verifier'])
            profile = await client.fetch_profile(token)
            if not profile.email:
                raise service.AuthError('В профиле SkolStream нет email')
            person = await upsert_person_from_profile(db, profile)
            user = await ensure_user(db, person, profile.email)
            access, refresh = await service.open_session(db, user, 'skolstream', *client_info(request))
            await db.commit()
        except (TokenError, SkolstreamError, service.AuthError) as exc:
            logger.warning('Вход через SkolStream не удался: %s', exc)
            response = RedirectResponse(f'{app_url}/login?error={quote(str(exc))}', status_code=302)
            response.delete_cookie(OAUTH_COOKIE, path=SKOLSTREAM_PATH)
            return response

        response = RedirectResponse(f'{app_url}{saved["next"]}', status_code=302)
        response.delete_cookie(OAUTH_COOKIE, path=SKOLSTREAM_PATH)
        set_session_cookies(response, access, refresh)
        return response

    @router.post('/refresh', status_code=status.HTTP_204_NO_CONTENT, summary='Продлить сессию')
    async def refresh(request: Request, response: Response, db: Db) -> Response:
        token = request.cookies.get(settings.auth.refresh_cookie)
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Требуется вход')
        try:
            access, new_refresh = await service.refresh_session(db, token)
        except service.AuthError as exc:
            denied = JSONResponse({'detail': str(exc)}, status_code=status.HTTP_401_UNAUTHORIZED)
            clear_session_cookies(denied)
            return denied
        await db.commit()
        set_session_cookies(response, access, new_refresh)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.post('/logout', status_code=status.HTTP_204_NO_CONTENT, summary='Выйти')
    async def logout(request: Request, response: Response, db: Db) -> Response:
        await service.close_session(db, request.cookies.get(settings.auth.refresh_cookie))
        await db.commit()
        clear_session_cookies(response)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get('/me', response_model=me_model, summary='Текущий пользователь')
    async def me(request: Request, db: Db, user: CurrentUser) -> Me:
        extra = await me_extra(db, request, user) if me_extra else {}
        return me_model(user=UserOut.model_validate(user), has_password=bool(user.password_hash), **extra)

    return router
