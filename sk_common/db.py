from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func, text
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from sk_common.runtime import settings

NAMING_CONVENTION = {
    'ix': 'ix_%(column_0_label)s',
    'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}


class Base(AsyncAttrs, DeclarativeBase):
    """Общая база моделей: модели пакета и сервиса лежат в одних метаданных, внешние ключи на users работают."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    """53-битный snowflake из функции next_id() (NEXT_ID_SQL): безопасен для JavaScript, больше ID из NocoBase."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, server_default=text('next_id()'))


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ID = (миллисекунды с 2020-01-01 << 12) | (nextval % 4096).
# 41 бит времени + 12 бит счётчика = 53 бита: число безопасно для JavaScript до 2089 года.
# Эпоха раньше, чем у NocoBase, поэтому новые ID всегда больше перенесённых.
NEXT_ID_SQL = (
    'CREATE SEQUENCE IF NOT EXISTS global_id_seq',
    """
    CREATE OR REPLACE FUNCTION next_id() RETURNS bigint
    LANGUAGE plpgsql AS $$
    DECLARE
        epoch_ms CONSTANT bigint := 1577836800000;
        now_ms bigint;
        seq bigint;
    BEGIN
        seq := nextval('global_id_seq') % 4096;
        now_ms := floor(extract(epoch FROM clock_timestamp()) * 1000);
        RETURN ((now_ms - epoch_ms) << 12) | seq;
    END;
    $$
    """,
)
DROP_NEXT_ID_SQL = ('DROP FUNCTION IF EXISTS next_id()', 'DROP SEQUENCE IF EXISTS global_id_seq')

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        db = settings.db
        if db.null_pool:
            _engine = create_async_engine(db.dsn, echo=db.echo, poolclass=NullPool)
        else:
            _engine = create_async_engine(
                db.dsn, echo=db.echo, pool_pre_ping=True, pool_size=db.pool_size, max_overflow=db.max_overflow
            )
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_maker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_maker()() as session:
        yield session
