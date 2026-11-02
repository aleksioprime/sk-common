import re
import uuid

import pytest
from sqlalchemy import select

from sk_common.people.models import Person, StudyClass, User
from sk_common.testing import CLASS_ID, STUDENT_ID
from tests.conftest import settings

pytestmark = pytest.mark.usefixtures('redis_clean')


def _token_from_mail(queue) -> str:
    function, args, _ = queue.jobs[-1]
    assert function == 'send_email'
    return re.search(r'token=([\w%-]+)', args[2]).group(1)


async def _magic_login(client, queue, email='guest@example.org', next_path='/events'):
    response = await client.post('/api/v1/auth/magic-link', json={'email': email, 'next': next_path})
    assert response.status_code == 202
    return await client.post('/api/v1/auth/magic-link/verify', json={'token': _token_from_mail(queue)})


async def test_magic_link_creates_external_participant(db, client, queue):
    response = await _magic_login(client, queue, email='Guest@Example.org')
    assert response.status_code == 200
    body = response.json()
    assert body['next'] == '/events'
    assert body['user']['email'] == 'guest@example.org'
    assert body['user']['person']['is_external'] is True
    assert settings.auth.access_cookie in response.cookies

    me = await client.get('/api/v1/auth/me')
    assert me.status_code == 200
    assert me.json()['user']['email'] == 'guest@example.org'


async def test_magic_link_is_single_use(db, client, queue):
    await client.post('/api/v1/auth/magic-link', json={'email': 'guest@example.org'})
    token = _token_from_mail(queue)
    assert (await client.post('/api/v1/auth/magic-link/verify', json={'token': token})).status_code == 200
    repeat = await client.post('/api/v1/auth/magic-link/verify', json={'token': token})
    assert repeat.status_code == 400


async def test_magic_link_rejects_external_redirect(db, client, queue):
    response = await _magic_login(client, queue, next_path='//evil.example/phish')
    assert response.json()['next'] == '/'


async def test_magic_link_links_existing_skolstream_person(db, client, queue):
    person = Person(
        email='teacher@skolkovo.example.org',
        last_name='Петров',
        source='skolstream',
        is_employee=True,
        skolstream_id=uuid.uuid4(),
    )
    db.add(person)
    await db.commit()
    response = await _magic_login(client, queue, email='teacher@skolkovo.example.org')
    assert response.json()['user']['person']['id'] == person.id


async def test_magic_link_rate_limit(db, client):
    for _ in range(settings.auth.magic_link_per_hour):
        assert (await client.post('/api/v1/auth/magic-link', json={'email': 'a@b.ru'})).status_code == 202
    assert (await client.post('/api/v1/auth/magic-link', json={'email': 'a@b.ru'})).status_code == 429


async def test_refresh_rotates_and_logout_revokes(db, client, queue):
    await _magic_login(client, queue)
    old_refresh = client.cookies.get(settings.auth.refresh_cookie)

    refreshed = await client.post('/api/v1/auth/refresh')
    assert refreshed.status_code == 204
    assert client.cookies.get(settings.auth.refresh_cookie) != old_refresh

    client.cookies.set(settings.auth.refresh_cookie, old_refresh, path='/api/v1/auth')
    stale = await client.post('/api/v1/auth/refresh')
    assert stale.status_code == 401

    await _magic_login(client, queue)
    assert (await client.post('/api/v1/auth/logout')).status_code == 204
    assert (await client.get('/api/v1/auth/me')).status_code == 401


async def test_foreign_origin_is_rejected(db, client):
    response = await client.post(
        '/api/v1/auth/magic-link', json={'email': 'a@b.ru'}, headers={'Origin': 'https://evil.example'}
    )
    assert response.status_code == 403


async def test_bootstrap_admin(db, client, queue, monkeypatch):
    monkeypatch.setattr(settings, 'bootstrap_admin_emails', ['boss@skolkovo.example.org'])
    response = await _magic_login(client, queue, email='boss@skolkovo.example.org')
    assert response.json()['user']['is_superuser'] is True


async def test_skolstream_login_and_callback(db, client, skolstream, monkeypatch):
    monkeypatch.setattr(settings.skolstream, 'base_url', 'https://skolstream.test')
    monkeypatch.setattr(settings.skolstream, 'client_id', 'sk-events')
    db.add(StudyClass(skolstream_id=uuid.UUID(CLASS_ID), name='9А', grade=9, letter='А'))
    await db.commit()

    start = await client.get('/api/v1/auth/skolstream/login', params={'next': '/my'})
    assert start.status_code == 302
    state = re.search(r'state=([\w-]+)', start.headers['location']).group(1)

    callback = await client.get('/api/v1/auth/skolstream/callback', params={'code': 'abc', 'state': state})
    assert callback.status_code == 302
    assert callback.headers['location'].endswith('/my')
    assert skolstream.exchanged[0][0] == 'abc'

    person = await db.scalar(select(Person).where(Person.skolstream_id == uuid.UUID(STUDENT_ID)))
    assert person.full_name == 'Иванова Анна Сергеевна'
    assert person.study_class.name == '9А'
    user = await db.scalar(select(User).where(User.person_id == person.id))
    assert user.email == 'student@skolkovo.example.org'


async def test_skolstream_callback_with_wrong_state(db, client, monkeypatch):
    monkeypatch.setattr(settings.skolstream, 'base_url', 'https://skolstream.test')
    monkeypatch.setattr(settings.skolstream, 'client_id', 'sk-events')
    await client.get('/api/v1/auth/skolstream/login')
    response = await client.get('/api/v1/auth/skolstream/callback', params={'code': 'abc', 'state': 'forged'})
    assert response.status_code == 302
    assert '/login?error=' in response.headers['location']


async def test_skolstream_disabled_by_default(db, client):
    providers = await client.get('/api/v1/auth/providers')
    assert providers.json() == {'magic_link': True, 'skolstream': False, 'captcha_site_key': None}
    assert (await client.get('/api/v1/auth/skolstream/login')).status_code == 404


async def test_person_deactivated_in_skolstream_cannot_log_in(db, client, queue):
    await _magic_login(client, queue, email='student@skolkovo.example.org')
    user = await db.scalar(select(User).where(User.email == 'student@skolkovo.example.org'))
    person = Person(
        source='skolstream', skolstream_id=uuid.UUID(STUDENT_ID), email=user.email, last_name='Иванова', is_active=False
    )
    db.add(person)
    await db.flush()
    user.person_id = person.id
    await db.commit()

    assert (await client.get('/api/v1/auth/me')).status_code == 401
    assert (await client.post('/api/v1/auth/refresh')).status_code == 401
    response = await client.post('/api/v1/auth/magic-link', json={'email': user.email, 'next': '/events'})
    if response.status_code == 202:
        verify = await client.post('/api/v1/auth/magic-link/verify', json={'token': _token_from_mail(queue)})
        assert verify.status_code >= 400


async def test_admin_is_not_locked_out_by_skolstream_deactivation(db, client, queue):
    await _magic_login(client, queue, email='admin@skolkovo.example.org')
    user = await db.scalar(select(User).where(User.email == 'admin@skolkovo.example.org'))
    person = Person(
        source='skolstream', skolstream_id=uuid.uuid4(), email=user.email, last_name='Админов', is_active=False
    )
    db.add(person)
    await db.flush()
    user.person_id = person.id
    user.is_superuser = True
    await db.commit()

    assert (await client.get('/api/v1/auth/me')).status_code == 200
