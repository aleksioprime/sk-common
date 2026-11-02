import pytest
from httpx import AsyncClient

from sk_common.security import hash_password, verify_password
from sk_common.testing import login_as

pytestmark = pytest.mark.usefixtures('redis_clean')


def _other(client: AsyncClient) -> AsyncClient:
    return AsyncClient(transport=client._transport, base_url=str(client.base_url), headers=client.headers)


def test_password_hash_roundtrip():
    stored = hash_password('секретный-пароль')
    assert stored.startswith('scrypt$') and verify_password('секретный-пароль', stored)
    assert not verify_password('другой', stored) and not verify_password('x', None)


async def test_admin_creates_user_and_user_logs_in(db, client):
    await login_as(db, client, email='root@example.org', is_superuser=True)
    created = await client.post(
        '/api/v1/people/users',
        json={'email': 'Org@Example.org', 'full_name': 'Петров Иван', 'password': 'pass-1234', 'is_employee': True},
    )
    assert created.status_code == 201, created.text
    assert created.json()['has_password'] is True and created.json()['person']['is_employee'] is True

    async with _other(client) as guest:
        bad = await guest.post('/api/v1/auth/login', json={'email': 'org@example.org', 'password': 'wrong-pass'})
        assert bad.status_code == 400
        ok = await guest.post(
            '/api/v1/auth/login', json={'email': 'org@example.org', 'password': 'pass-1234', 'next': '/surveys'}
        )
        assert ok.status_code == 200 and ok.json()['next'] == '/surveys'
        me = (await guest.get('/api/v1/auth/me')).json()
        assert me['has_password'] is True and me['user']['person']['full_name'] == 'Петров Иван'
        # смена своего пароля требует текущий
        assert (await guest.post('/api/v1/auth/password', json={'new_password': 'new-pass-99'})).status_code == 400
        changed = await guest.post(
            '/api/v1/auth/password', json={'current_password': 'pass-1234', 'new_password': 'new-pass-99'}
        )
        assert changed.status_code == 204

    # администратор сбрасывает пароль
    user_id = created.json()['id']
    assert (await client.patch(f'/api/v1/people/users/{user_id}', json={'password': 'reset-pass-1'})).status_code == 200


async def test_password_login_rate_limit(db, client):
    for _ in range(10):
        await client.post('/api/v1/auth/login', json={'email': 'nobody@example.org', 'password': 'x'})
    blocked = await client.post('/api/v1/auth/login', json={'email': 'nobody@example.org', 'password': 'x'})
    assert blocked.status_code == 429
