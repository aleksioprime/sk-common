"""Клиент SkolStream: OAuth2-вход, профиль пользователя и выгрузка справочников.

Вход: /o/authorize/ → /o/token/ (SkolStream выдаёт JWT) → /api/v1/user/me.
Справочники: /api/v1/integrations/directory/ со служебным токеном (контракт — в README пакета).
"""

from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

import httpx

from sk_common.runtime import settings


class SkolstreamError(Exception):
    pass


@dataclass
class SkolstreamProfile:
    id: str
    email: str
    last_name: str = ''
    first_name: str = ''
    middle_name: str = ''
    gender: str | None = None
    is_student: bool = False
    is_employee: bool = False
    class_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_me(cls, data: dict) -> 'SkolstreamProfile':
        return cls(
            id=str(data['id']),
            email=(data.get('email') or '').strip().lower(),
            last_name=data.get('last_name') or '',
            first_name=data.get('first_name') or '',
            middle_name=data.get('middle_name') or '',
            gender=data.get('gender') or None,
            is_student=bool(data.get('is_student')),
            is_employee=bool(data.get('is_employee') or data.get('is_admin')),
            class_ids=[str(group['id']) for group in data.get('student_groups') or [] if group.get('id')],
        )


class SkolstreamClient(Protocol):
    def authorize_url(self, redirect_uri: str, state: str, code_challenge: str) -> str: ...

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> str: ...

    async def fetch_profile(self, access_token: str) -> SkolstreamProfile: ...

    async def fetch_directory(self, updated_since: str | None = None) -> dict: ...


class HttpSkolstreamClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, directory_token: str, timeout: float):
        self.base_url = base_url.rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self.directory_token = directory_token
        self.timeout = timeout

    def authorize_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                'response_type': 'code',
                'client_id': self.client_id,
                'redirect_uri': redirect_uri,
                'scope': 'read',
                'state': state,
                'code_challenge': code_challenge,
                'code_challenge_method': 'S256',
            }
        )
        return f'{self.base_url}/o/authorize/?{query}'

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> str:
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code_verifier': code_verifier,
        }
        response = await self._request('POST', '/o/token/', data=data)
        token = response.json().get('access_token')
        if not token:
            raise SkolstreamError('SkolStream не выдал токен')
        return token

    async def fetch_profile(self, access_token: str) -> SkolstreamProfile:
        response = await self._request('GET', '/api/v1/user/me', headers={'Authorization': f'Bearer {access_token}'})
        return SkolstreamProfile.from_me(response.json())

    async def fetch_directory(self, updated_since: str | None = None) -> dict:
        if not self.directory_token:
            raise SkolstreamError('Не задан токен выгрузки справочников SkolStream')
        params = {'updated_since': updated_since} if updated_since else None
        response = await self._request(
            'GET',
            '/api/v1/integrations/directory/',
            params=params,
            headers={'Authorization': f'Token {self.directory_token}'},
        )
        return response.json()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout, headers={'User-Agent': settings.user_agent}
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SkolstreamError(f'SkolStream недоступен: {exc}') from exc
        if response.status_code >= 400:
            raise SkolstreamError(f'SkolStream ответил {response.status_code} на {path}')
        return response


def get_skolstream_client() -> SkolstreamClient:
    config = settings.skolstream
    return HttpSkolstreamClient(
        config.base_url, config.client_id, config.client_secret, config.directory_token, config.timeout
    )
