"""То, что сервис настраивает под себя: поля /me, личный одноразовый вход, письмо, имя клиента SkolStream."""

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from sk_common.auth.router import create_auth_router, session_method
from sk_common.auth.schemas import Me
from sk_common.auth.service import issue_personal_login, magic_link_letter
from sk_common.testing import login_as
from tests.conftest import ORIGIN, settings


class ServiceMe(Me):
    session_method: str | None = None


async def _extra(db, request, user) -> dict:
    return {'session_method': await session_method(db, request)}


def _app() -> FastAPI:
    app = FastAPI()
    api = APIRouter(prefix='/api')
    api.include_router(create_auth_router(me_model=ServiceMe, me_extra=_extra))
    app.include_router(api)
    return app


async def test_me_extra_fields_and_personal_login(db):
    user = await login_as(db, AsyncClient(), email='judge@example.org')
    token, _ = await issue_personal_login(db, user, '/sheets/1', purpose='judge_qr')
    await db.commit()

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url=ORIGIN, headers={'Origin': ORIGIN}) as http:
        verify = await http.post('/api/v1/auth/magic-link/verify', json={'token': token})
        assert verify.status_code == 200 and verify.json()['next'] == '/sheets/1'
        me = (await http.get('/api/v1/auth/me')).json()
        assert me['session_method'] == 'judge_qr'
        assert me['user']['email'] == 'judge@example.org'


def test_magic_link_letter_uses_service_name():
    subject, text, html = magic_link_letter('https://example.org/auth/magic?token=x')
    assert subject == 'Вход в SK Test'
    assert 'войти в SK Test' in text and settings.brand_color in html


def test_user_agent_names_the_service():
    assert settings.user_agent.startswith('SK-Test/')
