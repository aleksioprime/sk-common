"""Тесты пакета на отдельной базе sk_common_test. Пропускаются, если PostgreSQL или Redis недоступны."""

import os

os.environ.setdefault('DB__NAME', 'sk_common_test')
os.environ['DB__NULL_POOL'] = 'true'
os.environ['REDIS__DB'] = '2'
os.environ.setdefault('ENVIRONMENT', 'local')

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from fastapi import APIRouter, FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import sk_common  # noqa: E402
from sk_common import redis as redis_module  # noqa: E402
from sk_common.auth.router import create_auth_router  # noqa: E402
from sk_common.config import ServiceSettings  # noqa: E402
from sk_common.db import NEXT_ID_SQL, get_engine, get_session_maker  # noqa: E402
from sk_common.models import Base  # noqa: E402
from sk_common.people.router import router as people_router  # noqa: E402
from sk_common.queue import get_queue  # noqa: E402
from sk_common.skolstream import get_skolstream_client  # noqa: E402
from sk_common.testing import FakeQueue, FakeSkolstream  # noqa: E402
from sk_common.web import add_common_middleware  # noqa: E402

ORIGIN = 'http://localhost:8501'

settings = ServiceSettings(project_name='SK Test', secret_key='test-secret-key-for-sk-common-tests-only')
sk_common.configure(settings)


def create_app() -> FastAPI:
    app = FastAPI()
    add_common_middleware(app)
    api = APIRouter(prefix='/api')
    api.include_router(create_auth_router())
    api.include_router(people_router)
    app.include_router(api)
    return app


async def _prepare_database() -> bool:
    db = settings.db
    try:
        conn = await asyncpg.connect(
            host=db.host, port=db.port, user=db.user, password=db.password, database='postgres', timeout=3
        )
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        if not await conn.fetchval('select 1 from pg_database where datname = $1', db.name):
            await conn.execute(f'create database "{db.name}"')
    finally:
        await conn.close()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        for statement in NEXT_ID_SQL:
            await connection.execute(text(statement))
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return True


@pytest.fixture(scope='session')
def database_ready() -> bool:
    return asyncio.run(_prepare_database())


@pytest.fixture
async def db(database_ready):
    """Чистая БД для теста."""
    if not database_ready:
        pytest.skip('PostgreSQL недоступна')
    async with get_session_maker()() as session:
        names = ', '.join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        await session.execute(text(f'truncate {names} restart identity cascade'))
        await session.commit()
        yield session


@pytest.fixture
async def redis_clean(database_ready):
    redis_module._client = None
    client = redis_module.get_redis()
    try:
        await client.flushdb()
    except Exception:
        pytest.skip('Redis недоступен')
    yield client
    await client.aclose()
    redis_module._client = None


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def skolstream() -> FakeSkolstream:
    return FakeSkolstream()


@pytest.fixture
async def client(queue, skolstream) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_queue] = lambda: queue
    app.dependency_overrides[get_skolstream_client] = lambda: skolstream
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN, headers={'Origin': ORIGIN}) as http:
        yield http
