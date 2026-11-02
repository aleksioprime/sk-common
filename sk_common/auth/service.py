from datetime import datetime, timedelta
from html import escape
from urllib.parse import quote

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sk_common.audit import record
from sk_common.auth.models import AuthSession, MagicLinkToken
from sk_common.people.models import User
from sk_common.people.service import find_or_create_user_by_email, get_user_by_email, normalize_email
from sk_common.queue import Queue
from sk_common.redis import hit_limit
from sk_common.runtime import settings
from sk_common.security import (
    MIN_PASSWORD,
    create_access_token,
    hash_password,
    hash_secret,
    new_secret,
    now_utc,
    verify_password,
)


class AuthError(Exception):
    pass


class RateLimited(AuthError):
    pass


def safe_next_path(value: str | None) -> str:
    """Разрешаем только относительные пути внутри приложения, чтобы ссылка не уводила на чужой сайт."""
    if not value or not value.startswith('/') or value.startswith('//') or '\\' in value:
        return '/'
    return value[:500]


# --- Вход по ссылке из письма ---------------------------------------------------------------


def magic_link_letter(link: str) -> tuple[str, str, str]:
    """Тема, текст и HTML письма со ссылкой входа. Название сервиса и цвет кнопки — из настроек."""
    name, minutes = escape(settings.project_name), settings.auth.magic_link_ttl_minutes
    text = (
        'Здравствуйте!\n\n'
        f'Чтобы войти в {settings.project_name}, откройте ссылку:\n{link}\n\n'
        f'Ссылка действует {minutes} минут и подходит для одного входа.\n'
        'Если вы не запрашивали вход, просто удалите это письмо.'
    )
    html = (
        '<p>Здравствуйте!</p>'
        f'<p><a href="{link}" style="display:inline-block;padding:12px 20px;background:{settings.brand_color};'
        f'color:#fff;border-radius:10px;text-decoration:none;font-weight:600">Войти в {name}</a></p>'
        f'<p>Ссылка действует {minutes} минут и подходит для одного входа.</p>'
        '<p style="color:#666">Если вы не запрашивали вход, просто удалите это письмо.</p>'
    )
    return f'Вход в {settings.project_name}', text, html


async def request_magic_link(
    session: AsyncSession, redis: Redis, queue: Queue, email: str, next_path: str | None = None
) -> None:
    email = normalize_email(email)
    if await hit_limit(redis, f'magic:{email}', settings.auth.magic_link_per_hour, 3600):
        raise RateLimited('Слишком много запросов. Попробуйте через час.')

    token = new_secret()
    session.add(
        MagicLinkToken(
            email=email,
            token_hash=hash_secret(token),
            next_path=safe_next_path(next_path),
            expires_at=now_utc() + timedelta(minutes=settings.auth.magic_link_ttl_minutes),
        )
    )
    await session.flush()

    link = f'{settings.app_url.rstrip("/")}/auth/magic?token={quote(token)}'
    subject, text, html = magic_link_letter(link)
    await queue.enqueue('send_email', email, subject, text, html)


async def issue_personal_login(
    session: AsyncSession, user: User, next_path: str, purpose: str, ttl_minutes: int = 20
) -> tuple[str, datetime]:
    """Одноразовый вход, привязанный к учётной записи, а не к email (например, QR на распечатанном листе).
    purpose становится способом входа сессии."""
    token = new_secret()
    expires_at = now_utc() + timedelta(minutes=ttl_minutes)
    session.add(
        MagicLinkToken(
            email=user.email,
            purpose=purpose,
            user_id=user.id,
            token_hash=hash_secret(token),
            next_path=safe_next_path(next_path),
            expires_at=expires_at,
        )
    )
    await session.flush()
    return token, expires_at


async def consume_magic_link(session: AsyncSession, token: str) -> tuple[User, str, str]:
    record_ = await session.scalar(
        select(MagicLinkToken).where(MagicLinkToken.token_hash == hash_secret(token)).with_for_update()
    )
    if record_ is None or record_.used_at is not None or record_.expires_at < now_utc():
        raise AuthError('Ссылка недействительна или уже использована. Запросите новую.')
    record_.used_at = now_utc()
    if record_.user_id:
        user = await session.get(User, record_.user_id)
        if user is None:
            raise AuthError('Учётная запись не найдена. Обратитесь к организаторам.')
    else:
        user = await find_or_create_user_by_email(session, record_.email)
    return user, record_.next_path or '/', record_.purpose


async def login_with_password(session: AsyncSession, redis: Redis, email: str, password: str, ip: str | None) -> User:
    """Вход по email и паролю. Лимит попыток на адрес и на IP защищает от перебора."""
    email = normalize_email(email)
    if await hit_limit(redis, f'pwd:{email}', 10, 900) or (ip and await hit_limit(redis, f'pwd-ip:{ip}', 50, 900)):
        raise RateLimited('Слишком много попыток входа. Попробуйте через 15 минут.')
    user = await get_user_by_email(session, email)
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError('Неверный email или пароль')
    return user


def set_password(user: User, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f'Пароль должен быть не короче {MIN_PASSWORD} символов')
    user.password_hash = hash_password(password)


# --- Сессии ---------------------------------------------------------------------------------


async def open_session(
    session: AsyncSession, user: User, method: str, user_agent: str | None, ip: str | None
) -> tuple[str, str]:
    if reason := user.block_reason():
        raise AuthError(reason)
    refresh = new_secret()
    auth_session = AuthSession(
        user_id=user.id,
        refresh_hash=hash_secret(refresh),
        method=method,
        user_agent=(user_agent or '')[:500] or None,
        ip=ip,
        expires_at=now_utc() + timedelta(days=settings.auth.refresh_ttl_days),
    )
    session.add(auth_session)
    user.last_login_at = now_utc()
    await session.flush()
    record(session, 'auth.login', 'user', user.id, user.id, method=method)
    return create_access_token(user.id, auth_session.id), refresh


async def refresh_session(session: AsyncSession, refresh: str) -> tuple[str, str]:
    auth_session = await session.scalar(
        select(AuthSession).where(AuthSession.refresh_hash == hash_secret(refresh)).with_for_update()
    )
    if auth_session is None or auth_session.revoked_at is not None or auth_session.expires_at < now_utc():
        raise AuthError('Сессия завершена. Войдите снова.')
    user = await session.get(User, auth_session.user_id)
    if user is None or user.block_reason():
        raise AuthError(user.block_reason() if user else 'Учётная запись отключена.')
    new_refresh = new_secret()
    auth_session.refresh_hash = hash_secret(new_refresh)
    auth_session.last_used_at = now_utc()
    return create_access_token(user.id, auth_session.id), new_refresh


async def close_session(session: AsyncSession, refresh: str | None) -> None:
    if not refresh:
        return
    await session.execute(
        update(AuthSession)
        .where(AuthSession.refresh_hash == hash_secret(refresh), AuthSession.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )
