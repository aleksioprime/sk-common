from redis.asyncio import Redis

from sk_common.runtime import settings

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis.dsn, decode_responses=True)
    return _client


async def hit_limit(redis: Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Счётчик попыток в окне. Возвращает True, если лимит превышен."""
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return count > limit
