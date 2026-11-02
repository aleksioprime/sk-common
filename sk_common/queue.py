"""Постановка фоновых задач в arq. В тестах подменяется через зависимость get_queue."""

from typing import Any, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from sk_common.runtime import settings


class Queue(Protocol):
    async def enqueue(self, function: str, *args: Any, **kwargs: Any) -> None: ...


class ArqQueue:
    def __init__(self):
        self._pool: ArqRedis | None = None

    async def enqueue(self, function: str, *args: Any, **kwargs: Any) -> None:
        if self._pool is None:
            self._pool = await create_pool(
                RedisSettings(host=settings.redis.host, port=settings.redis.port, database=settings.redis.db)
            )
        await self._pool.enqueue_job(function, *args, **kwargs)


_queue = ArqQueue()


def get_queue() -> Queue:
    return _queue
